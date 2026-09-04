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


async def reclassify_script_logs(
    db: aiosqlite.Connection, *, job_id: int | None = None
) -> dict[int, int]:
    """저장된 로그 줄의 `kind` 를 현재 파서로 다시 매긴다. {작업번호: 바뀐 줄 수}.

    `kind` 는 줄 내용에서만 유도되는 표시용 값이라 다시 계산해도 안전하고,
    같은 코드로 두 번 돌려도 결과가 같다. `stream`(stdout/stderr)은 건드리지 않는다 —
    그쪽은 Slack 경로가 쓰는 원본 정보다.

    파서는 상태를 들고 있다(이어짐 본문이 여는 줄의 종류를 물려받는다). 그래서
    **작업별로, 넣은 순서 그대로** 다시 먹여야 한다.
    """
    from autodeploy.ansible_log import AnsibleLogParser

    if job_id is None:
        async with db.execute(
            "SELECT DISTINCT job_id FROM script_logs ORDER BY job_id"
        ) as cur:
            targets = [int(r["job_id"]) for r in await cur.fetchall()]
    else:
        targets = [job_id]

    changed: dict[int, int] = {}
    for target in targets:
        async with db.execute(
            "SELECT id, line, kind FROM script_logs WHERE job_id=? ORDER BY id",
            (target,),
        ) as cur:
            rows = await cur.fetchall()

        parser = AnsibleLogParser()
        updates = [
            (kind, row["id"])
            for row in rows
            if (kind := parser.feed(row["line"]).kind.value) != row["kind"]
        ]
        if not updates:
            continue
        await db.executemany("UPDATE script_logs SET kind=? WHERE id=?", updates)
        await db.commit()
        changed[target] = len(updates)
    return changed


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
    """host -> {"memo": ..., "key_installed_at": ..., "anydesk_id": ..., "serial": ...}"""
    async with db.execute(
        "SELECT host, memo, key_installed_at, anydesk_id, serial FROM server_meta"
    ) as cur:
        rows = await cur.fetchall()
    return {
        r["host"]: {
            "memo": r["memo"],
            "key_installed_at": r["key_installed_at"],
            "anydesk_id": r["anydesk_id"],
            "serial": r["serial"],
        }
        for r in rows
    }


async def set_server_serial(db: aiosqlite.Connection, host: str, serial: str) -> None:
    """`dmidecode -s system-serial-number` 로 읽은 본체 시리얼.

    **읽어낸 값일 때만 부른다.** dmidecode 가 실패했거나 제조사가 안 넣은 값
    ("Default string" 등)을 그대로 쓰면 서버마다 같은 문자열이 박혀, 기계를
    구분한다는 이 칸의 목적이 사라진다 (거르는 일은 node_info 가 한다).
    """
    await db.execute(
        """INSERT INTO server_meta (host, serial, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(host) DO UPDATE SET
             serial=excluded.serial, updated_at=CURRENT_TIMESTAMP""",
        (host, serial),
    )
    await db.commit()


async def set_anydesk_id(db: aiosqlite.Connection, host: str, anydesk_id: str) -> None:
    """준비 스크립트가 읽어온 AnyDesk 접속 ID.

    **메모와 따로 둔다.** 메모는 사람이 쓴 글이라 덮어쓰면 안 되고, 붙여 쓰면
    다시 등록할 때마다 같은 값이 겹겹이 쌓인다. 화면에서 메모 칸에 나란히 보인다.
    """
    await db.execute(
        """INSERT INTO server_meta (host, anydesk_id, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(host) DO UPDATE SET
             anydesk_id=excluded.anydesk_id, updated_at=CURRENT_TIMESTAMP""",
        (host, anydesk_id),
    )
    await db.commit()


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
    "id", "kind", "status", "env", "ref", "ref_type", "clean_mode", "only_tags", "sync_branch",
    "exit_code", "cancel_by", "current_step", "started_by",
    "slack_channel", "slack_thread_ts", "slack_permalink", "error_message",
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
        f"SELECT job_id, host, status, profile FROM job_hosts"
        f" WHERE job_id IN ({placeholders}) ORDER BY host",
        tuple(j["id"] for j in jobs),
    ) as cur:
        host_rows = await cur.fetchall()

    by_job: dict[int, list[dict]] = {}
    for r in host_rows:
        by_job.setdefault(r["job_id"], []).append(
            {"host": r["host"], "status": r["status"], "profile": r["profile"]}
        )
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
        "SELECT host, status, profile, recap_ok, recap_changed, recap_failed,"
        " recap_unreachable FROM job_hosts WHERE job_id=? ORDER BY host",
        (job_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "host": r["host"],
            "status": r["status"],
            "profile": r["profile"],
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


async def active_job_ids(db: aiosqlite.Connection) -> list[int]:
    """진행 중(대기·실행·승인 대기) 작업 번호. 막고 있는 것을 이름으로 말하려고."""
    marks = ",".join("?" * len(ACTIVE_STATUSES))
    async with db.execute(
        f"SELECT id FROM jobs WHERE status IN ({marks}) ORDER BY id", ACTIVE_STATUSES
    ) as cur:
        return [int(r["id"]) for r in await cur.fetchall()]


async def count_active_jobs(db: aiosqlite.Connection) -> int:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({placeholders})",
        ACTIVE_STATUSES,
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"])


async def delete_jobs(
    db: aiosqlite.Connection, *, job_ids: Sequence[int] | None = None
) -> tuple[list[int], list[int]]:
    """작업 기록을 지운다. `job_ids` 가 None 이면 **끝난 작업 전부**.

    돌려주는 것은 `(지운 id, 건너뛴 id)` 다. "전부" 로 지울 때도 건너뛴 것을
    같이 돌려준다 — 눌렀는데 뭔가 남았으면 이유를 볼 수 있어야 한다.

    **진행 중인 작업은 절대 지우지 않는다.** 러너가 그 job_id 로 로그를 계속
    쓰는 중이라, 행을 지우면 다음 INSERT 가 외래키에서 죽고 실행이 통째로
    넘어간다. 그래서 요청에 섞여 있어도 건너뛰고 그 id 를 돌려준다 —
    화면이 "왜 3건 중 2건만 지워졌는지" 를 말할 수 있게.

    job_events · script_logs · job_hosts 는 ON DELETE CASCADE 로 함께 사라진다
    (연결마다 `PRAGMA foreign_keys = ON` 이라 실제로 동작한다).
    없는 id 는 조용히 무시한다 — 두 번 눌러도 같은 결과가 되도록.
    """
    if job_ids is None:
        # "전부" 를 눌렀는데 뭔가 남았다면 왜 남았는지 말할 수 있어야 한다.
        # 그래서 지울 것만 고르지 않고 상태까지 같이 읽어 건너뛴 것도 돌려준다.
        async with db.execute("SELECT id, status FROM jobs") as cur:
            rows = [(int(r["id"]), str(r["status"])) for r in await cur.fetchall()]
        deletable = [i for i, st in rows if st not in ACTIVE_STATUSES]
        skipped = [i for i, st in rows if st in ACTIVE_STATUSES]
    else:
        # 순서를 지키면서 중복만 제거한다 (dict 는 삽입 순서를 보존).
        ids = list(dict.fromkeys(int(i) for i in job_ids))
        if not ids:
            return [], []
        holes = ",".join("?" * len(ids))
        async with db.execute(
            f"SELECT id, status FROM jobs WHERE id IN ({holes})", ids
        ) as cur:
            rows = {int(r["id"]): str(r["status"]) for r in await cur.fetchall()}
        deletable = [i for i in ids if i in rows and rows[i] not in ACTIVE_STATUSES]
        skipped: list[int] = [i for i in ids if rows.get(i) in ACTIVE_STATUSES]

    if deletable:
        holes = ",".join("?" * len(deletable))
        await db.execute(f"DELETE FROM jobs WHERE id IN ({holes})", deletable)
        await db.commit()
    return deletable, skipped


async def reap_stale_jobs(db: aiosqlite.Connection, *, reason: str) -> list[int]:
    """기동 시 남아있는 진행 중 작업을 정리한다 (§9).

    데몬이 죽으면 그 작업을 감독하던 러너도 함께 사라진다. 상태를 그대로 두면
    영원히 '진행 중'으로 보이고, 그동안 **서버 목록을 고칠 수 없다**
    (`_reject_if_busy`).

    - `running` · `awaiting` → 실패. 프로세스가 떴다가 감독자를 잃었다.
    - `queued` → 취소. 뜬 적이 없으니 실패가 아니라 취소가 맞다.

    queued 를 예전에는 그냥 뒀다. "재기동하면 그대로 실행된다"는 전제였는데
    **그렇지 않다** — 큐는 메모리에만 있고(`JobQueue._pending`) 기동 시 DB 에서
    되살리지 않는다. 그래서 재시작 때 줄 서 있던 작업은 영원히 '대기' 로 남아
    화면을 속이고 인벤토리 편집을 막는다. 되살릴 수 없으면 정리하는 것이 맞다.
    """
    async with db.execute(
        "SELECT id, status FROM jobs WHERE status IN ('queued','running','awaiting')"
    ) as cur:
        rows = [(int(r["id"]), str(r["status"])) for r in await cur.fetchall()]
    if not rows:
        return []

    ids = [i for i, _ in rows]
    queued = [i for i, st in rows if st == "queued"]

    await db.execute(
        "UPDATE jobs SET status='failed', finished_at=CURRENT_TIMESTAMP,"
        " error_message=COALESCE(error_message, ?)"
        " WHERE status IN ('running','awaiting')",
        (reason,),
    )
    if queued:
        holes = ",".join("?" * len(queued))
        await db.execute(
            f"UPDATE jobs SET status='cancelled', finished_at=CURRENT_TIMESTAMP,"
            f" cancel_by='system', error_message=COALESCE(error_message, ?)"
            f" WHERE id IN ({holes})",
            [reason, *queued],
        )
    # 호스트 결과도 작업과 같은 결말을 따른다 — 뜬 적 없는 대상을 '실패' 로
    # 적으면 나중에 실패 이력을 세는 눈이 속는다.
    started = [i for i, st in rows if st != "queued"]
    if started:
        holes = ",".join("?" * len(started))
        await db.execute(
            f"UPDATE job_hosts SET status='failed'"
            f" WHERE job_id IN ({holes}) AND status IN ('queued','running')",
            started,
        )
    if queued:
        holes = ",".join("?" * len(queued))
        await db.execute(
            f"UPDATE job_hosts SET status='cancelled'"
            f" WHERE job_id IN ({holes}) AND status IN ('queued','running')",
            queued,
        )
    await db.executemany(
        "INSERT INTO job_events (job_id, step, level, message) VALUES (?, ?, ?, ?)",
        [(job_id, "daemon", "error", reason) for job_id in ids],
    )
    await db.commit()
    return ids
