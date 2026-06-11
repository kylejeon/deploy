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
        await _migrate(db)
        await db.commit()


async def _migrate(db: aiosqlite.Connection) -> None:
    """기존 DB에 없는 컬럼을 추가 (멱등). schema.sql의 CREATE IF NOT EXISTS는
    이미 있는 테이블엔 손대지 않아서, 컬럼 추가는 별도 처리해야 함."""
    cur = await db.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in await cur.fetchall()}
    if "target_port" not in cols:
        await db.execute("ALTER TABLE jobs ADD COLUMN target_port INTEGER NOT NULL DEFAULT 22")


@asynccontextmanager
async def connect(path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    path = Path(path).expanduser()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
