"""env 기반 런타임 설정 로더. 시크릿은 env에서만 읽고 코드/문서엔 자리표시자."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class SettingsError(RuntimeError):
    pass


DEFAULT_DB_PATH = "~/Library/Application Support/autodeploy/state.db"


def resolve_db_path(env: Mapping[str, str] | None = None) -> Path:
    """DB 경로만 필요한 진입점(CLI 등)이 Slack 토큰 검증 없이 쓰기 위한 헬퍼."""
    e = env if env is not None else os.environ
    return Path(e.get("AUTODEPLOY_DB_PATH", DEFAULT_DB_PATH)).expanduser()


DEFAULT_HUBCTL_REPO_PATH = "~/hub-provisioning"


def resolve_hubctl_repo(env: Mapping[str, str] | None = None) -> Path:
    """hub-provisioning 저장소 경로. hubctl 은 항상 이 디렉터리에서 실행한다."""
    e = env if env is not None else os.environ
    raw = e.get("HUBCTL_REPO_PATH", "").strip() or DEFAULT_HUBCTL_REPO_PATH
    return Path(raw).expanduser()


def resolve_become_password(env: Mapping[str, str] | None = None) -> str:
    """ansible become(sudo) 비밀번호.

    타겟은 connecteve 계정 공통이라 SSH 비밀번호와 같다 (기존 workflow 도 동일 가정).
    계정을 분리하게 되면 BECOME_PASSWORD 로 따로 준다.
    """
    e = env if env is not None else os.environ
    return e.get("BECOME_PASSWORD", "").strip() or e.get("SSH_PASSWORD", "")


# hubctl 이 로그인 셸에서 상속받아야 하는 값들. 데몬(launchd)은 ~/.zshrc 를 읽지
# 않으므로 `zsh -lc` 로 감싸 실행한다. 여기 있는 이름은 마스킹 대상 판별에도 쓴다.
HUBCTL_SECRET_ENV: tuple[str, ...] = (
    "VAULT_TOKEN",
    "HUB_DEPLOY_GIT_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


@dataclass(frozen=True, slots=True)
class WebConfig:
    enabled: bool
    host: str
    port: int
    session_ttl_days: int
    secure_cookie: bool
    trust_forwarded: bool
    public_url: str


def _flag(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def resolve_web_config(env: Mapping[str, str] | None = None) -> WebConfig:
    """웹 콘솔 설정.

    바인드 주소 기본값은 **루프백**이다 (§11). 0.0.0.0 을 기본으로 두면 LAN 노출이
    설정 실수로 일어나는데, 이 콘솔은 타겟 서버에 sudo 로 임의 변경을 가할 수 있어
    노출 범위는 명시적 선택이어야 한다.
    """
    e = env if env is not None else os.environ
    try:
        port = int(e.get("WEB_PORT", "8080"))
    except ValueError as exc:
        raise SettingsError(f"WEB_PORT 는 정수여야 합니다: {e.get('WEB_PORT')!r}") from exc
    try:
        ttl = int(e.get("SESSION_TTL_DAYS", "14"))
    except ValueError as exc:
        raise SettingsError(
            f"SESSION_TTL_DAYS 는 정수여야 합니다: {e.get('SESSION_TTL_DAYS')!r}"
        ) from exc
    if ttl < 1:
        raise SettingsError("SESSION_TTL_DAYS 는 1 이상이어야 합니다")

    host = e.get("WEB_HOST", "").strip() or "127.0.0.1"
    # Slack 메시지에 넣을 콘솔 주소. 0.0.0.0 은 주소가 아니라 "전부"라는 뜻이라
    # 링크로 쓸 수 없다 — 그 경우엔 명시적으로 WEB_PUBLIC_URL 을 받아야 한다.
    public = e.get("WEB_PUBLIC_URL", "").strip().rstrip("/")
    if not public and host not in ("0.0.0.0", "::"):
        public = f"http://{host}:{port}"

    return WebConfig(
        enabled=_flag(e, "WEB_ENABLED"),
        host=host,
        port=port,
        session_ttl_days=ttl,
        secure_cookie=_flag(e, "WEB_SECURE_COOKIE"),
        trust_forwarded=_flag(e, "WEB_TRUST_FORWARDED"),
        public_url=public,
    )


def _expand_remote_home(path: str, ssh_user: str) -> str:
    """원격 서버 기준 `~` 확장. shlex.quote로 감싸도 안전한 절대경로로 만든다.

    파이썬의 Path.expanduser()는 로컬(맥미니) 홈을 쓰니 원격 셸용으로는 부적합.
    bash의 tilde expansion은 `~/foo`가 단일 인용 안에 들어가면 동작하지 않아서,
    `'~/gateway-infra-next'`가 literal '~' 폴더를 만드는 버그가 있었음.
    """
    if path == "~":
        return f"/home/{ssh_user}"
    if path.startswith("~/"):
        return f"/home/{ssh_user}/{path[2:]}"
    return path


@dataclass(frozen=True, slots=True)
class Settings:
    slack_bot_token: str
    slack_app_token: str
    slack_channel_id: str
    allowed_users: frozenset[str]
    ssh_user: str
    ssh_password: str
    bitbucket_user: str
    bitbucket_app_password: str
    db_path: Path
    config_path: Path
    repo_host_path: str
    repo_branch: str
    work_dir: str
    log_level: str
    # 설치 완료 후 자동 병원 등록용 마스터 계정 (on-premise + hybrid 공용).
    # 비어있으면 site_register 단계는 조용히 skip — 운영자가 env를 안 채웠어도
    # 기존 워크플로(SSH/스크립트)에는 영향 없음.
    site_admin_email: str
    site_admin_password: str
    # hybrid (with-ai/without-ai)에서 사용할 클라우드 site API base URL.
    # 운영 환경 분리가 필요하면 .env에서 prod로 오버라이드.
    site_cloud_base_url: str
    # site API 호출에 박는 x-api-env 헤더. Postman 캡쳐 기준 'dev'가 디폴트.
    site_api_env: str
    # Jira (생산관리 프로젝트) — product_register 단계용.
    # 비워두면 product_register 단계를 skip.
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_key: str
    # gateway-infra-next의 고정 NodePort. install 스크립트가 [INFO] X URL 출력을
    # 누락해도 Slack/site_register/product_register가 이 포트로 동작.
    port_frontend: int
    port_temporal: int
    port_webpacs: int
    # v2(웹 콘솔): hubctl 실행 경로 + become(sudo) 비밀번호.
    # 기존 Slack 워크플로는 이 값들을 쓰지 않으므로 기본값을 준다.
    hubctl_repo_path: Path = Path(DEFAULT_HUBCTL_REPO_PATH).expanduser()
    become_password: str = ""
    web: WebConfig = WebConfig(
        enabled=False,
        host="127.0.0.1",
        port=8080,
        session_ttl_days=14,
        secure_cookie=False,
        trust_forwarded=False,
        public_url="http://127.0.0.1:8080",
    )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = env if env is not None else os.environ

    def required(key: str) -> str:
        v = e.get(key, "").strip()
        if not v:
            raise SettingsError(f"missing required env var: {key}")
        return v

    allowed_raw = e.get("AUTODEPLOY_ALLOWED_USERS", "")
    allowed = frozenset(u.strip() for u in allowed_raw.split(",") if u.strip())

    cfg_raw = e.get("AUTODEPLOY_CONFIG_PATH", "config/deployment_types.yaml")

    ssh_user = e.get("SSH_USER", "connecteve").strip() or "connecteve"
    raw_work_dir = e.get("AUTODEPLOY_WORK_DIR", "~/gateway-infra-next")
    work_dir = _expand_remote_home(raw_work_dir, ssh_user)

    return Settings(
        slack_bot_token=required("SLACK_BOT_TOKEN"),
        slack_app_token=required("SLACK_APP_TOKEN"),
        slack_channel_id=required("SLACK_CHANNEL_ID"),
        allowed_users=allowed,
        ssh_user=ssh_user,
        ssh_password=required("SSH_PASSWORD"),
        bitbucket_user=e.get("BITBUCKET_USER", "youngwoochon").strip() or "youngwoochon",
        bitbucket_app_password=required("BITBUCKET_APP_PASSWORD"),
        db_path=resolve_db_path(e),
        config_path=Path(cfg_raw).expanduser(),
        repo_host_path=e.get(
            "AUTODEPLOY_REPO_HOST_PATH",
            "bitbucket.org/connecteve-workspace/gateway-infra-next.git",
        ),
        repo_branch=e.get("AUTODEPLOY_REPO_BRANCH", "dev"),
        work_dir=work_dir,
        log_level=e.get("LOG_LEVEL", "INFO"),
        site_admin_email=e.get("SITE_ADMIN_EMAIL", "").strip(),
        site_admin_password=e.get("SITE_ADMIN_PASSWORD", ""),
        site_cloud_base_url=e.get(
            "SITE_CLOUD_BASE_URL", "https://dev-gateway.connecteve.com"
        ).strip().rstrip("/"),
        site_api_env=e.get("SITE_API_ENV", "dev").strip() or "dev",
        jira_base_url=e.get(
            "JIRA_BASE_URL", "https://connecteve.atlassian.net"
        ).strip().rstrip("/"),
        jira_email=e.get("JIRA_EMAIL", "").strip(),
        jira_api_token=e.get("JIRA_API_TOKEN", ""),
        jira_key=e.get("JIRA_KEY", "PMFM").strip() or "PMFM",
        port_frontend=int(e.get("PORT_FRONTEND", "8000")),
        port_temporal=int(e.get("PORT_TEMPORAL", "8001")),
        port_webpacs=int(e.get("PORT_WEBPACS", "8002")),
        hubctl_repo_path=resolve_hubctl_repo(e),
        become_password=resolve_become_password(e),
        web=resolve_web_config(e),
    )
