# AutoDeploy

병원 납품용 Ubuntu 서버에 자사 컨테이너 제품을 자동 설치하고 Slack으로 진행상황을 공유하는 봇.

## 개요

- 운영 머신: 사내 맥미니 (launchd 24/7)
- 오케스트레이션: 맥미니 → SSH → 병원용 Ubuntu 24.04 (`connecteve` 계정)
- 설치 절차: `~/gateway-infra-next` 동기화 → infra `.sh` (sudo) → app `.sh` (non-sudo) → kubectl 헬스체크
- 진행 알림: Slack 채널 스레드. `@autodeploy install/status/list/cancel/help`

자세한 문서:
- [개발지시서](docs/specs/dev-spec-autodeploy-mvp-20260521.md)
- [디자인 명세](docs/specs/design-spec-autodeploy-mvp-20260521.md)
- [운영 가이드 (설치/launchd/명령/문제해결)](docs/operations.md)

## 개발

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
chmod 600 .env
# .env에 실제 시크릿 입력

pytest
```

## 보안 원칙

- SSH 패스워드·Slack 토큰·Bitbucket App Password는 **.env에만** (gitignore + chmod 600)
- 코드·문서·메모리·로그·Slack 메시지에 평문 노출 금지
