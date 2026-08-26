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
