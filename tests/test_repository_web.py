"""v2(웹 콘솔) 저장소 함수 — 특히 기동 시 좀비 작업 정리 (§9)."""
from __future__ import annotations

import pytest

from autodeploy.db import connect
from autodeploy.repository import (
    add_script_logs,
    count_active_jobs,
    get_job_detail,
    get_script_logs,
    list_jobs,
    reap_stale_jobs,
)

REASON = "데몬 재시작으로 중단됨"


@pytest.fixture
async def db(temp_db):
    async with connect(temp_db) as conn:
        yield conn


async def make_job(db, *, status: str, kind: str = "install", hosts=()) -> int:
    cur = await db.execute(
        "INSERT INTO jobs (kind, status, started_by) VALUES (?, ?, 'web:yonghyuk')",
        (kind, status),
    )
    job_id = int(cur.lastrowid)
    for host, host_status in hosts:
        await db.execute(
            "INSERT INTO job_hosts (job_id, host, status) VALUES (?, ?, ?)",
            (job_id, host, host_status),
        )
    await db.commit()
    return job_id


# ── reap_stale_jobs ─────────────────────────────────────────────────


async def test_running_job_becomes_failed(db):
    """데몬이 죽으면 러너도 사라진다. 상태를 두면 영원히 '진행 중'으로 보인다."""
    job_id = await make_job(db, status="running")
    assert await reap_stale_jobs(db, reason=REASON) == [job_id]

    job = await get_job_detail(db, job_id)
    assert job["status"] == "failed"
    assert job["error_message"] == REASON
    assert job["finished_at"] is not None


async def test_awaiting_job_is_also_reaped(db):
    """patch 승인 대기도 데몬이 살아있어야 이어서 apply 할 수 있다."""
    job_id = await make_job(db, status="awaiting", kind="patch")
    assert await reap_stale_jobs(db, reason=REASON) == [job_id]
    assert (await get_job_detail(db, job_id))["status"] == "failed"


async def test_queued_job_is_left_alone(db):
    """아직 프로세스가 뜬 적 없으므로 재기동 후 그대로 실행하면 된다."""
    job_id = await make_job(db, status="queued")
    assert await reap_stale_jobs(db, reason=REASON) == []
    assert (await get_job_detail(db, job_id))["status"] == "queued"


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_finished_jobs_are_untouched(db, status):
    job_id = await make_job(db, status=status)
    assert await reap_stale_jobs(db, reason=REASON) == []
    assert (await get_job_detail(db, job_id))["status"] == status


async def test_reap_updates_unfinished_hosts_only(db):
    job_id = await make_job(
        db,
        status="running",
        hosts=[("alpha", "succeeded"), ("beta", "running"), ("gamma", "queued")],
    )
    await reap_stale_jobs(db, reason=REASON)

    hosts = {h["host"]: h["status"] for h in (await get_job_detail(db, job_id))["hosts"]}
    assert hosts["alpha"] == "succeeded", "이미 끝난 호스트 결과를 덮어쓰면 안 된다"
    assert hosts["beta"] == "failed"
    assert hosts["gamma"] == "failed"


async def test_reap_records_an_event(db):
    job_id = await make_job(db, status="running")
    await reap_stale_jobs(db, reason=REASON)
    events = (await get_job_detail(db, job_id))["events"]
    assert [(e["step"], e["level"], e["message"]) for e in events] == [
        ("daemon", "error", REASON)
    ]


async def test_reap_preserves_an_existing_error_message(db):
    job_id = await make_job(db, status="running")
    await db.execute("UPDATE jobs SET error_message='원래 오류' WHERE id=?", (job_id,))
    await db.commit()
    await reap_stale_jobs(db, reason=REASON)
    assert (await get_job_detail(db, job_id))["error_message"] == "원래 오류"


async def test_reap_is_idempotent(db):
    await make_job(db, status="running")
    assert len(await reap_stale_jobs(db, reason=REASON)) == 1
    assert await reap_stale_jobs(db, reason=REASON) == []


async def test_reap_on_empty_db(db):
    assert await reap_stale_jobs(db, reason=REASON) == []


async def test_count_active_jobs(db):
    await make_job(db, status="queued")
    await make_job(db, status="running")
    await make_job(db, status="awaiting")
    await make_job(db, status="succeeded")
    assert await count_active_jobs(db) == 3


# ── 목록 / 로그 ─────────────────────────────────────────────────────


async def test_list_jobs_without_hosts_returns_empty_host_list(db):
    job_id = await make_job(db, status="succeeded")
    jobs = await list_jobs(db)
    assert jobs[0]["id"] == job_id
    assert jobs[0]["hosts"] == []


async def test_list_jobs_on_empty_db(db):
    assert await list_jobs(db) == []


async def test_get_job_detail_missing(db):
    assert await get_job_detail(db, 999) is None


async def test_script_logs_paging_by_cursor(db):
    """SSE 재연결은 마지막 line_id 이후만 받아야 한다 (§F4)."""
    job_id = await make_job(db, status="running")
    await add_script_logs(
        db,
        [(job_id, "bootstrap", "stdout", f"line {i}", None, "out") for i in range(5)],
    )
    first = await get_script_logs(db, job_id, after_id=0, limit=2)
    assert [r["line"] for r in first] == ["line 0", "line 1"]

    rest = await get_script_logs(db, job_id, after_id=first[-1]["id"])
    assert [r["line"] for r in rest] == ["line 2", "line 3", "line 4"]


async def test_script_logs_keep_host_and_kind(db):
    job_id = await make_job(db, status="running")
    await add_script_logs(
        db, [(job_id, "verify", "stdout", "fatal: [beta]: FAILED! =>", "beta", "err")]
    )
    row = (await get_script_logs(db, job_id))[0]
    assert row["host"] == "beta"
    assert row["kind"] == "err"
    assert row["stream"] == "stdout"


async def test_add_script_logs_with_no_rows_is_a_noop(db):
    job_id = await make_job(db, status="running")
    await add_script_logs(db, [])
    assert await get_script_logs(db, job_id) == []
