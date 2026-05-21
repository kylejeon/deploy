from __future__ import annotations

from pathlib import Path

import pytest

from autodeploy.settings import SettingsError, load_settings


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
    assert s.work_dir == "~/gateway-infra-next"
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
