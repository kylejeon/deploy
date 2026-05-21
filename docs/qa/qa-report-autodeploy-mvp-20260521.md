# QA 검수 보고서 — AutoDeploy MVP

- **작성자**: @qa
- **작성일**: 2026-05-21
- **검수 대상**:
  - 명세: [dev-spec v0.4](../specs/dev-spec-autodeploy-mvp-20260521.md), [design-spec v0.1](../specs/design-spec-autodeploy-mvp-20260521.md)
  - 코드: claude 브랜치 (커밋 미생성, 워킹트리 기준)
  - 산출물: `src/autodeploy/*.py`, `tests/*.py`, `config/deployment_types.yaml`, `deploy/launchd/com.connecteve.autodeploy.plist`, `docs/operations.md`
- **테스트**: 134/134 통과 (pytest 0.95s)

---

## 1. 요약 판정

**판정: Conditional Pass** — 핵심 워크플로 + Slack 통합 + DB 모델 + 데몬 entry point + 운영 자산이 모두 갖춰져 있고 단위 테스트가 두텁다. 그러나 **운영 신뢰성에 영향을 주는 결함 3건**(시크릿 누출 경로 1건 + 재시도 누락 1건 + 종료 처리 1건)이 있어 그 수정 전에는 본격 운영 권장 안 함. 사내 검증 운영은 가능 (이미 사내 사전 설치 컨텍스트라 폐쇄망 리스크는 낮음).

---

## 2. 수용 기준 체크리스트 (AC-1 ~ AC-17)

| AC | 내용 | 판정 | 증거 / 메모 |
|----|------|------|------------|
| AC-1 | install 5초 이내 ack + 스레드 | ✅ | [slack_app.py:118](../../src/autodeploy/slack_app.py#L118) → workflow.run task 시작 + [slack_notifier.py:48](../../src/autodeploy/slack_notifier.py#L48)에서 parent + ack 즉시 게시. Slack API 정상 시 1~2초 이내 |
| AC-2 | 각 단계 진입 시 스레드에 단계명 | ✅ | [slack_notifier.py:71](../../src/autodeploy/slack_notifier.py#L71) `step_started` |
| AC-3 | 스크립트 중 5초마다 stdout 마지막 10줄 | ✅ | [slack_notifier.py:91](../../src/autodeploy/slack_notifier.py#L91) 시간 기반 flush + [messages.py](../../src/autodeploy/messages.py) `stdout_preview` 마지막 10줄. 단위 테스트 통과 |
| AC-4 | 정상 완료 시 admin URL + status='succeeded' | ✅ | [workflow.py:170](../../src/autodeploy/workflow.py#L170) `_mark_success` + [test_workflow.py:71](../../tests/test_workflow.py#L71) |
| AC-5 | **SSH 실패 3회 재시도 후 failed** | ❌ | **재시도 로직 미구현**. [ssh.py:65](../../src/autodeploy/ssh.py#L65) `AsyncSSHClient.__aenter__`는 단일 시도. workflow는 첫 SSHError에서 즉시 failed. **결함 D-1** |
| AC-6 | `list N` 최근 N건 표 | ✅ | [slack_app.py:79](../../src/autodeploy/slack_app.py#L79) + [messages.py `list_response`](../../src/autodeploy/messages.py). 50건 cap 검증됨 |
| AC-7 | 권한 없는 사용자 거부 + 작업 미생성 | ✅ | [slack_app.py:55](../../src/autodeploy/slack_app.py#L55) early return + DB write 없음. `test_permission_denied_when_user_not_allowed` 통과 |
| AC-8 | launchd 자동 시작 + 60초 이내 재연결 | ⚠️ | launchd 자동 시작 ✅ ([plist](../../deploy/launchd/com.connecteve.autodeploy.plist) `KeepAlive=true`, `RunAtLoad=true`). 60초 이내 재연결은 slack-bolt 내장 백오프에 의존, **실제 측정 안 됨**. 첫 부팅 후 검증 필요 |
| AC-9 | 동일 IP 동시 install 거부 | ✅ | [slack_app.py:103](../../src/autodeploy/slack_app.py#L103) `find_active_by_ip` + `test_install_rejects_duplicate_active_ip` 통과 |
| AC-10 | 코드/문서/git에 시크릿 평문 없음 | ✅ | `grep -rE "xoxb-...|ATBBzv|zhsprxlqm"` → .env 외 없음. `.env`는 gitignore. 단 **결함 D-3 참조 (런타임 누출 경로)** |
| AC-11 | cancel | ⚠️ | **의도적 미구현** (v1.1). 명세 §9 D-items와 일치. handle_command는 안내 메시지 반환. 명세상 합의된 사항이라 결함 아님 |
| AC-12 | status → 현재 단계 + 경과 시간 | ⚠️ | 현재 단계 ✅. **경과 시간은 미표시** — [messages.py `status_response`](../../src/autodeploy/messages.py)는 단계명만 표시. **결함 D-4 (Medium)** |
| AC-13 | --type 누락 → "type 필수" + 유효 목록 | ✅ | [commands.py:81](../../src/autodeploy/commands.py#L81) ParseError + `test_install_missing_type_returns_parse_error` |
| AC-14 | 알 수 없는 type 거부 + 유효 목록 | ✅ | `test_install_unknown_type_returns_parse_error` |
| AC-15 | type별 정확한 스크립트·sudo·args | ✅ | `test_workflow.py::test_on_premise_uses_correct_scripts`, `test_hybrid_without_ai_passes_wo_ai_arg` |
| AC-16 | yaml에 type 추가 + 재시작 → 즉시 유효 | ✅ | config가 시작 시 동적 로드. 코드 변경 불필요 (구조적 보장). 실 yaml 변경 + 재시작 시나리오 별도 실측 권장 |
| AC-17 | list/status에 deployment_type 포함 | ✅ | `list_response`/`status_response` 모두 deployment_type 컬럼/필드 |

**합계: ✅ 13 · ⚠️ 3 · ❌ 1**

---

## 3. 결함 목록

### D-1 — SSH 연결 재시도 미구현 (High, 기능)

- **위치**: [src/autodeploy/ssh.py:48-72](../../src/autodeploy/ssh.py#L48) `AsyncSSHClient.__aenter__`
- **현재 동작**: `asyncssh.connect()` 1회 시도, 실패 시 즉시 `SSHError`. workflow는 그것을 `WorkflowError(SSH_CONNECT)`로 변환하여 즉시 failed.
- **명세 위반**: dev-spec §7 "SSH 연결 실패 | 지수 백오프 3회 재시도 → 모두 실패 시 failed" + AC-5
- **영향**: 일시적 네트워크 hiccup(병원 사내망 패킷 드롭, ARP 갱신 등)에서도 작업이 1회 만에 실패. 사용자가 수동 재시도 필요. **운영 신뢰성에 직접 영향**.
- **재현 경로**: 타겟 서버를 잠시 down → install 명령 → 즉시 failed (3회 시도 안 함)
- **권장 조치**: `AsyncSSHClient.__aenter__`에 명세대로 지수 백오프 3회 (예: 1s/2s/4s). 또는 workflow 레이어에서 SSHError를 retry decorator로 감싸기. 단위 테스트로 검증 가능 (FakeSSHClient에 fail_connect 카운트 옵션 추가).

### D-2 — 시스템 종료 시 진행 중 작업 미정리 (Medium, 안정성)

- **위치**: [src/autodeploy/__main__.py:120-130](../../src/autodeploy/__main__.py#L120) shutdown 핸들러
- **현재 동작**: SIGTERM 받으면 `bot_task.cancel()`만 호출. `workflow.run()`이 백그라운드 task로 떠 있는 상태에서 그대로 강제 종료.
- **명세 위반**: dev-spec F5.4 "정상 종료 신호 (SIGTERM) 처리 — 진행 중 작업은 `cancelled (system shutdown)` 상태로 기록 후 종료" + 엣지케이스 표 "맥미니 재부팅 → 종료 시 cancelled 기록"
- **영향**: 봇 재시작 후 DB의 해당 job은 `status='running'`인 채로 영원히 남음. `find_active_by_ip`가 그 job을 진행 중으로 인식 → 같은 IP에 install 영구 거부 (수동 SQL update 필요).
- **재현 경로**: install 시작 → 워크플로 진행 중 → `launchctl bootout` → DB의 해당 job 상태가 'running' 잔존
- **권장 조치**: `AutoDeployBot`에 진행 중 task 트랙킹(이미 `self._running_tasks` 존재). shutdown 시 각 task의 job_id를 DB에 `cancelled (system shutdown)`로 마크 + task.cancel() 후 timeout 안에 대기.

### D-3 — git clone 시 Bitbucket 토큰이 stderr 경유로 DB·Slack에 노출 위험 (High, 보안)

- **위치**: [src/autodeploy/git_sync.py:35-37](../../src/autodeploy/git_sync.py#L35), [src/autodeploy/workflow.py:160](../../src/autodeploy/workflow.py#L160) `_make_log_collector`
- **현재 동작**: `git clone https://user:TOKEN@bitbucket.org/...` 형태로 URL에 토큰 포함. clone 명령의 stderr가 workflow의 `_make_log_collector` 콜백을 통해 그대로:
  1. `script_logs` 테이블에 평문 저장 ([repository.py:79](../../src/autodeploy/repository.py#L79))
  2. `SlackNotifier.step_log` → 5초마다 chat.update의 stdout preview 코드블록에 표시 가능
- **위험**: git이 인증 실패·redirect·다른 에러 시 stderr에 URL 일부를 echo할 수 있음 (git 버전에 따라 다름). 실제 토큰이 새는지는 git 버전·로케일 의존. **이론적 위험 확실, 실측 필요**.
- **영향**: Bitbucket App Password가 봇 사용자가 보는 Slack 메시지·SQLite 파일에 평문으로 영구 저장될 가능성. 토큰 폐기·재발급 트리거.
- **권장 조치 (택 1)**:
  - (a) `git credential helper` 사용: 환경변수 `GIT_ASKPASS`를 임시 스크립트로 가리키게 → URL에 토큰 안 박음
  - (b) clone 시 `git -c credential.helper=...`로 stdin/env에서 패스워드 공급
  - (c) `on_line` 콜백에서 정규식으로 `https://USER:[^@]+@` 패턴 마스킹 (가장 빠른 임시 조치)
  - 최선은 (a) 또는 (b). (c)는 보조.

### D-4 — status 메시지에 경과 시간 누락 (Medium, UX)

- **위치**: [src/autodeploy/messages.py:255-279](../../src/autodeploy/messages.py#L255) `status_response`
- **현재 동작**: 진행 중 작업에 대해 IP·유형·현재 단계·요청자만 표시. **경과 시간 없음**.
- **명세 위반**: AC-12 "가장 최근 작업의 현재 단계 + 경과 시간 표시", design-spec M-8 "경과 시간 2분 47초"
- **영향**: 운영자가 "지금 작업이 얼마나 진행됐나" 한 번에 알 수 없음. Slack 스레드를 별도로 열어 시작 시각 확인 필요.
- **권장 조치**: `status_response(job)`에 `started_at` 받아서 `_fmt_duration(now - started_at)` 추가. DB 모델에 started_at은 이미 있음 (현재 dataclass에는 컬럼이 있지만 row→Job 변환에서 빠짐 → repository._row_to_job도 함께 보강 필요)

### D-5 — repository._row_to_job이 timestamp 컬럼을 무시 (Low, 일관성)

- **위치**: [src/autodeploy/repository.py:130-146](../../src/autodeploy/repository.py#L130) `_row_to_job`
- **현재 동작**: `created_at`/`started_at`/`finished_at` 컬럼을 Job 객체로 옮기지 않음. Job dataclass에는 해당 필드가 있지만 항상 None.
- **영향**: D-4 해결의 전제. status/list 메시지에서 시간 표시 불가. 운영 회고용 분석에도 제약.
- **권장 조치**: SQLite TIMESTAMP 컬럼을 datetime으로 파싱. aiosqlite의 `detect_types=PARSE_DECLTYPES` 또는 수동 `datetime.fromisoformat`.

---

## 4. 보안 점검 결과

| 항목 | 결과 | 비고 |
|------|------|------|
| 코드·문서·git·예시 평문 시크릿 | ✅ Clean | grep으로 노출 패턴 검사, .env(gitignore)만 |
| `.gitignore` 적용 | ✅ | `.env`, `.env.*`, `*.pem`, `*.key`, `credentials.json`, `secrets/` 모두 차단 |
| 셸 명령 주입 방지 | ✅ | `shlex.quote` 적용 — `code="HOSP; rm -rf /"` 같은 입력도 단일 인용 안에 갇힘 (`test_args_are_quoted_to_prevent_injection`) |
| Slack 메시지 텍스트에 시크릿 침투 | ✅ | 메시지 빌더는 hospital_code/name/address/IP만 노출. 토큰 미참조 |
| **git clone 토큰 stderr 누출** | ❌ | **D-3** |
| **`script_logs` 평문 저장** | ❌ | **D-3 동일 영향** |
| SSH known_hosts 검증 | ⚠️ | `known_hosts=None` 으로 검증 생략. 사내·폐쇄망 가정이라 위험 낮음. 운영 환경 확장 시 strict mode 옵션 추가 권장 |
| 봇 권한 모델 | ✅ | `AUTODEPLOY_ALLOWED_USERS` 화이트리스트. 빈 셋이면 개발 모드(모두 허용) — **운영 시 반드시 채워야 함**. 보고서에 운영 점검 항목으로 명시 |
| 봇 채널 제한 | ✅ | `SLACK_CHANNEL_ID` 일치 안 하면 거부 |
| 자격증명 회전 절차 | ✅ | `docs/operations.md §7`에 명시 |

---

## 5. 도메인 리스크 점검 (병원 환경)

| 리스크 | 평가 |
|-------|------|
| **자격증명 공유** | 모든 병원 서버가 같은 `connecteve` 패스워드 사용 시 한 곳 누출 = 전체 침해. **운영 정책 권장**: 병원별 패스워드 분리 또는 SSH 키 인증으로 전환. 의료 환경 인접이라 사고 비용 비가역. (운영 권고, 코드 결함 아님) |
| **폐쇄망 가정** | 사내 사전 설치라 인터넷 가능. 향후 운영 중 병원 서버 원격 관리 기능 추가 시 폐쇄망 시나리오 재설계 필요 (현 명세 비목표) |
| **kubectl 헬스체크 신뢰도** | `Running\|Completed` 외 모든 상태를 비정상 처리. Pending이 정상적인 init 단계일 수 있어 false positive 가능. 다만 보수적 판정이라 운영 안전성은 ✅ — 단계 타임아웃(D13: 10분)이 적정한지 실측 필요 |
| **운영자 새벽 디버깅** | error_message가 raw 에러라 약간 모호. 권장 조치(M-6)는 stub. 1차 디버깅 후 패턴 정비 권장 |
| **권한 모델 단순화** | CEO + 엔지니어 2-3명. 화이트리스트로 충분. 단 빈 셋 = 개발 모드라 운영 검증 시 채워졌는지 확인 필요 (체크리스트 항목) |
| **타겟 서버 부분 설치 잔존** | 작업 실패 시 타겟 서버는 부분 설치 상태로 남음. 자동 정리 없음 (의도). 운영 가이드(docs/operations.md)에 수동 정리 절차 명시 — 적정 |
| **App Password 누출 시 폭발 반경** | gateway-infra-next 레포 접근 가능. read만 가능한 별도 deploy key 사용 권장 (운영 권고) |

---

## 6. 관찰 / 개선 제안 (명세 외, 차단 아님)

| # | 제안 | 우선도 |
|---|------|------|
| O-1 | `launchctl` 설치 자동화 스크립트 (`deploy/install.sh`) — 현재 sed 수동 치환. 1회 실수 가능성 | Low |
| O-2 | `pytest --cov`로 커버리지 측정 + CI 통합 (GitHub Actions 등) | Medium |
| O-3 | M-6 "권장 조치" 영역에 단계·에러 패턴별 사전 정의된 힌트 (예: `ImagePullBackOff` → 레지스트리 권한 확인) — design-spec DD-8과 일치 | Medium |
| O-4 | Slack 봇 부팅 시 `chat.postMessage` 1회로 운영자 알림 ("AutoDeploy 봇 온라인 v0.1.0") — design-spec M-13 옵션. 현재 미구현 | Low |
| O-5 | `__main__`의 `bot_task` 비정상 종료 시 종료 코드 4 — Slack 4xx/5xx 같은 일시적 장애에도 종료. launchd가 재시작은 하지만 백오프 동안 알림 못 보냄. 일시 장애에는 봇 내부에서 재시도하는 게 더 안정적 | Medium |
| O-6 | `WorkflowConfig.admin_web_url_template`이 단순 `http://{ip}/` — D4 명시. 실 admin web 포트/경로 확인 후 갱신 필요 | High (배포 전) |
| O-7 | `time.monotonic()` 기반 시간 측정 — Phase 3b에서 한 번 함정 빠졌음. 다른 시간 측정 지점 (workflow의 step duration 등)에도 같은 패턴 없는지 재점검 | Low |
| O-8 | `pytest-asyncio` 모드 `auto` 사용 — 향후 sync 테스트 추가 시 `@pytest.mark.asyncio` 누락이 silent fail 가능. 명시적 `strict` 모드 검토 | Low |

---

## 7. 다음 단계 (@developer 작업 우선순위)

배포 전 반드시 해결 (Critical/High):

1. **D-3 토큰 stderr 마스킹** (Critical-Security) — git credential helper 도입 또는 on_line 콜백에서 URL 패턴 마스킹
2. **D-1 SSH 재시도** (High-Reliability) — 지수 백오프 3회 (1s/2s/4s)
3. **O-6 admin web URL 확정** (High-Functional) — 실 admin web 포트/경로 확인 후 `admin_web_url_template` 갱신

배포 후 v1.0.1에 포함 권장 (Medium):

4. **D-2 shutdown 시 진행 중 작업 cancelled 마크**
5. **D-4 + D-5 status 경과 시간 표시 + timestamp 컬럼 파싱**
6. **O-5 봇 비정상 종료 대신 내부 재시도**

v1.1로 미룸 (의도적):

- AC-11 cancel 명령 구현
- O-3 권장 조치 패턴 사전 정의
- 권한 모델 강화 (역할 분리 등)

---

## 8. 운영 배포 체크리스트 (배포 직전 점검)

- [ ] `.env`에 모든 필수 항목 채움 + chmod 600 + 평문 노출 경로 점검
- [ ] `AUTODEPLOY_ALLOWED_USERS` 비어있지 않음 (개발 모드 방지)
- [ ] D-3 마스킹 적용
- [ ] D-1 재시도 적용
- [ ] O-6 admin web URL 실 값 반영
- [ ] `pytest` 통과 (현재 134/134)
- [ ] 맥미니에서 `python -m autodeploy` 1회 수동 실행 + Slack `@autodeploy help` 응답 확인
- [ ] launchd 등록 + `launchctl print` 정상
- [ ] 재부팅 1회 → 자동 시작 확인 + 재연결 60초 이내 확인 (AC-8 실측)
- [ ] 테스트 서버에 install 1회 end-to-end 성공 (실측)
- [ ] Bitbucket App Password와 SSH 패스워드 회전 (대화 노출 이력 있음)
