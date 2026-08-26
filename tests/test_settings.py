from __future__ import annotations

from pathlib import Path

import pytest

from autodeploy.settings import (
    DEFAULT_HUBCTL_REPO_PATH,
    SettingsError,
    load_settings,
    resolve_become_password,
    resolve_hubctl_repo,
)


def _full_env() -> dict[str, str]:
    return {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_APP_TOKEN": "xapp-test",
        "SLACK_CHANNEL_ID": "C123ABC",
        "AUTODEPLOY_ALLOWED_USERS": "U01,U02, U03 ",
        "SSH_PASSWORD": "pw",
        "BITBUCKET_APP_PASSWORD": "ATBB...",
    }


def test_loads_minimum_required_env():
    s = load_settings(_full_env())
    assert s.slack_bot_token == "xoxb-test"
    assert s.slack_app_token == "xapp-test"
    assert s.slack_channel_id == "C123ABC"
    assert s.allowed_users == frozenset({"U01", "U02", "U03"})
    assert s.bitbucket_user == "youngwoochon"  # default
    assert s.repo_branch == "dev"
    # work_dir의 ~는 원격 ssh_user 기준 절대경로로 확장됨
    assert s.work_dir == "/home/connecteve/gateway-infra-next"
    assert s.log_level == "INFO"


def test_default_db_path_expands_tilde():
    s = load_settings(_full_env())
    assert isinstance(s.db_path, Path)
    assert str(s.db_path).startswith(str(Path.home()))


def test_allowed_users_can_be_empty():
    env = _full_env()
    env["AUTODEPLOY_ALLOWED_USERS"] = ""
    s = load_settings(env)
    assert s.allowed_users == frozenset()


def test_missing_required_token_raises():
    env = _full_env()
    del env["SLACK_BOT_TOKEN"]
    with pytest.raises(SettingsError, match="SLACK_BOT_TOKEN"):
        load_settings(env)


def test_empty_required_token_raises():
    env = _full_env()
    env["SLACK_BOT_TOKEN"] = "  "
    with pytest.raises(SettingsError, match="SLACK_BOT_TOKEN"):
        load_settings(env)


def test_bitbucket_user_override():
    env = _full_env()
    env["BITBUCKET_USER"] = "altuser"
    s = load_settings(env)
    assert s.bitbucket_user == "altuser"


def test_branch_override():
    env = _full_env()
    env["AUTODEPLOY_REPO_BRANCH"] = "main"
    s = load_settings(env)
    assert s.repo_branch == "main"


def test_work_dir_tilde_expanded_to_remote_home():
    """~/foo는 ssh_user 기준 /home/<user>/foo로 치환 (셸 quoting 안전성)."""
    env = _full_env()
    env["AUTODEPLOY_WORK_DIR"] = "~/custom-path"
    env["SSH_USER"] = "someuser"
    s = load_settings(env)
    assert s.work_dir == "/home/someuser/custom-path"


def test_work_dir_bare_tilde_becomes_home():
    env = _full_env()
    env["AUTODEPLOY_WORK_DIR"] = "~"
    s = load_settings(env)
    assert s.work_dir == "/home/connecteve"


def test_work_dir_absolute_path_unchanged():
    env = _full_env()
    env["AUTODEPLOY_WORK_DIR"] = "/opt/custom/path"
    s = load_settings(env)
    assert s.work_dir == "/opt/custom/path"


def test_site_admin_credentials_default_empty():
    """site_admin_email/password는 미설정 시 빈 문자열 — workflow에서 skip 조건으로 사용."""
    s = load_settings(_full_env())
    assert s.site_admin_email == ""
    assert s.site_admin_password == ""


def test_site_admin_credentials_load_from_env():
    env = _full_env()
    env["SITE_ADMIN_EMAIL"] = "admin@x.com"
    env["SITE_ADMIN_PASSWORD"] = "secret#$"
    s = load_settings(env)
    assert s.site_admin_email == "admin@x.com"
    assert s.site_admin_password == "secret#$"


def test_jira_settings_default_values():
    """Jira env 미설정 시 기본값 확인."""
    s = load_settings(_full_env())
    assert s.jira_base_url == "https://connecteve.atlassian.net"
    assert s.jira_email == ""
    assert s.jira_api_token == ""
    assert s.jira_key == "PMFM"


def test_jira_settings_loaded_from_env():
    env = _full_env()
    env["JIRA_BASE_URL"] = "https://custom.atlassian.net"
    env["JIRA_EMAIL"] = "jira@x.com"
    env["JIRA_API_TOKEN"] = "MYTOKEN"
    env["JIRA_KEY"] = "MYPROJECT"
    s = load_settings(env)
    assert s.jira_base_url == "https://custom.atlassian.net"
    assert s.jira_email == "jira@x.com"
    assert s.jira_api_token == "MYTOKEN"
    assert s.jira_key == "MYPROJECT"


# ── v2: hubctl (웹 콘솔) ────────────────────────────────────────────


def test_hubctl_repo_defaults_to_home():
    assert resolve_hubctl_repo({}) == Path(DEFAULT_HUBCTL_REPO_PATH).expanduser()


def test_hubctl_repo_from_env_expands_tilde():
    assert resolve_hubctl_repo({"HUBCTL_REPO_PATH": "~/other-repo"}) == (
        Path("~/other-repo").expanduser()
    )


def test_hubctl_repo_blank_falls_back_to_default():
    assert resolve_hubctl_repo({"HUBCTL_REPO_PATH": "   "}) == (
        Path(DEFAULT_HUBCTL_REPO_PATH).expanduser()
    )


def test_become_password_falls_back_to_ssh_password():
    """타겟은 connecteve 계정 공통이라 sudo 비밀번호 = SSH 비밀번호다."""
    assert resolve_become_password({"SSH_PASSWORD": "shared-pw"}) == "shared-pw"


def test_become_password_overrides_ssh_password():
    env = {"SSH_PASSWORD": "shared-pw", "BECOME_PASSWORD": "sudo-only-pw"}
    assert resolve_become_password(env) == "sudo-only-pw"


def test_become_password_absent_is_empty():
    """빈 값이면 hubctl 러너가 비밀번호 파일 자체를 만들지 않는다."""
    assert resolve_become_password({}) == ""


def test_load_settings_includes_hubctl_values():
    env = _full_env()
    env["HUBCTL_REPO_PATH"] = "~/hp"
    s = load_settings(env)
    assert s.hubctl_repo_path == Path("~/hp").expanduser()
    assert s.become_password == s.ssh_password
