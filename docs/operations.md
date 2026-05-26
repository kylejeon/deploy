# AutoDeploy 운영 가이드

맥미니(`192.168.100.195`)에 AutoDeploy를 설치하고 24/7로 띄우는 절차.

## 1. 사전 준비

### 맥미니 (오케스트레이터)
- macOS 맥미니에 인터넷 + 사내망 연결
- Python 3.11+ 설치 (3.14 검증)
- git 설치 (이 레포 clone용)
- Slack 앱 발급 (Bot Token, App-Level Token, Channel ID, Allowed User IDs)
- Bitbucket App Password (gateway-infra-next 접근용)
- 병원용 타겟 서버 자격증명 (`connecteve` 패스워드)

### 타겟 서버 (Ubuntu 24.04)

**필수 사전 설치**:
- `openssh-server` — 봇의 SSH 접속을 위함. 설치 후 `PasswordAuthentication yes` 확인
- `connecteve` 계정 (sudo NOPASSWD 권장)

```bash
sudo apt update && sudo apt install -y openssh-server
sudo systemctl enable --now ssh
```

**자동 설치되는 도구** (사전 설치 불필요, 봇이 알아서 깔아줌):
- `git` — git_pull 단계 진입 시 누락 감지하면 `sudo apt-get install -y git` 자동 실행
- `kubectl`, `docker`, 컨테이너 런타임 등 — 인프라 스크립트(`setup-*.sh`)가 책임

미리 깔아두는 게 마음 편하면:
```bash
sudo apt install -y git
```

## 2. 설치

```bash
# 1) 코드 동기화 (claude 브랜치)
cd ~
git clone https://github.com/kylejeon/deploy.git deploy
cd deploy
git checkout claude

# 2) 가상환경 + 의존성
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 3) .env 작성 (시크릿은 채팅 거치지 말고 터미널에서 직접)
cp .env.example .env
chmod 600 .env
nano .env   # 또는 vim/code 등
```

`.env` 채울 항목 (모두 운영 토큰; 절대 git 커밋 금지):
- `SLACK_BOT_TOKEN=xoxb-...`
- `SLACK_APP_TOKEN=xapp-...`
- `SLACK_CHANNEL_ID=C...`
- `AUTODEPLOY_ALLOWED_USERS=U01...,U02...`
- `SSH_PASSWORD=...` (`connecteve`)
- `BITBUCKET_APP_PASSWORD=ATBB...`

```bash
# 4) 검증 — 단위 테스트가 모두 통과해야 함
pytest

# 5) 한 번 직접 실행해서 connect 확인 (Ctrl+C로 종료)
python -m autodeploy
```

`Socket Mode 연결 시작` 로그가 뜨고 Slack 채널에 `@autodeploy help` 명령이 응답하면 OK.

## 3. launchd 등록 (24/7)

```bash
# 1) plist 복사 + 경로 치환
cp deploy/launchd/com.connecteve.autodeploy.plist ~/Library/LaunchAgents/

# 2) __USER__, __INSTALL_DIR__를 실제 값으로 치환
sed -i '' "s|__USER__|$USER|g; s|__INSTALL_DIR__|$HOME/deploy|g" \
    ~/Library/LaunchAgents/com.connecteve.autodeploy.plist

# 3) 로그 디렉토리 미리 생성
mkdir -p ~/Library/Logs/autodeploy

# 4) 등록 + 시작
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.connecteve.autodeploy.plist
launchctl print gui/$(id -u)/com.connecteve.autodeploy
```

부팅 시 자동 시작 (`RunAtLoad=true`), 실패 시 자동 재시작 (`KeepAlive=true`, 10초 백오프).

## 4. 운영 명령

| 작업 | 명령 |
|------|------|
| 상태 확인 | `launchctl print gui/$(id -u)/com.connecteve.autodeploy` |
| 재시작 | `launchctl kickstart -k gui/$(id -u)/com.connecteve.autodeploy` |
| 중지 | `launchctl bootout gui/$(id -u)/com.connecteve.autodeploy` |
| 로그 (stdout) | `tail -f ~/Library/Logs/autodeploy/stdout.log` |
| 로그 (stderr) | `tail -f ~/Library/Logs/autodeploy/stderr.log` |
| DB 직접 조회 | `sqlite3 "$HOME/Library/Application Support/autodeploy/state.db"` |

## 5. Slack 명령어 (운영자용 — `@autodeploy help`)

```
@autodeploy install <IP> --type=<TYPE> --code=<병원코드> [--name="..."] [--address="..."]
@autodeploy status [job-id]
@autodeploy list [N]
@autodeploy cancel <job-id>     # v1.1 예정
@autodeploy help
```

유효 TYPE (`config/deployment_types.yaml`):
- `on-premise` — 병원 내부망 단독
- `hybrid-with-ai` — Hybrid + inference 컨테이너 포함
- `hybrid-without-ai` — Hybrid + AWS의 inference 사용

## 6. 문제 해결

### 봇이 시작 안 됨
1. `tail -50 ~/Library/Logs/autodeploy/stderr.log`로 에러 확인
2. `.env`의 필수 변수 누락 시 `[설정 오류] missing required env var: ...` 로그
3. 토큰 만료/회수 시 Slack API `invalid_auth`

### 멘션을 안 받음
1. Slack 앱의 OAuth scopes 확인: `app_mentions:read`, `chat:write`, `chat:write.public`
2. Event Subscriptions에 `app_mention` 구독 + Socket Mode 활성화 확인
3. 봇을 채널에 초대했는지 확인 (`/invite @autodeploy`)

### 설치 작업이 SSH 단계에서 실패
1. 맥미니에서 타겟 서버까지 ssh 직접 가능한지: `ssh connecteve@<IP>`
2. `connecteve` 패스워드 변경 여부
3. 타겟 서버 방화벽/포트 22 차단 여부

### 설치 작업이 git_pull 단계에서 실패
1. Bitbucket App Password 만료/회수 여부 → 재발급
2. 타겟 서버에서 `bitbucket.org` 도달 가능한지

### 설치 작업이 헬스체크에서 타임아웃
1. 타겟 서버에서 `kubectl get pods -A` 직접 실행 — 어떤 pod이 비정상인지
2. 이미지 풀 실패 (registry 접근) 가능성
3. 설치 스크립트가 부분적으로 실패했을 수 있음 — DB의 `script_logs` 확인

## 7. 자격증명 회전 (보안 정책)

`.env`의 토큰들은 정기 회전이 권장됩니다. 회전 절차:

1. 새 토큰 발급 (Slack/Bitbucket 관리 페이지)
2. 터미널에서 `.env` 직접 편집 — **새 토큰을 채팅/이슈/메신저에 절대 붙여넣지 말 것**
3. `launchctl kickstart -k gui/$(id -u)/com.connecteve.autodeploy`로 재시작
4. 이전 토큰 폐기

## 8. 참고

- 명세: [docs/specs/dev-spec-autodeploy-mvp-20260521.md](specs/dev-spec-autodeploy-mvp-20260521.md), [design-spec](specs/design-spec-autodeploy-mvp-20260521.md)
- 진행 기록: [progress.txt](../progress.txt)
- CLAUDE.md 작업 규칙: [../CLAUDE.md](../CLAUDE.md)
