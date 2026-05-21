from __future__ import annotations

import aiosqlite
import pytest

from autodeploy.db import connect


@pytest.mark.asyncio
async def test_init_creates_all_three_tables(temp_db):
    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
            names = [r["name"] for r in rows]
    assert "jobs" in names
    assert "job_events" in names
    assert "script_logs" in names


@pytest.mark.asyncio
async def test_insert_job_minimal_required_fields(temp_db):
    async with connect(temp_db) as db:
        await db.execute(
            """INSERT INTO jobs
               (target_ip, deployment_type, hospital_code, status, started_by, slack_channel)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("192.168.1.50", "hybrid-with-ai", "HOSP01", "queued", "U01ABC", "C0CHAN"),
        )
        await db.commit()
        async with db.execute("SELECT * FROM jobs") as cur:
            row = await cur.fetchone()
    assert row["target_ip"] == "192.168.1.50"
    assert row["deployment_type"] == "hybrid-with-ai"
    assert row["hospital_code"] == "HOSP01"
    assert row["status"] == "queued"


@pytest.mark.asyncio
async def test_invalid_status_rejected_by_check_constraint(temp_db):
    async with connect(temp_db) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                """INSERT INTO jobs
                   (target_ip, deployment_type, hospital_code, status, started_by, slack_channel)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("1.2.3.4", "on-premise", "HOSP02", "bogus", "U", "C"),
            )
            await db.commit()


@pytest.mark.asyncio
async def test_job_event_cascade_delete(temp_db):
    async with connect(temp_db) as db:
        cur = await db.execute(
            """INSERT INTO jobs
               (target_ip, deployment_type, hospital_code, status, started_by, slack_channel)
               VALUES ('1.2.3.4', 'on-premise', 'H', 'queued', 'U', 'C')"""
        )
        job_id = cur.lastrowid
        await db.execute(
            "INSERT INTO job_events (job_id, step, level, message) VALUES (?, 'ssh_connect', 'info', 'started')",
            (job_id,),
        )
        await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await db.commit()
        async with db.execute("SELECT COUNT(*) AS n FROM job_events") as cur2:
            row = await cur2.fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_init_db_is_idempotent(temp_db):
    # 두 번째 init 호출이 에러 없이 통과
    from autodeploy.db import init_db
    await init_db(temp_db)
    async with connect(temp_db) as db:
        async with db.execute("SELECT COUNT(*) AS n FROM jobs") as cur:
            row = await cur.fetchone()
    assert row["n"] == 0
