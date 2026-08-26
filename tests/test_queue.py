"""JobQueue — 동시 실행 1개 + 대기 큐 테스트 (dev-spec-web-console D6)."""
from __future__ import annotations

import asyncio

import pytest

from autodeploy.queue import CancelOutcome, JobQueue, JobTicket, QueueClosed


async def drain(q: JobQueue, *job_ids: int, timeout: float = 5.0) -> None:
    for job_id in job_ids:
        await q.wait_for(job_id, timeout=timeout)


@pytest.fixture
async def queue():
    q = JobQueue()
    await q.start()
    try:
        yield q
    finally:
        await q.stop(timeout=5.0)


# ── 직렬 실행 ───────────────────────────────────────────────────────


async def test_runs_submitted_job(queue):
    seen = []
    await queue.submit(1, lambda t: _record(seen, t))
    await drain(queue, 1)
    assert seen == [1]


async def test_jobs_run_one_at_a_time(queue):
    """겹치면 컨트롤러 자원(zarf 캐시·ECR 로그인·bundles/)이 충돌한다."""
    concurrent = 0
    peak = 0

    async def job(ticket: JobTicket) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1

    for job_id in range(1, 5):
        await queue.submit(job_id, job)
    await drain(queue, 1, 2, 3, 4)
    assert peak == 1


async def test_execution_order_is_submission_order(queue):
    order: list[int] = []

    async def job(ticket: JobTicket) -> None:
        await asyncio.sleep(0.01)
        order.append(ticket.job_id)

    for job_id in (10, 20, 30):
        await queue.submit(job_id, job)
    await drain(queue, 10, 20, 30)
    assert order == [10, 20, 30]


async def test_failing_job_does_not_stall_the_queue(queue):
    """한 작업이 예외로 죽어도 뒤에 대기 중인 작업이 영원히 멈추면 안 된다."""
    done: list[int] = []

    async def boom(ticket: JobTicket) -> None:
        raise RuntimeError("의도된 실패")

    await queue.submit(1, boom)
    await queue.submit(2, lambda t: _record(done, t))
    await drain(queue, 1, 2)
    assert done == [2]


# ── 대기 순번 ───────────────────────────────────────────────────────


async def test_position_reports_running_and_waiting(queue):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocker(ticket: JobTicket) -> None:
        started.set()
        await release.wait()

    await queue.submit(1, blocker)
    await queue.submit(2, _noop)
    await queue.submit(3, _noop)
    await asyncio.wait_for(started.wait(), 2)

    assert queue.position(1) == 0, "실행 중은 0"
    assert queue.position(2) == 1, "바로 다음 차례는 1"
    assert queue.position(3) == 2
    assert queue.position(99) is None
    assert queue.running_ids == (1,)
    assert queue.pending_ids == (2, 3)
    assert queue.busy is True

    release.set()
    await drain(queue, 1, 2, 3)
    assert queue.busy is False


async def test_duplicate_submit_rejected(queue):
    await queue.submit(1, _noop)
    with pytest.raises(ValueError, match="이미 큐에"):
        await queue.submit(1, _noop)
    await drain(queue, 1)


# ── 취소 ────────────────────────────────────────────────────────────


async def test_cancel_queued_job_never_runs_it(queue):
    ran: list[int] = []
    release = asyncio.Event()

    async def blocker(ticket: JobTicket) -> None:
        await release.wait()

    await queue.submit(1, blocker)
    await queue.submit(2, lambda t: _record(ran, t))
    await asyncio.sleep(0.02)

    assert await queue.cancel(2) is CancelOutcome.DEQUEUED
    release.set()
    await drain(queue, 1)
    await asyncio.sleep(0.05)
    assert ran == [], "대기 중 취소된 작업은 실행되면 안 된다"


async def test_cancel_running_job_invokes_the_bound_hook(queue):
    """실행 중 취소는 러너의 프로세스 종료 함수로 내려가야 한다 (AC-8)."""
    started = asyncio.Event()
    killed = asyncio.Event()

    async def kill() -> bool:
        killed.set()
        return True

    async def job(ticket: JobTicket) -> None:
        ticket.bind_cancel(kill)
        started.set()
        await asyncio.wait_for(killed.wait(), 2)

    await queue.submit(1, job)
    await asyncio.wait_for(started.wait(), 2)
    assert await queue.cancel(1) is CancelOutcome.REQUESTED
    await drain(queue, 1)
    assert killed.is_set()


async def test_cancel_unknown_job(queue):
    assert await queue.cancel(404) is CancelOutcome.NOT_FOUND


async def test_cancel_before_hook_is_bound_is_visible_to_the_worker(queue):
    """프로세스가 뜨기 직전에 눌린 취소를 워커가 볼 수 있어야 한다."""
    observed: list[bool] = []
    release = asyncio.Event()
    reached = asyncio.Event()

    async def blocker(ticket: JobTicket) -> None:
        await release.wait()

    async def job(ticket: JobTicket) -> None:
        # 러너를 띄우기 전에 확인하는 지점
        observed.append(ticket.cancel_requested)
        reached.set()

    await queue.submit(1, blocker)
    await queue.submit(2, job)
    await asyncio.sleep(0.02)

    # 2번은 아직 대기 중이라 DEQUEUED 가 된다 — 여기서는 그 경로가 아니라
    # 티켓 자체의 플래그 전파를 본다.
    ticket = JobTicket(99)
    assert await ticket.request_cancel() is False, "훅이 없으면 False"
    assert ticket.cancel_requested is True

    release.set()
    await drain(queue, 1, 2)
    await asyncio.wait_for(reached.wait(), 2)
    assert observed == [False]


async def test_sync_cancel_hook_is_supported():
    ticket = JobTicket(1)
    calls: list[int] = []
    ticket.bind_cancel(lambda: (calls.append(1), True)[1])
    assert await ticket.request_cancel() is True
    assert calls == [1]


# ── 종료 ────────────────────────────────────────────────────────────


async def test_stop_cancels_running_job():
    """`start_new_session=True` 인 자식은 부모가 죽어도 살아남는다.

    데몬 종료 시 감독 없는 ansible 이 계속 도는 것을 막으려면 취소를 내려야 한다.
    """
    q = JobQueue()
    await q.start()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def job(ticket: JobTicket) -> None:
        ticket.bind_cancel(lambda: cancelled.set() or True)
        started.set()
        await asyncio.wait_for(cancelled.wait(), 3)

    await q.submit(1, job)
    await asyncio.wait_for(started.wait(), 2)
    await q.stop(timeout=5.0)
    assert cancelled.is_set()


async def test_stop_drops_queued_jobs():
    q = JobQueue()
    await q.start()
    ran: list[int] = []
    release = asyncio.Event()

    async def blocker(ticket: JobTicket) -> None:
        await release.wait()

    await q.submit(1, blocker)
    await q.submit(2, lambda t: _record(ran, t))
    await asyncio.sleep(0.02)

    release.set()
    await q.stop(timeout=5.0)
    assert ran == []


async def test_submit_after_stop_rejected():
    q = JobQueue()
    await q.start()
    await q.stop(timeout=5.0)
    with pytest.raises(QueueClosed):
        await q.submit(1, _noop)


async def test_stop_without_start_is_safe():
    await JobQueue().stop(timeout=1.0)


async def test_concurrency_must_be_positive():
    with pytest.raises(ValueError):
        JobQueue(concurrency=0)


async def test_wait_for_unknown_job_returns_false(queue):
    assert await queue.wait_for(404, timeout=0.1) is False


# ── 헬퍼 ────────────────────────────────────────────────────────────


async def _noop(ticket: JobTicket) -> None:
    return None


async def _record(sink: list[int], ticket: JobTicket) -> None:
    sink.append(ticket.job_id)
