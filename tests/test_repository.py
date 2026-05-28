from __future__ import annotations

import pytest

from autodeploy import repository as repo
from autodeploy.db import connect
from autodeploy.models import Job, JobStatus, Step


def _new_job(**overrides) -> Job:
    base = dict(
        id=None,
        target_ip="192.168.1.50",
        deployment_type="hybrid-with-ai",
        hospital_code="HOSP01",
        started_by="U01",
        slack_channel="C01",
    )
    base.update(overrides)
    return Job(**base)


@pytest.mark.asyncio
async def test_create_job_returns_id_and_persists(temp_db):
    async with connect(temp_db) as db:
        job_id = await repo.create_job(db, _new_job())
        loaded = await repo.get_job(db, job_id)
    assert loaded is not None
    assert loaded.id == job_id
    assert loaded.target_ip == "192.168.1.50"
    assert loaded.deployment_type == "hybrid-with-ai"
    assert loaded.status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_mark_running_sets_started_at(temp_db):
    async with connect(temp_db) as db:
        job_id = await repo.create_job(db, _new_job())
        await repo.mark_running(db, job_id)
        loaded = await repo.get_job(db, job_id)
    assert loaded.status == JobStatus.RUNNING


@pytest.mark.asyncio
async def test_update_current_step_and_commit_sha(temp_db):
    async with connect(temp_db) as db:
        job_id = await repo.create_job(db, _new_job())
        await repo.update_current_step(db, job_id, Step.GIT_PULL)
        await repo.update_commit_sha(db, job_id, "abc123def")
        loaded = await repo.get_job(db, job_id)
    assert loaded.current_step == Step.GIT_PULL
    assert loaded.script_commit_sha == "abc123def"


@pytest.mark.asyncio
async def test_finish_job_succeeded(temp_db):
    async with connect(temp_db) as db:
        job_id = await repo.create_job(db, _new_job())
        await repo.mark_running(db, job_id)
        await repo.finish_job(
            db,
            job_id,
            JobStatus.SUCCEEDED,
            admin_web_url="http://192.168.1.50/",
        )
        loaded = await repo.get_job(db, job_id)
    assert loaded.status == JobStatus.SUCCEEDED
    assert loaded.admin_web_url == "http://192.168.1.50/"
    assert loaded.error_message is None


@pytest.mark.asyncio
async def test_finish_job_failed_records_error(temp_db):
    async with connect(temp_db) as db:
        job_id = await repo.create_job(db, _new_job())
        await repo.mark_running(db, job_id)
        await repo.finish_job(
            db,
            job_id,
            JobStatus.FAILED,
            error_message="kubectl readiness timeout",
        )
        loaded = await repo.get_job(db, job_id)
    assert loaded.status == JobStatus.FAILED
    assert loaded.error_message == "kubectl readiness timeout"


@pytest.mark.asyncio
async def test_add_event_and_script_log(temp_db):
    async with connect(temp_db) as db:
        job_id = await repo.create_job(db, _new_job())
        await repo.add_event(db, job_id, "ssh_connect", "info", "started")
        await repo.add_script_log(db, job_id, "infra_install", "stdout", "Installing...")

        async with db.execute("SELECT * FROM job_events WHERE job_id=?", (job_id,)) as cur:
            events = await cur.fetchall()
        async with db.execute("SELECT * FROM script_logs WHERE job_id=?", (job_id,)) as cur:
            logs = await cur.fetchall()
    assert len(events) == 1
    assert events[0]["message"] == "started"
    assert len(logs) == 1
    assert logs[0]["stream"] == "stdout"


@pytest.mark.asyncio
async def test_list_recent_jobs_ordered_desc(temp_db):
    async with connect(temp_db) as db:
        id1 = await repo.create_job(db, _new_job(target_ip="1.1.1.1"))
        id2 = await repo.create_job(db, _new_job(target_ip="2.2.2.2"))
        id3 = await repo.create_job(db, _new_job(target_ip="3.3.3.3"))
        recent = await repo.list_recent_jobs(db, limit=10)
    # 가장 최신이 첫 번째
    assert [j.id for j in recent] == [id3, id2, id1]


@pytest.mark.asyncio
async def test_find_jobs_by_thread_ts_returns_chain_newest_first(temp_db):
    async with connect(temp_db) as db:
        thread = "1700000000.000001"
        # 같은 스레드: 3건
        id1 = await repo.create_job(db, _new_job(slack_thread_ts=thread))
        id2 = await repo.create_job(db, _new_job(slack_thread_ts=thread, target_ip="2.2.2.2"))
        id3 = await repo.create_job(db, _new_job(slack_thread_ts=thread, target_ip="3.3.3.3"))
        # 다른 스레드
        other_thread = "1700000000.000002"
        await repo.create_job(db, _new_job(slack_thread_ts=other_thread))
        # 스레드 없음
        await repo.create_job(db, _new_job())

        chain = await repo.find_jobs_by_thread_ts(db, thread)
        empty = await repo.find_jobs_by_thread_ts(db, "9999999999.999999")

    assert [j.id for j in chain] == [id3, id2, id1]
    assert empty == []


@pytest.mark.asyncio
async def test_find_active_jobs_returns_only_running_or_queued(temp_db):
    async with connect(temp_db) as db:
        # queued (방금 만들어 mark_running 안 함)
        q_id = await repo.create_job(db, _new_job(target_ip="1.1.1.1"))
        # running
        r_id = await repo.create_job(db, _new_job(target_ip="2.2.2.2"))
        await repo.mark_running(db, r_id)
        # succeeded
        s_id = await repo.create_job(db, _new_job(target_ip="3.3.3.3"))
        await repo.finish_job(db, s_id, JobStatus.SUCCEEDED)
        # failed
        f_id = await repo.create_job(db, _new_job(target_ip="4.4.4.4"))
        await repo.finish_job(db, f_id, JobStatus.FAILED, error_message="x")
        # cancelled
        c_id = await repo.create_job(db, _new_job(target_ip="5.5.5.5"))
        await repo.finish_job(db, c_id, JobStatus.CANCELLED)

        active = await repo.find_active_jobs(db, limit=10)

    ids = {j.id for j in active}
    assert ids == {q_id, r_id}  # 종료된 3건은 빠짐


@pytest.mark.asyncio
async def test_find_active_jobs_respects_limit(temp_db):
    async with connect(temp_db) as db:
        ids = []
        for i in range(5):
            jid = await repo.create_job(db, _new_job(target_ip=f"10.0.0.{i}"))
            await repo.mark_running(db, jid)
            ids.append(jid)

        result = await repo.find_active_jobs(db, limit=2)

    # 가장 최근 2건
    assert [j.id for j in result] == [ids[-1], ids[-2]]


@pytest.mark.asyncio
async def test_find_active_by_ip_only_returns_running_or_queued(temp_db):
    async with connect(temp_db) as db:
        # 진행 중
        id1 = await repo.create_job(db, _new_job(target_ip="10.0.0.1"))
        await repo.mark_running(db, id1)
        # 다른 IP
        await repo.create_job(db, _new_job(target_ip="10.0.0.2"))
        # 같은 IP에 완료된 작업
        id3 = await repo.create_job(db, _new_job(target_ip="10.0.0.1"))
        await repo.finish_job(db, id3, JobStatus.SUCCEEDED)

        active = await repo.find_active_by_ip(db, "10.0.0.1")
    assert len(active) == 1
    assert active[0].id == id1
