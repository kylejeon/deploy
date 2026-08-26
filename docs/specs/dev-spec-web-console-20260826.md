# 개발지시서 — AutoDeploy 웹 콘솔 (hubctl 기반)

- **작성자**: @planner
- **작성일**: 2026-08-26
- **버전**: v1.2 (D1·D2 확정, F9 추가)
- **연결 스펙**: `docs/specs/dev-spec-autodeploy-mvp-20260521.md` (기존 Slack 봇)
- **프로토타입**: https://claude.ai/code/artifact/feb4c815-30be-4e68-8518-c1f5ccd47e6d
- **대상 저장소**: `~/hub-provisioning` (bin/hubctl, ansible-core 2.21.1)
- **상태**: 초안 (구현 전 용혁 컨펌 필요 — §13 결정 필요 항목)

---

## 1. 배경 / 문제

현재 설치 진행상황은 Slack 스레드가 유일한 창구다. 작업이 30분~1시간 걸리는데
Slack은 (a) 지난 작업을 되짚기 어렵고 (b) 여러 서버를 동시에 설치할 때 스레드가
엉키며 (c) 전체 로그를 훑어보기 불편하다.

동시에 설치 방식 자체가 바뀌고 있다. 기존 `gateway-infra-next`의 `setup-*.sh`
직접 실행에서, Ansible 기반 `hub-provisioning`(`hubctl`)으로 전환 중이다. 새
콘솔은 처음부터 hubctl 래퍼로 만든다.

### 실측으로 확인된 전제 (2026-08-26, `~/hub-provisioning`)

| 항목 | 확인 내용 |
|---|---|
| CLI | `bootstrap`·`configure`·`install`·`verify`·`preflight`·`patch(create\|apply)`·`status`·`rollback`·`clean` |
| `--` 뒤 인자 | ansible-playbook 으로 그대로 전달 (`PASSTHROUGH`) |
| `-l` | ansible `--limit`. 쉼표 나열로 부분 집합 지정 가능. 생략 시 인벤토리 전체 |
| 부분 실패 | `any_errors_fatal`·`serial` 미설정 → 실패 호스트만 빠지고 나머지는 다음 play 까지 계속 |
| 출력 | 파이프로도 줄 단위 즉시 flush (버퍼링 없음). 갱신 단위는 **TASK** |
| `-K` 대체 | `ANSIBLE_BECOME_PASSWORD_FILE` 지원 (ansible-core 2.12+) |
| `clean` | **대화형 확인 필수** (`read -r`), `-y` 명시 거부 → 봇은 playbook 직접 호출 |
| `patch` 원샷 | `[y/N]` 프롬프트 → 봇은 `create` / `apply` 2단계로 분리 |
| 포트 | traefik hostPort 고정 8000(frontend)·8001(temporal-web)·8002(web-pacs) |
| `sites.yml` | `.gitignore` 대상 (실 IP/계정). 커밋 충돌 없음 |

---

## 2. 목표 / 비목표

### 목표
1. 사내 LAN(추후 외부 고정 IP)에서 브라우저로 **설치 진행상황을 실시간 확인**
2. 웹에서 **설치 시작 / 취소 / 재시도 / 초기화 / patch / verify** 실행
3. **여러 서버 동시 설치** — 한 번의 hubctl 실행(`-l a,b,c`)으로 처리
4. **서버 인벤토리(sites.yml) 관리** — 웹에서 추가·편집·삭제
5. **SSH 키 자동 등록** — 서버 등록 시 봇이 공개키를 타겟에 심어 이후 무인 실행 보장
6. 계정 기반 인증 (현재 2명)
7. 기존 Slack 알림은 그대로 유지 — 웹에서 시작한 작업도 Slack 스레드에 게시

### 비목표 (v1에서 다루지 않음)
- HTTPS 종단 처리 (외부 노출 시 리버스 프록시 전제 — §11)
- 계정 관리 웹 화면 (CLI로만)
- `hubctl status` (컨트롤러 KUBECONFIG 기준이라 원격 서버엔 무의미 → `verify` 사용)
- airgap 번들 반입 경로 (`patch apply -e patch_bundle=<파일>`)
- 작업 예약/스케줄링
- 모바일 전용 레이아웃 (반응형까지만)

---

## 3. 유저 스토리

1. 용혁은 노트북에서 `http://맥미니:8080` 을 열고 로그인해 지금 돌고 있는 설치의
   현재 단계와 경과 시간을 5초 안에 파악한다.
2. 수진은 새 병원 2곳에 동시에 설치해야 한다. 서버 2대를 체크하고 환경 `dev`를
   골라 시작하면 한 번의 실행으로 병렬 처리된다.
3. 설치 중 한 대가 ImagePullBackOff 로 실패한다. 목록에서 실패한 호스트만 보이고,
   "실패한 1대만 재시도" 버튼으로 그 서버만 다시 돌린다.
4. 설치가 꼬인 서버를 반납해야 한다. 초기화 화면에서 `--uninstall` 을 고르고
   호스트명을 직접 입력해 확인한 뒤 실행한다.
5. 새 앱 버전을 올리려 한다. patch 를 걸면 번들이 생성되고 **바뀌는 앱 목록**이
   화면에 뜬다. 확인 후 승인해야 서버에 적용된다.
6. 신규 서버를 인벤토리에 추가한다. 웹 폼에 5개 필드를 넣으면 sites.yml 이 갱신된다.

---

## 4. 기능 요구사항

### F1. 인증 / 세션

- **저장**: `users` 테이블. 비밀번호는 `hashlib.scrypt` (n=2^14, r=8, p=1, dklen=32),
  솔트 16바이트 랜덤. 평문·가역 암호화 저장 금지.
- **세션**: `sessions` 테이블에 토큰 해시 저장. 쿠키 `ad_session`,
  `HttpOnly` + `SameSite=Lax` + `Path=/`, 외부 노출 시 `Secure`.
  기본 만료 **14일**, 슬라이딩 갱신 없음(재로그인).
- **CLI**: `python -m autodeploy adduser <id>` / `passwd <id>` / `deluser <id>` / `users`
  - 비밀번호는 프롬프트로만 입력받는다(인자로 받지 않음 — 셸 히스토리 노출 방지).
- **무차별 대입 방어**: (IP, username) 조합으로 5회 실패 시 60초 잠금.
  실패 응답은 "아이디 또는 비밀번호가 올바르지 않습니다" 단일 메시지.
- 모든 API·페이지는 세션 필수. 미인증 시 `/login` 리다이렉트(HTML) 또는 401(JSON).

### F2. 서버 인벤토리 (sites.yml)

- **읽기**: `HUBCTL_REPO_PATH/inventory/sites.yml` 파싱. 스키마는 5개 필드:
  `host`(키) / `ansible_host` / `ansible_user` / `site_name` / `profile`
  - `profile` ∈ `onprem | hybrid-with-ai | hybrid-without-ai`
- **쓰기**: 임시파일 + `os.replace` 로 원자적 교체. 쓰기 직전
  `sites.yml.bak-<YYYYmmdd-HHMMSS>` 백업(최근 10개 유지).
- **동시 편집 방지**: 웹이 읽은 시점의 파일 `mtime_ns`를 폼에 실어 보내고, 저장 시
  현재 값과 다르면 409 거부 + "다른 곳에서 수정됐습니다. 새로고침 후 다시 시도" 안내.
- **실행 중 편집 금지**: 진행 중 작업이 있으면 저장 거부(인벤토리가 바뀌면 실행 중인
  playbook 의 대상이 흔들린다).
- **검증**: host 이름 `^[A-Za-z0-9][A-Za-z0-9_-]*$`, `ansible_host`는 IPv4/호스트명,
  중복 host 금지, `profile` enum.
- **메모(병원명)**: sites.yml 스키마에 없는 값이므로 AutoDeploy DB `server_meta`에 보관.
- **주석 유실**: pyyaml 은 주석을 보존하지 못한다 → 봇이 파일 상단에 고정 헤더 주석을
  다시 써넣는다. (D3 참고)

### F3. 작업 실행 (hubctl Runner)

실행 위치는 항상 `HUBCTL_REPO_PATH` (기본 `~/hub-provisioning`).

| 작업 | 실행 명령 |
|---|---|
| install | `./bin/hubctl install -e <env> -l <hosts>` [`-- -e hub_deploy_ref=<ref>` [`-e hub_deploy_ref_type=tag`]] |
| configure | `./bin/hubctl configure -e <env> -l <hosts>` [동일 passthrough] |
| verify | `./bin/hubctl verify -l <hosts>` |
| rollback | `./bin/hubctl rollback -l <hosts>` |
| patch (1) | `./bin/hubctl patch create -- -e hub_deploy_ref=<ref>` [`-e hub_deploy_ref_type=tag`] |
| patch (2) | `./bin/hubctl patch apply -l <hosts>` — **웹 승인 후에만** |
| clean | `ansible-playbook clean.yml -i inventory/sites.yml -l <host> -e confirm=<host> -e level=<reset\|uninstall> -e keep_data=<true\|false>` |

- **`-l` 은 항상 명시**한다. 전체 선택이어도 호스트를 전부 나열한다.
  (`-l` 생략 시 "실행 시점의 인벤토리 전체"가 대상이 되어, 나중에 추가된 서버까지 딸려 들어간다.)
- **clean 이 hubctl 을 우회하는 이유**: `cmd_clean()` 이 `read -r` 로 호스트명을
  직접 받고 못 읽으면 exit 2, `-y` 도 명시 거부한다. `clean.yml` 헤더 주석이 위
  직접 호출 형태를 정식 사용법으로 문서화하고 있다. 웹이 이미 호스트명 타이핑
  확인을 받으므로 그 값을 `confirm=` 으로 넘긴다.
- **patch 원샷 금지**: `cmd_patch()` 기본 분기가 `[y/N]` 프롬프트를 띄운다.
  반드시 `create` → (웹 승인) → `apply` 2단계로 실행한다.
- **sudo 비밀번호**: `-K` 를 쓰지 않는다. `ANSIBLE_BECOME_PASSWORD_FILE` 환경변수로
  0600 임시 파일 경로를 넘긴다. 파일은 작업 종료 시 즉시 삭제한다.
- **환경변수 주입**: launchd 데몬은 `~/.zshrc` 를 읽지 않는다. hubctl 이 요구하는
  `VAULT_ADDR`·`VAULT_TOKEN`·`HUB_DEPLOY_GIT_TOKEN`·`HUB_DEPLOY_GIT_USER`·AWS 자격을
  로그인 셸에서 상속받기 위해 `zsh -lc "<명령>"` 형태로 실행한다.
  명령 조립 시 모든 인자에 `shlex.quote` 적용.
- **동시 실행 1개**: 컨트롤러 쪽 자원(zarf 캐시, ECR 로그인, `bundles/`)이 겹치므로
  전역 큐로 직렬화한다. 대기 중 작업은 `queued` 상태로 목록에 보인다.
  (여러 서버 동시 설치는 "작업 여러 개"가 아니라 "작업 하나에 `-l a,b,c`" 로 처리한다.)
- **취소**: `start_new_session=True` 로 프로세스 그룹을 만들고, 취소 시 그룹에
  `SIGTERM` → 10초 후에도 살아있으면 `SIGKILL`. 다중 호스트 작업은 **전부** 중단된다
  (프로세스가 하나이므로 개별 호스트 취소 불가).

### F4. 로그 수집 / 스트리밍

- 서브프로세스의 stdout·stderr 를 **줄 단위**로 읽어 (a) `script_logs` 테이블 적재
  (b) 메모리 링버퍼(최근 2000줄) (c) SSE 구독자에게 push.
- **색상**: `ANSIBLE_FORCE_COLOR` 는 쓰지 않는다. 색 없는 평문을 받아 접두사로 분류하고
  웹에서 칠한다.
- **라인 분류** (`stream` 컬럼):

  | 판정 | 값 |
  |---|---|
  | `PLAY [` / `TASK [` / `━━ ` 로 시작 | `task` |
  | `ok: [` | `ok` |
  | `changed: [` | `chg` |
  | `fatal: [` / `FAILED!` / `unreachable` | `err` |
  | `skipping: [` | `skip` |
  | `PLAY RECAP` 이후 `<host> : ok=N ...` | `recap` |
  | 그 외 | `out` |

- **호스트 추출**: `^(ok|changed|fatal|skipping|unreachable): \[([^\]]+)\]` 의 2번 그룹,
  RECAP 구간은 `^(\S+)\s*:\s*ok=\d+` 의 1번 그룹. 매칭 없으면 `host=NULL`
  (컨트롤러 공통 줄 — 호스트 필터에서 항상 표시).
- **마스킹**: 기존 `mask_url_secrets` 재사용 + `VAULT_TOKEN`·become 비밀번호·
  `HUB_DEPLOY_GIT_TOKEN` 값이 문자열로 나타나면 `***` 치환. DB 적재 **전에** 적용.
- **SSE**: `GET /api/jobs/{id}/stream?after=<line_id>` — `after` 이후 줄부터 보낸다.
  15초마다 주석 하트비트(`: ping`). 연결 끊김 시 브라우저가 마지막 line_id 로 재연결.
- **"마지막 출력 이후"**: 서버가 `last_line_at` 을 함께 내려주고 화면이 매초 갱신.
  60초 경과 시 강조, 120초 경과 시 "긴 작업 진행 중일 수 있습니다" 문구 추가.
  (TASK 단위 갱신이라 긴 TASK 중에는 수 분간 정적인 것이 정상 — 사용자 불안 방지 목적)

### F5. 진행 단계 판정

로그 스트림에서 **PLAY 이름**으로 단계 경계를 잡는다 (실측한 정확한 문자열):

| 단계 키 | 판정 문자열 |
|---|---|
| `preflight` | `━━ Preflight` (hubctl `header()` 출력) |
| `bootstrap` | `PLAY [Bootstrap (host -> empty k0s)]` |
| `configure` | `PLAY [Configuration (empty k0s -> platform)]` |
| `verify` | `PLAY [hubctl verify]` |
| `create` | `PLAY [패치 번들 생성 (patch_create)]` |
| `apply` | `PLAY [패치 번들 적용 (patch_apply)]` |
| `rollback` | `PLAY [패치 롤백 (patch_apply/rollback)]` |
| `clean` | `PLAY [Clean (초기화 — reset\|uninstall)]` |

- **호스트별 최종 상태**는 `PLAY RECAP` 의 `failed=` / `unreachable=` 값으로 판정한다.
  둘 다 0 이면 `succeeded`, 아니면 `failed`. RECAP 에 아예 안 나온 호스트는 `failed`
  (이전 play 에서 탈락).
- **작업 전체 상태**는 프로세스 종료 코드로 판정: `0` = succeeded, 그 외 = failed.
  단 취소로 죽인 경우는 `cancelled` 로 덮어쓴다.

### F6. 여러 서버 동시 설치

- 새 설치 화면은 **체크박스 다중 선택** + 전체 선택.
- 선택한 호스트를 쉼표로 이어 `-l` 하나로 넘긴다 → ansible 이 forks 단위로 병렬 처리.
  `ansible.cfg` 에 forks 설정이 없어 **기본 5**. 6대 이상 선택 시 화면에 안내를 띄운다.
- `-e env` 와 `hub_deploy_ref` 는 **실행당 하나**라 선택한 전부에 공통 적용된다.
  서로 다른 환경이 필요하면 작업을 나눠야 한다.
- 프로파일은 호스트별 인벤토리 값이라 섞여도 무방하다 (화면에 `혼합` 표기).
- 부분 실패 시: 실패한 호스트만 대상으로 하는 `재시도` 를 기본 버튼으로,
  전체 재시도를 보조 버튼으로 제공한다.

### F7. Slack 연동 유지

- 웹에서 시작한 작업도 기존 `SlackNotifier` 로 스레드를 만들고 진행상황을 게시한다.
- `jobs.started_by` 에 `web:<username>` 형태로 기록 (기존 Slack 경로는 `slack:@user`).
- 작업 상세 화면의 "Slack 스레드 열기" 는 `slack_thread_ts` 로 딥링크를 만든다.

### F9. SSH 키 등록 (설치 전 필수 준비)

hubctl 은 타겟에 **키 인증**으로 접속한다. 키가 안 깔려 있으면 설치가
`Permission denied (publickey,password)` / `UNREACHABLE` 로 죽는다. runbook §1-1 이
사람에게 시키던 작업을 봇이 대신한다.

**동작 순서** (서버 1대당 딱 한 번)

1. **컨트롤러 키 확인** — `~/.ssh/id_ed25519(.pub)` 이 없으면
   `ssh-keygen -t ed25519 -N "" -C autodeploy@macmini -f ~/.ssh/id_ed25519` 로 생성.
   (2026-08-26 실측: 맥미니에 이미 존재 → 실제로는 건너뛴다)
2. **타겟에 비밀번호로 접속** — 기존 `AsyncSSHClient`(asyncssh) 재사용.
   비밀번호는 웹 폼에서 **1회 입력**받아 메모리에만 두고, 작업 종료 시 폐기한다.
3. **공개키 설치** — 타겟에서 순서대로 실행:
   ```
   mkdir -p ~/.ssh && chmod 700 ~/.ssh
   touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
   grep -qxF '<PUBKEY>' ~/.ssh/authorized_keys || echo '<PUBKEY>' >> ~/.ssh/authorized_keys
   ```
   - `grep -qxF` 로 **중복 등록을 막는다** (재실행해도 안전 = 멱등).
   - 타겟에 `~/.ssh` 를 만드는 것이 목적이므로 **타겟에서 `ssh-keygen` 은 실행하지 않는다.**
     쓰지 않을 개인키를 납품 서버에 남기지 않기 위함. (타겟이 Bitbucket 등 외부로
     나가야 할 일이 생기면 그때 별도 단계로 추가한다.)
4. **키 인증 검증** — 비밀번호를 **끄고** 키만으로 재접속해 `true` 를 실행한다.
   `asyncssh.connect(..., password=None, client_keys=[~/.ssh/id_ed25519])`.
   여기서 성공해야 등록 완료로 인정한다 (runbook 의 `ssh <user>@<host> true` 에 해당).
5. **기록** — `server_meta.key_installed_at` 갱신.

**연동 지점**

- 서버 추가/편집 화면에 "SSH 키 등록" 섹션 (비밀번호 입력).
- 서버 목록에 **SSH 키 컬럼** — 미등록이면 `키 등록` 버튼.
- 새 설치에서 **키 미등록 서버가 선택되면 시작을 막고** 해당 서버를 지목해 안내한다.
  (설치 30분 돌리고 나서 SSH 로 죽는 것보다 시작 전에 거르는 편이 낫다)

**주의**

- 비밀번호는 DB·로그·응답 어디에도 남기지 않는다. 로그 마스킹 대상에 추가.
- `known_hosts` 는 `None`(검증 안 함)으로 둔다 — `ansible.cfg` 의
  `host_key_checking = False` 와 정합.
- 타겟 계정이 sudo 를 쓰려면 여전히 become 비밀번호가 필요하다. SSH 키 등록과
  sudo 비밀번호는 별개다 (runbook 도 `-K` 는 sudo 용이라고 못박고 있다).

---

### F8. 화면

프로토타입(위 링크)이 확정 명세다. 구성:

1. **로그인** — 아이디/비밀번호
2. **대시보드** — preflight 상태 스트립(Vault·hvac·AWS·Bitbucket) / 진행 중 카드 /
   통계 타일 / 최근 작업 표
3. **작업 상세** — 메타 + 실행 명령 + 진행바 / 단계 타임라인 + 호스트별 상태 /
   로그 콘솔(호스트 필터·자동 스크롤·마지막 출력 표시) / 결과 블록
4. **서버** — sites.yml 목록 표 + 추가·편집·삭제 모달 + YAML 미리보기 + 행별 액션
5. **새 설치** — 서버 다중 선택 / 환경 / ref / 실행 계획·명령 미리보기 / 확인 모달
6. **서버 초기화** — 서버 선택 / 3방식 / 실행 계획 / **호스트명 타이핑 확인**

---

## 5. 아키텍처 / 모듈 분할

```
launchd (com.connecteve.autodeploy)
└─ python -m autodeploy
   ├─ AutoDeployBot (기존, Slack Socket Mode)
   ├─ WebApp (신규, aiohttp) ── :8080
   │   ├─ auth (세션·로그인)
   │   ├─ api (jobs / servers / stream)
   │   └─ static (단일 HTML + JS)
   └─ JobQueue (신규, 동시 1개)
       └─ HubctlRunner ── zsh -lc → ./bin/hubctl … / ansible-playbook clean.yml
            └─ LogPump ── 줄 파싱 → DB + SSE 브로드캐스트
```

### 신규 파일
| 파일 | 역할 |
|---|---|
| `src/autodeploy/web/__init__.py` | aiohttp app 팩토리, 라우팅 |
| `src/autodeploy/web/auth.py` | scrypt 해시, 세션 발급·검증, 로그인 제한 |
| `src/autodeploy/web/api.py` | JSON 엔드포인트 |
| `src/autodeploy/web/sse.py` | SSE 브로드캐스터 |
| `src/autodeploy/web/static/console.html` | 프로토타입 기반 단일 페이지 |
| `src/autodeploy/hubctl.py` | 명령 조립 + 서브프로세스 실행 + 취소 |
| `src/autodeploy/ansible_log.py` | 라인 분류·호스트 추출·PLAY/RECAP 파싱 |
| `src/autodeploy/inventory.py` | sites.yml 읽기/쓰기/검증/백업 |
| `src/autodeploy/ssh_keys.py` | 컨트롤러 키 확보 + 타겟 authorized_keys 설치 + 키 인증 검증 |
| `src/autodeploy/queue.py` | 전역 작업 큐 (동시 1개) |
| `src/autodeploy/cli.py` | `adduser` / `passwd` / `deluser` / `users` |

### 기존 파일 변경
- `models.py` — `JobKind`, `JobHost`, `User`, `Session` 추가. `Job` 에 `kind/env/ref/ref_type/mode/hosts`
- `repository.py` — 신규 테이블 CRUD, `script_logs.host` 반영
- `schema.sql` + `db.py::_migrate` — §6 스키마 추가 (기존 DB 멱등 마이그레이션)
- `settings.py` — `WEB_ENABLED`·`WEB_HOST`·`WEB_PORT`·`HUBCTL_REPO_PATH`·`BECOME_PASSWORD` 등
- `__main__.py` — WebApp·JobQueue 기동/종료, 서브커맨드 디스패치(`adduser` 등)
- `.env.example` — 신규 항목

---

## 6. DB 스키마 변경

```sql
-- 신규
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  pw_hash       BLOB NOT NULL,
  pw_salt       BLOB NOT NULL,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at TIMESTAMP,
  disabled_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,          -- sha256(랜덤 32바이트)
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  TIMESTAMP NOT NULL,
  client_ip   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS job_hosts (
  job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  host             TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
  recap_ok         INTEGER,
  recap_changed    INTEGER,
  recap_failed     INTEGER,
  recap_unreachable INTEGER,
  PRIMARY KEY (job_id, host)
);

CREATE TABLE IF NOT EXISTS server_meta (   -- sites.yml 에 없는 웹 전용 부가정보
  host              TEXT PRIMARY KEY,
  memo              TEXT,                    -- 병원명 등
  key_installed_at  TIMESTAMP,               -- SSH 공개키 등록 + 키 인증 검증 성공 시각 (F9)
  updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 기존 jobs 확장 (db.py::_migrate 에서 ALTER TABLE ADD COLUMN, 멱등)
--   kind        TEXT NOT NULL DEFAULT 'install'   -- install|configure|patch|rollback|verify|clean
--   env         TEXT                              -- dev|stage|prod (patch/rollback/verify/clean 은 NULL)
--   ref         TEXT                              -- hub_deploy_ref
--   ref_type    TEXT                              -- branch|tag|commit
--   clean_mode  TEXT                              -- reset|reset-keep|uninstall
--   exit_code   INTEGER
--   cancel_by   TEXT
-- 기존 target_ip/deployment_type/hospital_code 는 legacy 데이터용으로 nullable 유지

-- script_logs 확장
--   host  TEXT     -- 줄에서 추출한 호스트 (없으면 NULL)
```

- `jobs.status` CHECK 에 `awaiting` 추가 필요 (patch 승인 대기).
  SQLite 는 CHECK 변경이 안 되므로, 기존 DB는 `_migrate` 에서 테이블 재생성
  (`jobs_new` 생성 → INSERT SELECT → DROP → RENAME) 한다. **작업 전 DB 백업**.

---

## 7. HTTP API

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/login` | `{username, password}` → 세션 쿠키 |
| POST | `/api/logout` | 세션 폐기 |
| GET | `/api/me` | 로그인 사용자 |
| GET | `/api/preflight` | hubctl preflight 실행 결과 (4항목) |
| GET | `/api/servers` | sites.yml + server_meta 병합 목록 (`mtime_ns` 포함) |
| POST | `/api/servers` | 추가 (`mtime_ns` 검증) |
| PUT | `/api/servers/{host}` | 수정 |
| DELETE | `/api/servers/{host}` | 삭제 |
| POST | `/api/servers/{host}/ssh-key` | SSH 키 등록 — `{password}` 1회. 성공 시 `key_installed_at` 갱신 |
| GET | `/api/jobs?limit=` | 목록 |
| GET | `/api/jobs/{id}` | 상세 (단계·호스트별 상태 포함) |
| POST | `/api/jobs` | 작업 생성 — `{kind, hosts[], env?, ref?, ref_type?, clean_mode?, confirm?}` |
| POST | `/api/jobs/{id}/cancel` | 취소 |
| POST | `/api/jobs/{id}/approve` | patch 적용 승인 → `patch apply` 실행 |
| POST | `/api/jobs/{id}/reject` | patch 적용 거부 (번들 유지) |
| GET | `/api/jobs/{id}/stream?after=` | SSE 로그 스트림 |
| GET | `/api/jobs/{id}/log` | 전체 로그 텍스트 다운로드 |

- 모든 변경 요청(POST/PUT/DELETE)은 CSRF 토큰 필요 (세션에 묶인 랜덤값, 헤더로 전달).
- `POST /api/jobs` 의 `kind=clean` 은 `confirm` 필드가 대상 호스트명과 정확히 일치해야
  수락한다 (서버 측에서도 재검증 — 화면 검증만 믿지 않는다).

---

## 8. 상태 / 이벤트 모델

```
queued ──▶ running ──▶ succeeded
             │  │
             │  └────▶ failed        (exit_code != 0)
             │  └────▶ cancelled     (사용자 취소)
             └────────▶ awaiting ──▶ running ──▶ succeeded   (patch: create 완료 후 승인)
                            └──────▶ cancelled                (승인 거부 — 번들은 유지)
```

- `job_hosts.status` 는 작업 시작 시 전부 `running`, RECAP 파싱 시점에 확정.
- `job_events` 는 기존 테이블을 그대로 쓴다 (`step` 컬럼에 §F5 단계 키 저장).

---

## 9. 에러 / 엣지케이스

| 상황 | 처리 |
|---|---|
| Vault 토큰 만료 | preflight 가 실패 → 작업 생성 시 400 + "Vault 로그인 필요" 안내. 대시보드 스트립에 빨강 |
| `hubctl` 없음 / 경로 오류 | 기동 시 1회 점검, 실패 시 로그 경고 + 웹 배너 |
| `-l` 오타·없는 호스트 | ansible 이 `leaves us with no hosts to target` 로 즉시 종료 → 실패로 기록. 웹은 목록에서 고르므로 발생 불가 |
| 진행 중 인벤토리 수정 시도 | 409 거부 |
| 작업 중 데몬 종료 | 기동 시 `running`/`awaiting` 인 작업을 `failed` 로 정리하고 "데몬 재시작으로 중단됨" 이벤트 기록 |
| SSE 연결 끊김 | 브라우저가 마지막 line_id 로 재연결, 서버는 그 이후만 전송 |
| 로그 폭주 | DB 적재는 전부, 브라우저 전송은 최근 2000줄. 작업당 로그 상한 20만 줄 초과 시 이후는 파일에만 |
| 동시 작업 요청 | 큐에 `queued` 로 대기. 화면에 "앞선 작업 종료 후 시작됩니다" 표시 |
| patch 승인 없이 방치 | `awaiting` 은 24시간 후 자동 `cancelled` (번들은 남음) |
| SSH 키 미등록 서버로 설치 시작 | 400 거부 + 해당 호스트 지목. 화면에서도 시작 버튼 차단 |
| SSH 키 등록 시 비밀번호 오류 | "비밀번호가 올바르지 않습니다" — 재입력. 3회 실패 시 60초 대기 |
| 타겟 sshd 미설치 / 포트 차단 | `Connection refused` 를 그대로 노출하고 "타겟에 openssh-server 가 설치돼 있는지 확인" 안내 |
| authorized_keys 에 이미 같은 키 | `grep -qxF` 로 중복 없이 통과 (멱등) |
| become 비밀번호 파일 유출 | 0600, 작업 종료 즉시 삭제, 로그 마스킹 |

---

## 10. 수용 기준 (Acceptance Criteria)

- **AC-1** 미인증 상태로 `/` 접근 시 로그인 화면이 뜬다.
- **AC-2** `autodeploy adduser yonghyuk` 로 만든 계정으로 로그인된다. 잘못된 비밀번호 5회 후 60초 잠긴다.
- **AC-3** 서버 화면에서 신규 호스트를 추가하면 `sites.yml` 이 갱신되고 `.bak-*` 백업이 남는다.
- **AC-4** 새 설치에서 서버 2대를 선택하면 명령 미리보기가 `-l a,b` 로 표시되고, 실행 시 실제로 그 2대만 대상이 된다.
- **AC-5** 진행 중 작업 상세에서 로그가 TASK 단위로 흘러들어오고, "마지막 출력 이후" 표시가 매초 갱신된다.
- **AC-6** 다중 호스트 작업에서 호스트 필터를 누르면 해당 호스트 줄 + 공통 줄만 남는다.
- **AC-7** 한 호스트만 실패한 경우 호스트별 상태에 성공/실패가 갈려 표시되고, "실패한 N대만 재시도" 가 그 호스트만 대상으로 새 작업을 만든다.
- **AC-8** 작업 취소 시 ansible 프로세스가 실제로 종료되고 상태가 `cancelled` 이 된다.
- **AC-9** 초기화는 호스트명을 정확히 입력해야 실행되며, `--keep-data` 선택 시 `keep_data=true` 로 전달된다.
- **AC-10** patch 는 번들 생성 후 `awaiting` 에서 멈추고, 승인해야 apply 가 실행된다. 거부 시 서버는 변경되지 않는다.
- **AC-11** 웹에서 시작한 작업이 Slack 채널에도 스레드로 게시된다.
- **AC-12** 데몬을 재시작해도 지난 작업 목록과 로그가 그대로 조회된다.
- **AC-13** 로그 어디에도 `VAULT_TOKEN`·become 비밀번호·Bitbucket 토큰 평문이 없다.
- **AC-14** 라이트/다크 테마 양쪽에서 화면이 읽힌다.
- **AC-15** 키 미등록 서버에 "키 등록"을 실행하면 타겟 `authorized_keys` 에 공개키가 1줄 추가되고, 비밀번호 없이 키만으로 재접속이 성공한다. 두 번 실행해도 줄이 늘지 않는다.
- **AC-16** 키 미등록 서버가 포함된 채로는 설치를 시작할 수 없다.

---

## 11. 보안

- v1 은 **사내 LAN 전용**. 바인드 주소는 `WEB_HOST` 로 제어하며 기본 `0.0.0.0` 대신
  명시적 설정을 요구한다.
- 외부 고정 IP 노출 시 **반드시 HTTPS 리버스 프록시를 앞에 둔다**. 세션 쿠키가
  평문으로 흐르면 계정이 그대로 털린다. 프록시 도입 전까지 외부 노출 금지.
- 이 콘솔은 **타겟 서버에 sudo 로 임의 변경을 가할 수 있는 권한**을 웹에 노출한다.
  계정은 최소 인원에게만 발급하고, 모든 작업 생성·취소·승인은 `jobs.started_by` /
  `job_events` 에 사용자명과 함께 남긴다(감사 로그).
- 파괴적 작업(clean)은 서버 측에서도 `confirm` 재검증.
- 비밀은 `.env`(0600)와 프로세스 환경에만 존재. DB·로그·응답에 실리지 않는다.

---

## 12. 구현 순서

| Phase | 내용 | 산출 |
|---|---|---|
| A | 스키마 마이그레이션 + `inventory.py` + `ssh_keys.py` + `cli.py`(계정) | 테스트 통과, CLI 로 계정 생성 가능, 실서버 1대에 키 등록 성공 |
| B | `hubctl.py` + `ansible_log.py` + `queue.py` — 실행·파싱·취소 | 단위 테스트(가짜 프로세스), 실제 verify 1회 성공 |
| C | aiohttp 앱 + 인증 + 서버/작업 조회 API | 로그인 후 목록 조회 |
| D | SSE 스트리밍 + 작업 생성/취소/승인 | 실시간 로그 확인 |
| E | 프로토타입 HTML 이식 + Slack 연동 + 문서(`docs/operations.md`) | AC 전체 통과 |

---

## 13. 결정 필요 항목 (용혁 컨펌)

| # | 항목 | 기본 제안 |
|---|---|---|
| ~~D1~~ | **확정 (2026-08-26)** — Slack 은 기존 경로 그대로 두고, **설치는 웹에서 진행**한다. 웹 콘솔은 hubctl 전용. Slack 코드는 이번 작업에서 건드리지 않는다 (알림 게시만 유지) |
| ~~D2~~ | **확정 (2026-08-26)** — `ansible-playbook clean.yml -e confirm=<host> -e level=… -e keep_data=…` 직접 호출. 웹에서 호스트명 타이핑 확인을 받고 서버 측에서 재검증 |
| D3 | sites.yml 주석 유실 허용 여부 (pyyaml). 보존하려면 `ruamel.yaml` 의존성 추가 필요 | 고정 헤더 주석만 재기록, 의존성 추가 안 함 |
| D4 | 병원 등록/제품 등록 단계를 hubctl install 뒤에 붙일 때, 병원 식별자로 무엇을 쓸지 (`site_name`? 별도 병원코드?) | `site_name` 사용 |
| D5 | become 비밀번호를 `.env` 에 둘지, 별도 파일/키체인에 둘지 | `.env` (기존 `SSH_PASSWORD` 와 동일 수준) |
| D9 | SSH 키 등록 시 비밀번호를 매번 입력받을지, `.env` 의 `SSH_PASSWORD` 를 기본값으로 채워둘지 | **매번 입력** (서버마다 비밀번호가 다를 수 있음). `.env` 값은 자동 사용하지 않음 |
| D6 | 컨트롤러에서 ansible 프로세스를 동시에 몇 개까지 돌릴지 (§F3 참고). **서버 대수 제한이 아니라 "작업 개수" 제한** | 전역 1개 + 대기 큐 |
| D7 | 웹 포트 8080 / 세션 만료 14일 | 그대로 |
| D8 | 외부 고정 IP 단계에서 HTTPS 프록시로 무엇을 쓸지 (Caddy 권장) | 별도 결정, v1 범위 밖 |

---

## 14. 참고

- 프로토타입: https://claude.ai/code/artifact/feb4c815-30be-4e68-8518-c1f5ccd47e6d
- hubctl: `~/hub-provisioning/bin/hubctl`
- 운영 가이드: `~/hub-provisioning/docs/RUNBOOK-QA.md`, `RUNBOOK-hubctl.md`, `RUNBOOK-clean.md`
- 기존 봇 스펙: `docs/specs/dev-spec-autodeploy-mvp-20260521.md`
