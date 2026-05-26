"""Job/JobEvent/ScriptLog CRUD. workflow가 직접 SQL 만지지 않게 격리."""
from __future__ import annotations

import aiosqlite

from autodeploy.models import Job, JobStatus, Step


async def create_job(db: aiosqlite.Connection, job: Job) -> int:
    cur = await db.execute(
        """INSERT INTO jobs
           (target_ip, deployment_type, hospital_code, hospital_name, hospital_address,
            status, started_by, slack_channel, slack_thread_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.target_ip,
            job.deployment_type,
            job.hospital_code,
            job.hospital_name,
            job.hospital_address,
            job.status.value,
            job.started_by,
            job.slack_channel,
            job.slack_thread_ts,
        ),
    )
    await db.commit()
    return int(cur.lastrowid)


async def mark_running(db: aiosqlite.Connection, job_id: int) -> None:
    await db.execute(
        "UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=?",
        (job_id,),
    )
    await db.commit()


async def update_current_step(db: aiosqlite.Connection, job_id: int, step: Step) -> None:
    await db.execute("UPDATE jobs SET current_step=? WHERE id=?", (step.value, job_id))
    await db.commit()


async def update_commit_sha(db: aiosqlite.Connection, job_id: int, sha: str) -> None:
    await db.execute("UPDATE jobs SET script_commit_sha=? WHERE id=?", (sha, job_id))
    await db.commit()


async def update_thread_ts(db: aiosqlite.Connection, job_id: int, thread_ts: str) -> None:
    await db.execute("UPDATE jobs SET slack_thread_ts=? WHERE id=?", (thread_ts, job_id))
    await db.commit()


async def finish_job(
    db: aiosqlite.Connection,
    job_id: int,
    status: JobStatus,
    *,
    admin_web_url: str | None = None,
    error_message: str | None = None,
) -> None:
    await db.execute(
        """UPDATE jobs SET
              status = ?,
              finished_at = CURRENT_TIMESTAMP,
              admin_web_url = COALESCE(?, admin_web_url),
              error_message = COALESCE(?, error_message)
           WHERE id = ?""",
        (status.value, admin_web_url, error_message, job_id),
    )
    await db.commit()


async def add_event(
    db: aiosqlite.Connection,
    job_id: int,
    step: str,
    level: str,
    message: str,
) -> None:
    await db.execute(
        "INSERT INTO job_events (job_id, step, level, message) VALUES (?, ?, ?, ?)",
        (job_id, step, level, message),
    )
    await db.commit()


async def add_script_log(
    db: aiosqlite.Connection,
    job_id: int,
    step: str,
    stream: str,
    line: str,
) -> None:
    await db.execute(
        "INSERT INTO script_logs (job_id, step, stream, line) VALUES (?, ?, ?, ?)",
        (job_id, step, stream, line),
    )
    await db.commit()


async def get_job(db: aiosqlite.Connection, job_id: int) -> Job | None:
    async with db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_job(row) if row else None


async def list_recent_jobs(db: aiosqlite.Connection, limit: int = 10) -> list[Job]:
    async with db.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_job(r) for r in rows]


async def find_active_by_ip(db: aiosqlite.Connection, target_ip: str) -> list[Job]:
    async with db.execute(
        "SELECT * FROM jobs WHERE target_ip=? AND status IN ('queued','running') ORDER BY id DESC",
        (target_ip,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_job(r) for r in rows]


async def find_jobs_by_thread_ts(
    db: aiosqlite.Connection, thread_ts: str
) -> list[Job]:
    """같은 슬랙 스레드에 묶인 작업들 (재시도 체인). 가장 최근부터 반환."""
    async with db.execute(
        "SELECT * FROM jobs WHERE slack_thread_ts=? ORDER BY id DESC",
        (thread_ts,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_job(r) for r in rows]


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        id=row["id"],
        target_ip=row["target_ip"],
        deployment_type=row["deployment_type"],
        hospital_code=row["hospital_code"],
        started_by=row["started_by"],
        slack_channel=row["slack_channel"],
        hospital_name=row["hospital_name"],
        hospital_address=row["hospital_address"],
        status=JobStatus(row["status"]),
        current_step=Step(row["current_step"]) if row["current_step"] else None,
        slack_thread_ts=row["slack_thread_ts"],
        admin_web_url=row["admin_web_url"],
        script_commit_sha=row["script_commit_sha"],
        error_message=row["error_message"],
    )
