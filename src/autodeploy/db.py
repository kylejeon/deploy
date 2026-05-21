"""SQLite 부트스트랩 + 연결 헬퍼."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


async def init_db(path: str | Path) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


@asynccontextmanager
async def connect(path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    path = Path(path).expanduser()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
