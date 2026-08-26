"""작업 로그 실시간 브로드캐스트 (dev-spec-web-console §F4).

## 느린 구독자를 어떻게 다루는가

ansible 은 초당 수십 줄을 쏟아낼 수 있다. 브라우저 탭이 백그라운드로 밀리거나
네트워크가 느리면 구독자 큐가 밀리는데, 여기서 **작업 쪽을 기다리게 하면 안 된다**
— 화면 하나 때문에 설치가 느려지는 셈이 되기 때문이다.

그래서 큐를 유한하게 두고, 넘치면 그 구독만 **끊는다**. 클라이언트는 마지막으로 받은
`line_id` 를 들고 재연결하고, 서버는 DB 에서 그 이후를 다시 읽어 이어붙인다. 즉
끊김은 데이터 손실이 아니라 "따라잡기"로 흡수된다 — 이 구조가 성립하는 이유는
모든 줄이 브로드캐스트 **전에** DB 에 적재되기 때문이다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# 구독자당 버퍼. 이걸 넘기면 따라잡기를 포기하고 끊는다.
SUBSCRIBER_QUEUE_SIZE = 512


# eq=False: 구독은 값이 아니라 개체다. 기본 dataclass 는 __eq__ 를 만들면서
# __hash__ 를 없애버려 set 에 담을 수 없다.
@dataclass(slots=True, eq=False)
class Subscription:
    job_id: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(SUBSCRIBER_QUEUE_SIZE))
    dropped: bool = False

    async def get(self) -> dict | None:
        """다음 이벤트. 작업이 끝나 스트림이 닫히면 None."""
        return await self.queue.get()


class SseBroker:
    """작업별 구독자 관리. 프로세스 메모리에만 존재한다."""

    __slots__ = ("_subs",)

    def __init__(self) -> None:
        self._subs: dict[int, set[Subscription]] = {}

    def subscribe(self, job_id: int) -> Subscription:
        sub = Subscription(job_id)
        self._subs.setdefault(job_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.job_id)
        if subs is None:
            return
        subs.discard(sub)
        if not subs:
            self._subs.pop(sub.job_id, None)

    def subscriber_count(self, job_id: int) -> int:
        return len(self._subs.get(job_id, ()))

    def publish(self, job_id: int, event: dict) -> None:
        for sub in tuple(self._subs.get(job_id, ())):
            if sub.dropped:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # 따라오지 못하는 구독자. 끊고, 클라이언트가 after= 로 재연결하게 둔다.
                sub.dropped = True
                self._wake(sub)
                log.info("SSE 구독자가 밀려서 끊음 (job=%d)", job_id)

    def publish_many(self, job_id: int, events: list[dict]) -> None:
        for event in events:
            self.publish(job_id, event)

    def close_job(self, job_id: int) -> None:
        """작업 종료 — 남은 구독자에게 스트림 끝을 알린다."""
        for sub in tuple(self._subs.get(job_id, ())):
            self._wake(sub)

    def _wake(self, sub: Subscription) -> None:
        """None 을 넣어 대기 중인 get() 을 깨운다. 큐가 꽉 찼으면 한 칸 비우고 넣는다."""
        try:
            sub.queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                sub.queue.get_nowait()
                sub.queue.put_nowait(None)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
