"""server_meta — 웹 전용 부가정보 (메모 / SSH 키 등록 시각)."""
from __future__ import annotations

import pytest

from autodeploy import repository as repo
from autodeploy.db import connect


@pytest.mark.asyncio
async def test_memo_upsert(temp_db):
    async with connect(temp_db) as db:
        await repo.set_server_memo(db, "yonseiwa", "연세와병원")
        assert (await repo.get_server_meta(db))["yonseiwa"]["memo"] == "연세와병원"

        await repo.set_server_memo(db, "yonseiwa", "연세와병원 (본관)")
        meta = await repo.get_server_meta(db)
        assert len(meta) == 1, "같은 호스트는 갱신"
        assert meta["yonseiwa"]["memo"] == "연세와병원 (본관)"


@pytest.mark.asyncio
async def test_key_installed_lifecycle(temp_db):
    async with connect(temp_db) as db:
        assert await repo.get_server_meta(db) == {}

        await repo.mark_key_installed(db, "yonseiwa")
        assert (await repo.get_server_meta(db))["yonseiwa"]["key_installed_at"] is not None

        await repo.clear_key_installed(db, "yonseiwa")
        assert (await repo.get_server_meta(db))["yonseiwa"]["key_installed_at"] is None


@pytest.mark.asyncio
async def test_memo_and_key_coexist(temp_db):
    async with connect(temp_db) as db:
        await repo.set_server_memo(db, "yonseiwa", "연세와병원")
        await repo.mark_key_installed(db, "yonseiwa")
        row = (await repo.get_server_meta(db))["yonseiwa"]
    assert row["memo"] == "연세와병원"
    assert row["key_installed_at"] is not None


@pytest.mark.asyncio
async def test_delete_server_meta(temp_db):
    async with connect(temp_db) as db:
        await repo.mark_key_installed(db, "yonseiwa")
        await repo.delete_server_meta(db, "yonseiwa")
        assert await repo.get_server_meta(db) == {}


@pytest.mark.asyncio
async def test_serial_upsert_does_not_touch_the_memo(temp_db):
    """시리얼은 기계에서 읽어온 값이고 메모는 사람이 쓴 글이다. 서로 덮지 않는다."""
    async with connect(temp_db) as db:
        await repo.set_server_memo(db, "yonseiwa", "연세와병원")
        await repo.set_server_serial(db, "yonseiwa", "PF3ABCDE")

        row = (await repo.get_server_meta(db))["yonseiwa"]
        assert row["serial"] == "PF3ABCDE"
        assert row["memo"] == "연세와병원"

        # 본체를 바꾸면 다시 읽는다 — 겹쳐 쌓이지 않고 교체된다.
        await repo.set_server_serial(db, "yonseiwa", "PF9ZZZZZ")
        meta = await repo.get_server_meta(db)
        assert len(meta) == 1
        assert meta["yonseiwa"]["serial"] == "PF9ZZZZZ"


@pytest.mark.asyncio
async def test_serial_starts_empty(temp_db):
    """읽기 전에는 비어 있다. 지어내지 않는다."""
    async with connect(temp_db) as db:
        await repo.mark_key_installed(db, "yonseiwa")
        assert (await repo.get_server_meta(db))["yonseiwa"]["serial"] is None
