"""계정 관리 CLI (dev-spec-web-console §F1)."""
from __future__ import annotations

import pytest

from autodeploy import cli
from autodeploy.accounts import authenticate, get_user
from autodeploy.db import connect


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """CLI 가 쓰는 DB 를 임시 경로로 돌린다 (load_dotenv 는 기존 env 를 덮지 않는다)."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("AUTODEPLOY_DB_PATH", str(db))
    return db


def _prompt(value: str):
    return lambda _msg: value


def test_help_and_unknown_command(capsys):
    assert cli.main([]) == 0
    assert cli.main(["--help"]) == 0
    assert cli.main(["bogus"]) == 2
    assert "알 수 없는 명령" in capsys.readouterr().err


def test_adduser_then_authenticate(cli_env, capsys):
    assert cli.main(["adduser", "yonghyuk"], prompt=_prompt("prototype-pw")) == 0
    assert "계정을 만들었습니다" in capsys.readouterr().out

    async def check():
        async with connect(cli_env) as db:
            return await authenticate(db, "yonghyuk", "prototype-pw")

    import asyncio

    assert asyncio.run(check()) is not None


def test_adduser_mismatched_confirmation(cli_env, capsys):
    seq = iter(["first-password", "second-password"])
    assert cli.main(["adduser", "yonghyuk"], prompt=lambda _m: next(seq)) == 1
    assert "다릅니다" in capsys.readouterr().err


def test_adduser_short_password(cli_env, capsys):
    assert cli.main(["adduser", "yonghyuk"], prompt=_prompt("short")) == 1
    assert "최소 8자" in capsys.readouterr().err


def test_adduser_duplicate(cli_env, capsys):
    cli.main(["adduser", "yonghyuk"], prompt=_prompt("prototype-pw"))
    capsys.readouterr()
    assert cli.main(["adduser", "yonghyuk"], prompt=_prompt("prototype-pw")) == 1
    assert "이미 있는" in capsys.readouterr().err


def test_missing_username_arg(cli_env, capsys):
    assert cli.main(["adduser"]) == 2
    assert "아이디가 필요합니다" in capsys.readouterr().err


def test_users_listing(cli_env, capsys):
    assert cli.main(["users"]) == 0
    assert "등록된 계정이 없습니다" in capsys.readouterr().out

    cli.main(["adduser", "yonghyuk"], prompt=_prompt("prototype-pw"))
    cli.main(["adduser", "sujin"], prompt=_prompt("prototype-pw"))
    capsys.readouterr()

    assert cli.main(["users"]) == 0
    out = capsys.readouterr().out
    assert "yonghyuk" in out and "sujin" in out


def test_passwd_changes_password(cli_env, capsys):
    cli.main(["adduser", "yonghyuk"], prompt=_prompt("prototype-pw"))
    assert cli.main(["passwd", "yonghyuk"], prompt=_prompt("brand-new-pw")) == 0

    import asyncio

    async def check():
        async with connect(cli_env) as db:
            return (
                await authenticate(db, "yonghyuk", "prototype-pw"),
                await authenticate(db, "yonghyuk", "brand-new-pw"),
            )

    old, new = asyncio.run(check())
    assert old is None and new is not None


def test_passwd_unknown_user(cli_env, capsys):
    assert cli.main(["passwd", "ghost"], prompt=_prompt("prototype-pw")) == 1
    assert "없는 아이디" in capsys.readouterr().err


def test_deluser(cli_env, capsys):
    cli.main(["adduser", "yonghyuk"], prompt=_prompt("prototype-pw"))
    assert cli.main(["deluser", "yonghyuk"]) == 0

    import asyncio

    async def check():
        async with connect(cli_env) as db:
            return await get_user(db, "yonghyuk")

    assert asyncio.run(check()) is None


def test_cli_creates_db_when_missing(cli_env):
    assert not cli_env.exists()
    cli.main(["users"])
    assert cli_env.exists(), "CLI 가 DB 를 부트스트랩해야 한다"
