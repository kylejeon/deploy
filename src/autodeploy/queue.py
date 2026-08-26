"""전역 작업 큐 — 동시 실행 1개 + 대기 (dev-spec-web-console §F3 / D6).

## 왜 1개인가

동시에 도는 hubctl 두 개가 **컨트롤러(맥미니) 쪽 자원을 공유**한다:

- `roles/patch_apply/tasks/main.yml:11` 이 `bundles/` 에서 **mtime 최신 번들을
  자동 선택**한다 → 패치 두 개가 겹치면 남의 번들을 적용할 수 있다.
- `ecr_auth`·`secrets_fetch`·`gitops_publish`·`app_charts_fetch`·`platform_secrets`
  가 `delegate_to: localhost` 로 돈다 → zarf 캐시·ECR 로그인 세션이 겹친다.

여러 서버를 동시에 설치하는 것은 "작업 여러 개"가 아니라 **"작업 하나에 `-l a,b,c`"**
로 처리하므로, 이 제한이 다중 서버 설치를 막지 않는다. 제한되는 것은 작업 개수다.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

log = logging.getLogger(__name__)


class CancelOutcome(StrEnum):
    NOT_FOUND = "not_found"
    DEQUEUED = "dequeued"    # 아직 시작 전이라 큐에서 뺐다 (프로세스가 뜬 적 없음)
    REQUESTED = "requested"  # 실행 중 — 러너에 종료를 요청했다


CancelHook = Callable[[], Awaitable[bool] | bool]


@dataclass(slots=True)
class JobTicket:
    """큐에 들어간 작업 하나의 손잡이. 워커가 러너를 띄운 뒤 취소 훅을 건다."""

    job_id: int
    cancel_requested: bool = False
    _hook: CancelHook | None = field(default=None, repr=False)

    def bind_cancel(self, hook: CancelHook) -> None:
        """러너의 종료 함수를 등록한다.

        등록 시점엔 아직 프로세스가 없을 수 있다. "등록 직전에 눌린 취소"를 놓치지
        않으려면 호출자가 등록 후 `cancel_requested` 를 확인하고 실행을 건너뛰어야
        하며, 실행에 들어간 뒤의 경쟁은 러너가 기동 직후 자기 취소 플래그를 다시
        확인해 처리한다 (`HubctlRunner.run`).
        """
        self._hook = hook

    async def request_cancel(self) -> bool:
        self.cancel_requested = True
        if self._hook is None:
            return False
        result = self._hook()
        if asyncio.iscoroutine(result):
            return bool(await result)
        return bool(result)


JobRunner = Callable[[JobTicket], Awaitable[None]]


@dataclass(slots=True)
class _Entry:
    ticket: JobTicket
    run: JobRunner
    done: asyncio.Event = field(default_factory=asyncio.Event)


class QueueClosed(RuntimeError):
    pass


class JobQueue:
    """직렬 실행기. `submit` 한 순서대로 하나씩 돌린다."""

    def __init__(self, *, concurrency: int = 1) -> None:
        if concurrency < 1:
            raise ValueError("concurrency 는 1 이상이어야 합니다")
        self._concurrency = concurrency
        self._pending: deque[_Entry] = deque()
        self._running: dict[int, _Entry] = {}
        self._cond = asyncio.Condition()
        self._workers: list[asyncio.Task[None]] = []
        self._closing = False

    # -- 수명주기 --

    async def start(self) -> None:
        if self._workers:
            return
        self._closing = False
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"jobqueue-worker-{i}")
            for i in range(self._concurrency)
        ]

    async def stop(self, *, cancel_running: bool = True, timeout: float = 30.0) -> None:
        """워커를 세운다.

        실행 중인 작업은 기본적으로 취소한다. `start_new_session=True` 로 띄운
        자식은 부모가 죽어도 살아남기 때문에, 그냥 두면 감독자 없는 ansible 이
        계속 서버를 건드리고 로그는 어디에도 남지 않는다.
        """
        async with self._cond:
            self._closing = True
            dequeued = list(self._pending)
            self._pending.clear()
            running = list(self._running.values())
            self._cond.notify_all()

        for entry in dequeued:
            entry.ticket.cancel_requested = True
            entry.done.set()

        if cancel_running:
            for entry in running:
                try:
                    await entry.ticket.request_cancel()
                except Exception:
                    log.exception("작업 %d 취소 중 오류", entry.ticket.job_id)

        workers = self._workers
        self._workers = []
        if workers:
            _, pendingtasks = await asyncio.wait(workers, timeout=timeout)
            for task in pendingtasks:
                task.cancel()

    # -- 큐 조작 --

    async def submit(self, job_id: int, run: JobRunner) -> JobTicket:
        async with self._cond:
            if self._closing:
                raise QueueClosed("큐가 종료 중입니다")
            if job_id in self._running or any(e.ticket.job_id == job_id for e in self._pending):
                raise ValueError(f"이미 큐에 있는 작업입니다: {job_id}")
            entry = _Entry(JobTicket(job_id), run)
            self._pending.append(entry)
            self._cond.notify()
        return entry.ticket

    async def cancel(self, job_id: int) -> CancelOutcome:
        async with self._cond:
            for entry in self._pending:
                if entry.ticket.job_id == job_id:
                    self._pending.remove(entry)
                    entry.ticket.cancel_requested = True
                    entry.done.set()
                    return CancelOutcome.DEQUEUED
            running = self._running.get(job_id)
        if running is None:
            return CancelOutcome.NOT_FOUND
        # 조건변수를 쥔 채로 프로세스를 죽이지 않는다 — SIGTERM 유예 동안 큐 전체가 멈춘다.
        await running.ticket.request_cancel()
        return CancelOutcome.REQUESTED

    async def wait_for(self, job_id: int, *, timeout: float | None = None) -> bool:
        """해당 작업이 끝날 때까지 (테스트·종료 처리용). 큐에 없으면 False."""
        async with self._cond:
            entry = self._running.get(job_id) or next(
                (e for e in self._pending if e.ticket.job_id == job_id), None
            )
        if entry is None:
            return False
        if timeout is None:
            await entry.done.wait()
            return True
        try:
            await asyncio.wait_for(entry.done.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # -- 조회 --

    def position(self, job_id: int) -> int | None:
        """0 = 실행 중, 1 이상 = 앞에 남은 작업 수. 큐에 없으면 None."""
        if job_id in self._running:
            return 0
        for index, entry in enumerate(self._pending):
            if entry.ticket.job_id == job_id:
                return index + 1
        return None

    @property
    def running_ids(self) -> tuple[int, ...]:
        return tuple(self._running)

    @property
    def pending_ids(self) -> tuple[int, ...]:
        return tuple(e.ticket.job_id for e in self._pending)

    @property
    def busy(self) -> bool:
        return bool(self._running) or bool(self._pending)

    # -- 워커 --

    async def _worker(self, index: int) -> None:
        while True:
            async with self._cond:
                while not self._pending and not self._closing:
                    await self._cond.wait()
                if not self._pending:
                    return
                entry = self._pending.popleft()
                self._running[entry.ticket.job_id] = entry
            try:
                await entry.run(entry.ticket)
            except asyncio.CancelledError:
                log.warning("작업 %d 워커가 취소됨", entry.ticket.job_id)
                raise
            except Exception:
                # 한 작업의 실패로 큐가 서면 안 된다. 상태 기록은 run 쪽 책임.
                log.exception("작업 %d 실행 중 처리되지 않은 예외", entry.ticket.job_id)
            finally:
                entry.done.set()
                async with self._cond:
                    self._running.pop(entry.ticket.job_id, None)
                    self._cond.notify_all()
