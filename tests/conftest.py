"""공용 pytest 픽스처."""
from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from autodeploy.db import init_db


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    await init_db(db_path)
    return db_path
