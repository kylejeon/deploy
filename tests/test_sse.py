"""SseBroker — 특히 느린 구독자가 작업을 붙잡지 못하게 하는 부분 (§F4)."""
from __future__ import annotations

import asyncio

from autodeploy.web.sse import SUBSCRIBER_QUEUE_SIZE, SseBroker


def line(n: int) -> dict:
    return {"type": "line", "id": n, "line": f"line {n}"}


async def test_subscriber_receives_published_events():
    broker = SseBroker()
    sub = broker.subscribe(1)
    broker.publish(1, line(1))
    assert await sub.get() == line(1)


async def test_events_go_only_to_the_matching_job():
    broker = SseBroker()
    a, b = broker.subscribe(1), broker.subscribe(2)
    broker.publish(1, line(1))
    assert await a.get() == line(1)
    assert b.queue.empty()


async def test_multiple_subscribers_all_receive():
    broker = SseBroker()
    subs = [broker.subscribe(1) for _ in range(3)]
    broker.publish(1, line(1))
    for sub in subs:
        assert await sub.get() == line(1)


async def test_publish_with_no_subscribers_is_a_noop():
    SseBroker().publish(99, line(1))


async def test_unsubscribe_stops_delivery():
    broker = SseBroker()
    sub = broker.subscribe(1)
    broker.unsubscribe(sub)
    broker.publish(1, line(1))
    assert sub.queue.empty()
    assert broker.subscriber_count(1) == 0


async def test_close_job_wakes_waiting_subscribers():
    """작업이 끝나면 대기 중인 get() 이 None 으로 깨어나 스트림을 닫는다."""
    broker = SseBroker()
    sub = broker.subscribe(1)
    waiter = asyncio.create_task(sub.get())
    await asyncio.sleep(0)
    broker.close_job(1)
    assert await asyncio.wait_for(waiter, 2) is None


async def test_slow_subscriber_is_dropped_not_blocking():
    """화면 하나가 밀린다고 설치가 느려지면 안 된다.

    끊긴 구독자는 마지막 id 로 재연결해 DB 에서 따라잡는다 — 모든 줄이 방송 전에
    적재되므로 손실이 아니다.
    """
    broker = SseBroker()
    slow = broker.subscribe(1)
    for i in range(SUBSCRIBER_QUEUE_SIZE + 50):
        broker.publish(1, line(i))
    assert slow.dropped is True


async def test_dropped_subscriber_still_gets_a_terminator():
    """끊겼어도 스트림 루프가 빠져나올 수 있어야 한다 (안 그러면 핸들러가 매달린다)."""
    broker = SseBroker()
    sub = broker.subscribe(1)
    for i in range(SUBSCRIBER_QUEUE_SIZE + 5):
        broker.publish(1, line(i))
    assert sub.dropped is True

    seen_none = False
    while not sub.queue.empty():
        if sub.queue.get_nowait() is None:
            seen_none = True
    assert seen_none


async def test_a_dropped_subscriber_does_not_stop_healthy_ones():
    broker = SseBroker()
    slow = broker.subscribe(1)
    fast = broker.subscribe(1)

    for i in range(SUBSCRIBER_QUEUE_SIZE + 5):
        broker.publish(1, line(i))
        if not fast.queue.empty():
            fast.queue.get_nowait()

    assert slow.dropped is True
    broker.publish(1, line(9999))
    assert fast.dropped is False


async def test_publish_many():
    broker = SseBroker()
    sub = broker.subscribe(1)
    broker.publish_many(1, [line(1), line(2)])
    assert (await sub.get())["id"] == 1
    assert (await sub.get())["id"] == 2


async def test_subscriber_count_tracks_subscriptions():
    broker = SseBroker()
    assert broker.subscriber_count(1) == 0
    a = broker.subscribe(1)
    b = broker.subscribe(1)
    assert broker.subscriber_count(1) == 2
    broker.unsubscribe(a)
    broker.unsubscribe(b)
    assert broker.subscriber_count(1) == 0


async def test_unsubscribing_twice_is_safe():
    broker = SseBroker()
    sub = broker.subscribe(1)
    broker.unsubscribe(sub)
    broker.unsubscribe(sub)
