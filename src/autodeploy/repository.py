"""Job/JobEvent/ScriptLog CRUD. workflow가 직접 SQL 만지지 않게 격리."""
from __future__ import annotations

from collections.abc import Sequence

import aiosqlite

from autodeploy.models import Job, JobStatus, Step


async def create_job(db: aiosqlite.Connection, job: Job) -> int:
    cur = await db.execute(
        """INSERT INTO jobs
           (target_ip, target_port, deployment_type, hospital_code,
            hospital_name, hospital_address,
            status, started_by, slack_channel, slack_thread_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.target_ip,
            job.target_port,
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
    *,
    host: str | None = None,
    kind: str | None = None,
) -> int:
    """로그 한 줄 적재. 돌려주는 id 가 SSE 재연결(`?after=`)의 커서다."""
    cur = await db.execute(
        "INSERT INTO script_logs (job_id, step, stream, line, host, kind)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, step, stream, line, host, kind),
    )
    await db.commit()
    return int(cur.lastrowid)


async def add_script_logs(
    db: aiosqlite.Connection,
    rows: Sequence[tuple[int, str, str, str, str | None, str | None]],
) -> None:
    """(job_id, step, stream, line, host, kind) 묶음 적재.

    hubctl 은 초당 수십 줄을 쏟아내는데 줄마다 commit 하면 SQLite 가 매번 fsync 한다.
    호출부에서 짧게 모아 한 번에 넣는다.
    """
    if not rows:
        return
    await db.executemany(
        "INSERT INTO script_logs (job_id, step, stream, line, host, kind)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
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


async def find_active_jobs(
    db: aiosqlite.Connection, limit: int = 10
) -> list[Job]:
    """진행 중(queued/running) 작업들. 가장 최근부터. `status` 명령 무인자 분기에 사용."""
    async with db.execute(
        "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT ?",
        (limit,),
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
        target_port=row["target_port"],
        status=JobStatus(row["status"]),
        current_step=Step(row["current_step"]) if row["current_step"] else None,
        slack_thread_ts=row["slack_thread_ts"],
        admin_web_url=row["admin_web_url"],
        script_commit_sha=row["script_commit_sha"],
        error_message=row["error_message"],
    )


# ── v2: server_meta (sites.yml 에 없는 웹 전용 부가정보) ────────────────

async def get_server_meta(db: aiosqlite.Connection) -> dict[str, dict[str, str | None]]:
    """host -> {"memo": ..., "key_installed_at": ...}"""
    async with db.execute("SELECT host, memo, key_installed_at FROM server_meta") as cur:
        rows = await cur.fetchall()
    return {
        r["host"]: {"memo": r["memo"], "key_installed_at": r["key_installed_at"]}
        for r in rows
    }


async def set_server_memo(db: aiosqlite.Connection, host: str, memo: str | None) -> None:
    await db.execute(
        """INSERT INTO server_meta (host, memo, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(host) DO UPDATE SET memo=excluded.memo, updated_at=CURRENT_TIMESTAMP""",
        (host, memo),
    )
    await db.commit()


async def mark_key_installed(db: aiosqlite.Connection, host: str) -> None:
    """F9 성공 시각 기록. 설치 시작 전 게이트가 이 값을 본다."""
    await db.execute(
        """INSERT INTO server_meta (host, key_installed_at, updated_at)
           VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT(host) DO UPDATE SET
             key_installed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP""",
        (host,),
    )
    await db.commit()


async def clear_key_installed(db: aiosqlite.Connection, host: str) -> None:
    """초기화(uninstall) 등으로 키 상태를 신뢰할 수 없게 됐을 때."""
    await db.execute(
        "UPDATE server_meta SET key_installed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE host=?",
        (host,),
    )
    await db.commit()


async def delete_server_meta(db: aiosqlite.Connection, host: str) -> None:
    await db.execute("DELETE FROM server_meta WHERE host=?", (host,))
    await db.commit()


# ── v2: 웹 콘솔용 조회 ──────────────────────────────────────────────
#
# v1 의 get_job/list_recent_jobs 는 Job 데이터클래스(구 SSH 워크플로 모양)를
# 돌려주고 Slack 봇이 쓰고 있다. 웹은 kind/env/ref/job_hosts 가 필요해 모양이
# 다르므로 건드리지 않고 따로 둔다 (D1: Slack 경로 불변).

_JOB_COLUMNS = (
    "id", "kind", "status", "env", "ref", "ref_type", "clean_mode",
    "exit_code", "cancel_by", "current_step", "started_by",
    "slack_channel", "slack_thread_ts", "error_message",
    "created_at", "started_at", "finished_at",
)

ACTIVE_STATUSES = ("queued", "running", "awaiting")


def _job_dict(row: aiosqlite.Row) -> dict:
    return {name: row[name] for name in _JOB_COLUMNS}


async def list_jobs(db: aiosqlite.Connection, *, limit: int = 50) -> list[dict]:
    """최근 작업 목록. 호스트는 job_hosts 에서 한 번에 끌어와 N+1 을 피한다."""
    async with db.execute(
        f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    jobs = [_job_dict(r) for r in rows]
    if not jobs:
        return []

    placeholders = ",".join("?" * len(jobs))
    async with db.execute(
        f"SELECT job_id, host, status FROM job_hosts WHERE job_id IN ({placeholders})"
        " ORDER BY host",
        tuple(j["id"] for j in jobs),
    ) as cur:
        host_rows = await cur.fetchall()

    by_job: dict[int, list[dict]] = {}
    for r in host_rows:
        by_job.setdefault(r["job_id"], []).append({"host": r["host"], "status": r["status"]})
    for job in jobs:
        job["hosts"] = by_job.get(job["id"], [])
    return jobs


async def get_job_detail(db: aiosqlite.Connection, job_id: int) -> dict | None:
    async with db.execute(
        f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE id=?", (job_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    job = _job_dict(row)
    job["hosts"] = await get_job_hosts(db, job_id)
    job["events"] = await get_job_events(db, job_id)
    return job


async def get_job_hosts(db: aiosqlite.Connection, job_id: int) -> list[dict]:
    async with db.execute(
        "SELECT host, status, recap_ok, recap_changed, recap_failed, recap_unreachable"
        " FROM job_hosts WHERE job_id=? ORDER BY host",
        (job_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "host": r["host"],
            "status": r["status"],
            "recap": None
            if r["recap_ok"] is None
            else {
                "ok": r["recap_ok"],
                "changed": r["recap_changed"],
                "failed": r["recap_failed"],
                "unreachable": r["recap_unreachable"],
            },
        }
        for r in rows
    ]


async def get_job_events(db: aiosqlite.Connection, job_id: int) -> list[dict]:
    async with db.execute(
        "SELECT step, level, message, created_at FROM job_events"
        " WHERE job_id=? ORDER BY id",
        (job_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "step": r["step"],
            "level": r["level"],
            "message": r["message"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def get_script_logs(
    db: aiosqlite.Connection,
    job_id: int,
    *,
    after_id: int = 0,
    limit: int = 2000,
) -> list[dict]:
    """`after_id` 이후 로그 줄. SSE 재연결 커서(§F4)와 전체 로그 조회에 함께 쓴다."""
    async with db.execute(
        "SELECT id, step, stream, line, host, kind, created_at FROM script_logs"
        " WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
        (job_id, after_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "id": r["id"],
            "step": r["step"],
            "stream": r["stream"],
            "line": r["line"],
            "host": r["host"],
            "kind": r["kind"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def count_active_jobs(db: aiosqlite.Connection) -> int:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({placeholders})",
        ACTIVE_STATUSES,
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"])


async def reap_stale_jobs(db: aiosqlite.Connection, *, reason: str) -> list[int]:
    """기동 시 남아있는 running/awaiting 작업을 실패로 정리한다 (§9).

    데몬이 죽으면 그 작업을 감독하던 러너도 함께 사라진다. 상태를 그대로 두면
    영원히 '진행 중'으로 보이고, 큐가 이미 하나 돌고 있다고 오해해 새 작업이
    시작되지 않는다. queued 는 아직 시작 전이라 건드리지 않는다.
    """
    async with db.execute(
        "SELECT id FROM jobs WHERE status IN ('running','awaiting')"
    ) as cur:
        ids = [int(r["id"]) for r in await cur.fetchall()]
    if not ids:
        return []

    await db.execute(
        "UPDATE jobs SET status='failed', finished_at=CURRENT_TIMESTAMP,"
        " error_message=COALESCE(error_message, ?)"
        " WHERE status IN ('running','awaiting')",
        (reason,),
    )
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE job_hosts SET status='failed'"
        f" WHERE job_id IN ({placeholders}) AND status IN ('queued','running')",
        ids,
    )
    await db.executemany(
        "INSERT INTO job_events (job_id, step, level, message) VALUES (?, ?, ?, ?)",
        [(job_id, "daemon", "error", reason) for job_id in ids],
    )
    await db.commit()
    return ids
