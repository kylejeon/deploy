# 개발지시서 — AutoDeploy MVP

- **작성자**: @planner
- **작성일**: 2026-05-21
- **버전**: v0.1 (초안)
- **연결 리서치**: 별도 리서치 없이 사용자(CEO) 인터뷰 기반으로 작성
- **상태**: 초안 (사용자 컨펌 필요)

---

## 1. 배경 / 문제

회사는 병원에 자사 컨테이너 제품을 탑재한 Ubuntu 서버를 납품한다. 현재는 사내에서 사전 설치를 **수동 SSH + 스크립트**로 진행하는데:

- 매번 동일한 절차를 사람이 반복 → 휴먼 에러 발생 가능
- 진행 상황을 CEO/엔지니어가 즉시 알기 어려움
- 어느 단계에서 실패했는지 사후 추적이 어려움
- 이력(언제 어느 서버에 무엇을 깔았는가)이 흩어져 있음

**AutoDeploy**는 이 사전 설치 워크플로를 **CEO 맥미니에서 Slack 명령으로 트리거**하고, 진행 상황을 **Slack 채널에서 실시간 공유**하며, 설치 이력을 SQLite에 기록하는 도구다.

---

## 2. 목표 / 비목표

### 목표
1. 맥미니에서 **Slack 명령**으로 병원용 Ubuntu 서버에 자동 설치 트리거
2. 인프라 스크립트 → 어플리케이션 스크립트 **순차 실행**
3. **단계별 진행 상황을 Slack 스레드**로 실시간 공유 (현재 단계, stdout 미리보기, 완료/실패)
4. **설치 이력을 SQLite에 저장** + Slack에서 조회 (`status`, `list`)
5. **CEO + 엔지니어 2~3명**이 사용 (화이트리스트)
6. 맥미니 부팅 시 자동 기동 (launchd 24/7)

### 비목표 (v1에서 다루지 않음)
- 운영 중인 병원 서버 원격 관리/모니터링 (별도 도구)
- 자사 제품 admin web 자체 수정 (자사 제품팀 책임)
- 일반 LLM 트러블슈팅·Q&A (범위 외 — 봇은 정해진 명령만)
- 자동 롤백 / 재시도 후 자동 복구 (실패 시 사람이 결정)
- **동시 다중 설치 (v1은 1건만 동시 진행)** — 큐 도입은 v2
- 웹 대시보드 / 별도 GUI
- 다채널 운영 (단일 채널 고정)

---

## 3. 유저 스토리

- **US-1** (CEO): "Slack 채널에서 `@autodeploy install 192.168.1.50`을 보내면, 봇이 즉시 작업 ID를 알려주고 단계별 진행을 스레드에 올려준다."
- **US-2** (엔지니어): "설치 중 실패하면 어느 단계·어떤 에러로 실패했는지 즉시 본문에 빨간색으로 강조되어 알림이 온다."
- **US-3** (CEO): "`@autodeploy list 10`으로 최근 10건의 설치 이력 (IP, 상태, 소요시간)을 한눈에 본다."
- **US-4** (엔지니어): "진행 중 작업이 무엇인지 알고 싶을 때 `@autodeploy status`로 현재 상태를 본다."
- **US-5** (CEO): "설치 정상 완료 시 admin web 접속 URL을 받아 바로 병원 정보를 등록하러 간다."
- **US-6** (CEO): "권한이 없는 외부인이 채널에 들어와 명령을 시도하면 봇이 거절한다."

---

## 4. 기능 요구사항

### F1. Slack 봇 (Socket Mode)
| ID | 요구사항 |
|----|---------|
| F1.1 | `@autodeploy install <IP> [--code=<병원코드>] [--name="<병원이름>"] [--address="<주소>"]` — 새 설치 시작. 옵션은 모두 선택사항이나 입력 시 jobs 테이블에 기록 |
| F1.2 | `@autodeploy status [job-id]` — 진행 중 작업 상태 조회 (job-id 생략 시 최신 1건) |
| F1.3 | `@autodeploy list [N]` — 최근 N건 (기본 10, 최대 50) 이력 조회 |
| F1.4 | `@autodeploy cancel <job-id>` — 진행 중 작업 취소 (**현재 단계 종료 후** 중단, 강제 종료 아님) |
| F1.5 | `@autodeploy help` — 명령 목록 |
| F1.6 | 봇 멘션 없는 메시지는 무시 |
| F1.7 | 알 수 없는 명령은 사용법 힌트 응답 |

### F2. 설치 워크플로
| ID | 요구사항 |
|----|---------|
| F2.1 | SSH 접속: `connecteve@<ip>` (패스워드는 환경변수에서 읽음) |
| F2.2 | 타겟 서버에서 git 레포 pull (디렉토리: `/opt/autodeploy/scripts/`, 레포 주소는 **D1 결정 후** env로 주입) |
| F2.3 | `infra.sh` 실행 (`sudo`) — stdout/stderr를 라인 단위로 수신 |
| F2.4 | `app.sh` 실행 (`sudo`) — 동일 |
| F2.5 | 헬스체크: admin web HTTP GET (URL 패턴은 **D4 결정 후**) — 최대 60초, 2초 간격 |
| F2.6 | 완료 시 admin web URL을 Slack 스레드에 게시 |

### F3. 진행 상황 보고
| ID | 요구사항 |
|----|---------|
| F3.1 | 작업 시작 시 채널에 **새 메시지 + 스레드** 생성, 이후 모든 보고는 해당 스레드 안 |
| F3.2 | 각 단계 진입/완료마다 스레드 메시지 |
| F3.3 | 스크립트 실행 중 **5초마다** 현재 stdout **마지막 10줄**을 코드블록으로 표시 (한 메시지를 `chat.update`로 갱신, 새 메시지 폭주 방지) |
| F3.4 | 실패 시: 종료 코드 + stderr 마지막 20줄 + 단계명을 **빨간 attachment**로 강조 |
| F3.5 | 정상 완료 시: 총 소요시간, admin web URL, 입력된 병원 정보(있으면) 요약 |

### F4. 상태 저장 (SQLite)
| ID | 요구사항 |
|----|---------|
| F4.1 | 모든 작업 시작·종료·단계 전이를 `jobs`, `job_events`에 기록 |
| F4.2 | 스크립트 stdout/stderr는 `script_logs`에 라인 단위 저장 |
| F4.3 | 이력 조회는 `jobs` 테이블 정렬·필터로 |
| F4.4 | DB 파일 위치: `~/Library/Application Support/autodeploy/state.db` (XDG-like 경로) |
| F4.5 | 일 1회 자동 백업 (같은 디렉토리에 `state.db.YYYYMMDD.bak`, 30일치 회전) |

### F5. 24/7 데몬 (launchd)
| ID | 요구사항 |
|----|---------|
| F5.1 | 맥미니 부팅 시 자동 시작 (launchd `KeepAlive=true`) |
| F5.2 | Slack Socket Mode WebSocket 끊기면 **지수 백오프 재연결** (1s, 2s, 4s, ... 최대 60s) |
| F5.3 | 로그 파일: `~/Library/Logs/autodeploy/autodeploy.log` (일별 회전, 30일 보관) |
| F5.4 | 정상 종료 신호 (SIGTERM) 처리 — 진행 중 작업은 `cancelled (system shutdown)` 상태로 기록 후 종료 |

### F6. 권한
| ID | 요구사항 |
|----|---------|
| F6.1 | 화이트리스트 Slack 사용자 ID 목록 (env: `AUTODEPLOY_ALLOWED_USERS=U01ABC,U02DEF,...`) |
| F6.2 | 화이트리스트 외 사용자의 명령은 "권한 없음" 메시지로 거부, `job_events` 또는 보안 로그에 기록 |
| F6.3 | 화이트리스트는 코드 재시작 없이 env 갱신 + `@autodeploy reload-users` 명령으로 적용 (선택사항, v1.1 가능) |

### F7. 자격증명 보안 (필수)
| ID | 요구사항 |
|----|---------|
| F7.1 | 모든 시크릿(SSH 패스워드, Slack 토큰 2종)은 **환경변수에서 읽음**. 코드/스크립트/문서/git 평문 금지 |
| F7.2 | 권장 보관: macOS Keychain 또는 `.env` (gitignore, chmod 600) |
| F7.3 | 로그·Slack 메시지에 시크릿이 새지 않도록 마스킹 |

---

## 5. DB 스키마 (SQLite)

```sql
-- 설치 작업 1건당 1 row
CREATE TABLE jobs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  target_ip         TEXT NOT NULL,
  hospital_code     TEXT,
  hospital_name     TEXT,
  hospital_address  TEXT,
  status            TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
  current_step      TEXT,   -- ssh_connect|git_pull|infra_install|app_install|healthcheck|done
  started_by        TEXT NOT NULL,    -- Slack user ID
  slack_channel     TEXT NOT NULL,
  slack_thread_ts   TEXT,             -- 스레드 부모 메시지 ts
  admin_web_url     TEXT,
  error_message     TEXT,
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at        TIMESTAMP,
  finished_at       TIMESTAMP
);

-- 단계 전이 및 의미 있는 이벤트
CREATE TABLE job_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step        TEXT NOT NULL,
  level       TEXT NOT NULL CHECK(level IN ('info','warn','error')),
  message     TEXT NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 스크립트 raw 출력 (디버깅용)
CREATE TABLE script_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step        TEXT NOT NULL,   -- infra_install | app_install | git_pull
  stream      TEXT NOT NULL CHECK(stream IN ('stdout','stderr')),
  line        TEXT NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_jobs_status      ON jobs(status);
CREATE INDEX idx_jobs_created_at  ON jobs(created_at DESC);
CREATE INDEX idx_job_events_job   ON job_events(job_id, created_at);
CREATE INDEX idx_script_logs_job  ON script_logs(job_id, step, id);
```

**마이그레이션**: 단일 파일 SQLite이므로 코드에서 `CREATE TABLE IF NOT EXISTS` 부트스트랩. 스키마 변경 시 `schema_version` 테이블 + 마이그레이션 스크립트 도입(v1.1).

---

## 6. 상태/이벤트 모델

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running: 워커 픽업
    running --> succeeded: healthcheck 통과
    running --> failed: 단계 에러
    running --> cancelled: 사용자 cancel
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
```

**단계 (current_step)**:

```
ssh_connect → git_pull → infra_install → app_install → healthcheck → done
```

각 단계 진입 시 `job_events`에 `info`, 실패 시 `error` 기록.

---

## 7. 에러 / 엣지케이스

| 상황 | 처리 |
|------|------|
| SSH 연결 실패 | 지수 백오프 3회 재시도 → 모두 실패 시 `failed`, 마지막 에러 저장 |
| git pull 실패 (네트워크/인증/충돌) | 재시도 없음 → 즉시 `failed`, stderr 전체 저장 |
| sudo 패스워드 프롬프트 등장 | 사전 NOPASSWD 가정 (D3 결정). 발견 시 `failed` + "NOPASSWD sudoers 필요" 메시지 |
| 스크립트 비정상 종료 (exit != 0) | 즉시 `failed`, stderr 마지막 20줄 보고 |
| 헬스체크 60초 안에 200 응답 없음 | `failed (healthcheck timeout)` |
| 같은 IP에 동시 install | 두 번째 요청 거부 ("작업 #N 진행 중"), 기존 job-id 안내 |
| Slack WebSocket 끊김 | 진행 중 작업은 계속, 메시지는 메모리 큐 → 재연결 시 일괄 전송 |
| 맥미니 재부팅 (진행 중 작업 있는 경우) | 종료 시 `cancelled (system shutdown)` 기록. 자동 재개 안 함 |
| Slack `chat.update` rate limit | 5초 간격 + 실패 시 지수 백오프, 마지막 갱신만 유지 |
| `cancel` 명령 받음 | 현재 단계 끝까지 진행 후 다음 단계 진입 시 중단, `cancelled` 기록 |
| 화이트리스트 외 사용자 명령 | 거부 응답 + 보안 로그 (`job_events` 또는 별도) |
| stdout 라인이 매우 김 (> 4KB) | 라인 단위로 자르되 DB에는 그대로, Slack 표시는 truncate |

---

## 8. 수용 기준 (Acceptance Criteria)

| ID | Given / When / Then |
|----|---------------------|
| AC-1 | Given 화이트리스트 사용자, When `@autodeploy install 192.168.1.50` 전송, Then 5초 이내 봇이 "작업 #N 시작" 응답 + 스레드 생성 |
| AC-2 | When 각 단계 진입, Then 스레드에 단계명 + 시작 시각 게시 |
| AC-3 | When 스크립트 실행 중, Then 5초마다 stdout 마지막 10줄을 코드블록으로 갱신 (메시지 폭주 없음) |
| AC-4 | When 모든 단계 정상 완료, Then admin web URL + 총 소요시간 게시, `jobs.status='succeeded'` |
| AC-5 | When SSH 3회 재시도 모두 실패, Then 실패 메시지 (빨간) + `jobs.status='failed'`, `error_message`에 마지막 에러 |
| AC-6 | When `@autodeploy list 5`, Then 최근 5건 표 형식 (id, ip, status, 소요시간, 시작시각) |
| AC-7 | Given 화이트리스트 외 사용자, When 명령 전송, Then "권한 없음" 응답, 작업 미생성 |
| AC-8 | When 맥미니 재부팅, Then launchd가 봇 자동 시작 + Slack 재연결 60초 이내 |
| AC-9 | Given IP `X`에 진행 중 작업, When 동일 IP에 install 재요청, Then 거부 + 기존 job-id 안내 |
| AC-10 | Then 코드/문서/git/Slack 메시지 어디에도 SSH 패스워드·Slack 토큰 평문 없음 (정적 검사 + 리뷰) |
| AC-11 | When `@autodeploy cancel <id>`, Then 다음 단계 진입 시 중단, `jobs.status='cancelled'`, 스레드에 취소 메시지 |
| AC-12 | When `@autodeploy status`, Then 가장 최근 작업의 현재 단계 + 경과 시간 표시 |

---

## 9. 결정 필요 항목 (CEO 컨펌)

| ID | 항목 | 추천안 | 이유 |
|----|------|--------|------|
| **D1** | 설치 스크립트 git 레포 주소 | TBD (CEO 확인 중) | 명세 핵심 입력 |
| **D2** | 타겟 서버 스크립트 디렉토리 | `/opt/autodeploy/scripts/` | FHS 관행, 권한 분리 쉬움 |
| **D3** | sudo NOPASSWD 적용 범위 | `connecteve ALL=(ALL) NOPASSWD: /usr/bin/docker, /opt/autodeploy/scripts/*.sh` (제한적) | 보안과 자동화의 균형 |
| **D4** | 헬스체크 URL 패턴 | `http://<ip>:<PORT>/health` 또는 `/login` (자사 제품팀 확인 필요) | 명확한 200 응답이 있는 endpoint |
| **D5** | 병원 정보 입력 시점 | install 명령의 옵션으로 받기 (`--code`, `--name`, `--address`) | DB 추적 + admin web 등록 시 참고 가능 |
| **D6** | Slack 채널 정책 | 단일 채널 고정 (env: `SLACK_CHANNEL_ID`) | v1 단순화 |
| **D7** | 구현 언어/스택 | Python 3.11+ (`slack-sdk` + `asyncssh` + `aiosqlite`) | 사용자 친숙, 비동기 SSH·DB 쉬움, 맥미니 표준 |
| **D8** | 시크릿 저장소 | 시작은 `.env` (gitignore + chmod 600), 추후 macOS Keychain 이전 | 빠른 시작, 보안 점진 강화 |
| **D9** | 봇 이름/멘션 토큰 | `@autodeploy` | 직관적 |
| **D10** | DB 파일 위치 | `~/Library/Application Support/autodeploy/state.db` | macOS 관행 |

위 추천안은 사용자 별다른 의견 없으면 **기본값으로 채택**하고 @designer · @developer 단계로 진행 가능.

---

## 10. 참고 / 링크

- 프로젝트 메모리: `~/.claude/projects/-Users-yonghyuk-deploy/memory/project_autodeploy_overview.md`
- 시크릿 정책 메모리: `~/.claude/projects/-Users-yonghyuk-deploy/memory/feedback_no_secrets_in_files.md`
- 에이전트 정의: [.claude/agents/planner.md](../../.claude/agents/planner.md)
- 라우팅: [CLAUDE.md](../../CLAUDE.md)
- 참고 라이브러리:
  - Slack Bolt for Python (Socket Mode): https://slack.dev/bolt-python/
  - asyncssh: https://asyncssh.readthedocs.io/
  - aiosqlite: https://aiosqlite.omnilib.dev/

---

## 다음 단계

1. **CEO 컨펌**: §9 결정 필요 항목 D1~D10 검토
2. 확정 후 → **@designer** 호출하여 Slack 메시지 포맷 / 명령 응답 UX의 디자인 명세 (`docs/specs/design-spec-autodeploy-mvp-*.md`) 작성
3. 그 다음 → **@developer** 호출하여 구현 시작
