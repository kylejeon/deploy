"""계정/비밀번호 (dev-spec-web-console §F1)."""
from __future__ import annotations

import pytest

from autodeploy.accounts import (
    AccountError,
    authenticate,
    create_user,
    delete_user,
    get_user,
    hash_password,
    list_users,
    set_password,
    verify_password,
)
from autodeploy.db import connect


@pytest.mark.asyncio
async def test_hash_is_salted_and_verifies():
    salt1, h1 = hash_password("correct-horse")
    salt2, h2 = hash_password("correct-horse")
    assert salt1 != salt2, "매번 다른 솔트"
    assert h1 != h2, "같은 비밀번호라도 해시가 달라야 한다"
    assert verify_password("correct-horse", salt1, h1)
    assert not verify_password("wrong", salt1, h1)


def test_short_password_rejected():
    with pytest.raises(AccountError, match="최소 8자"):
        hash_password("short")


@pytest.mark.asyncio
async def test_create_and_authenticate(temp_db):
    async with connect(temp_db) as db:
        uid = await create_user(db, "yonghyuk", "prototype-pw")
        assert uid > 0
        user = await authenticate(db, "yonghyuk", "prototype-pw")
        assert user is not None and user.username == "yonghyuk"


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_user_both_none(temp_db):
    async with connect(temp_db) as db:
        await create_user(db, "yonghyuk", "prototype-pw")
        assert await authenticate(db, "yonghyuk", "nope-nope") is None
        assert await authenticate(db, "ghost", "prototype-pw") is None


@pytest.mark.asyncio
async def test_plaintext_never_stored(temp_db):
    async with connect(temp_db) as db:
        await create_user(db, "yonghyuk", "prototype-pw")
        async with db.execute("SELECT pw_hash, pw_salt FROM users") as cur:
            row = await cur.fetchone()
    assert b"prototype-pw" not in bytes(row["pw_hash"])
    assert b"prototype-pw" not in bytes(row["pw_salt"])


@pytest.mark.asyncio
async def test_duplicate_username_rejected(temp_db):
    async with connect(temp_db) as db:
        await create_user(db, "yonghyuk", "prototype-pw")
        with pytest.raises(AccountError, match="이미 있는"):
            await create_user(db, "yonghyuk", "other-password")


@pytest.mark.parametrize("bad", ["a", "Yonghyuk", "-lead", "has space", "x" * 33, "한글"])
@pytest.mark.asyncio
async def test_invalid_usernames(temp_db, bad):
    async with connect(temp_db) as db:
        with pytest.raises(AccountError, match="아이디"):
            await create_user(db, bad, "prototype-pw")


@pytest.mark.asyncio
async def test_set_password_changes_login_and_kills_sessions(temp_db):
    async with connect(temp_db) as db:
        uid = await create_user(db, "yonghyuk", "prototype-pw")
        await db.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('t', ?, '2099-01-01')",
            (uid,),
        )
        await db.commit()

        await set_password(db, "yonghyuk", "brand-new-pw")

        assert await authenticate(db, "yonghyuk", "prototype-pw") is None
        assert await authenticate(db, "yonghyuk", "brand-new-pw") is not None
        async with db.execute("SELECT COUNT(*) AS n FROM sessions") as cur:
            assert (await cur.fetchone())["n"] == 0, "비밀번호 변경 시 세션 무효화"


@pytest.mark.asyncio
async def test_delete_user_and_list(temp_db):
    async with connect(temp_db) as db:
        await create_user(db, "yonghyuk", "prototype-pw")
        await create_user(db, "sujin", "prototype-pw")
        assert [u.username for u in await list_users(db)] == ["sujin", "yonghyuk"]

        await delete_user(db, "sujin")
        assert [u.username for u in await list_users(db)] == ["yonghyuk"]
        assert await get_user(db, "sujin") is None

        with pytest.raises(AccountError, match="없는 아이디"):
            await delete_user(db, "sujin")


@pytest.mark.asyncio
async def test_disabled_user_cannot_login(temp_db):
    async with connect(temp_db) as db:
        await create_user(db, "yonghyuk", "prototype-pw")
        await db.execute("UPDATE users SET disabled_at='2026-08-26' WHERE username='yonghyuk'")
        await db.commit()
        assert await authenticate(db, "yonghyuk", "prototype-pw") is None


@pytest.mark.asyncio
async def test_last_login_recorded(temp_db):
    async with connect(temp_db) as db:
        await create_user(db, "yonghyuk", "prototype-pw")
        assert (await get_user(db, "yonghyuk")).last_login_at is None
        await authenticate(db, "yonghyuk", "prototype-pw")
        assert (await get_user(db, "yonghyuk")).last_login_at is not None


@pytest.mark.asyncio
async def test_deleting_user_cascades_sessions(temp_db):
    async with connect(temp_db) as db:
        uid = await create_user(db, "yonghyuk", "prototype-pw")
        await db.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('t', ?, '2099-01-01')",
            (uid,),
        )
        await db.commit()
        await delete_user(db, "yonghyuk")
        async with db.execute("SELECT COUNT(*) AS n FROM sessions") as cur:
            assert (await cur.fetchone())["n"] == 0
