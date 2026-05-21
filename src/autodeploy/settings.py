"""env 기반 런타임 설정 로더. 시크릿은 env에서만 읽고 코드/문서엔 자리표시자."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class SettingsError(RuntimeError):
    pass


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


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = env if env is not None else os.environ

    def required(key: str) -> str:
        v = e.get(key, "").strip()
        if not v:
            raise SettingsError(f"missing required env var: {key}")
        return v

    allowed_raw = e.get("AUTODEPLOY_ALLOWED_USERS", "")
    allowed = frozenset(u.strip() for u in allowed_raw.split(",") if u.strip())

    db_raw = e.get("AUTODEPLOY_DB_PATH", "~/Library/Application Support/autodeploy/state.db")
    cfg_raw = e.get("AUTODEPLOY_CONFIG_PATH", "config/deployment_types.yaml")

    return Settings(
        slack_bot_token=required("SLACK_BOT_TOKEN"),
        slack_app_token=required("SLACK_APP_TOKEN"),
        slack_channel_id=required("SLACK_CHANNEL_ID"),
        allowed_users=allowed,
        ssh_user=e.get("SSH_USER", "connecteve").strip() or "connecteve",
        ssh_password=required("SSH_PASSWORD"),
        bitbucket_user=e.get("BITBUCKET_USER", "youngwoochon").strip() or "youngwoochon",
        bitbucket_app_password=required("BITBUCKET_APP_PASSWORD"),
        db_path=Path(db_raw).expanduser(),
        config_path=Path(cfg_raw).expanduser(),
        repo_host_path=e.get(
            "AUTODEPLOY_REPO_HOST_PATH",
            "bitbucket.org/connecteve-workspace/gateway-infra-next.git",
        ),
        repo_branch=e.get("AUTODEPLOY_REPO_BRANCH", "dev"),
        work_dir=e.get("AUTODEPLOY_WORK_DIR", "~/gateway-infra-next"),
        log_level=e.get("LOG_LEVEL", "INFO"),
    )
