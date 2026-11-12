from __future__ import annotations

import asyncio

from app.services.live_feed import LiveFeed


def test_publish_reaches_all_subscribers() -> None:
    async def scenario() -> None:
        feed = LiveFeed()
        q1 = feed.subscribe("r1")
        q2 = feed.subscribe("r1")
        feed.publish("r1", {"type": "live.delta", "purpose": "p", "content": "hello"})
        got1 = await asyncio.wait_for(q1.get(), timeout=1)
        got2 = await asyncio.wait_for(q2.get(), timeout=1)
        assert got1["content"] == "hello"
        assert got2["content"] == "hello"
        assert feed.subscriber_count("r1") == 2
        feed.unsubscribe("r1", q1)
        feed.unsubscribe("r1", q2)
        assert feed.subscriber_count("r1") == 0

    asyncio.run(scenario())


def test_slow_subscriber_never_blocks_publish() -> None:
    async def scenario() -> None:
        feed = LiveFeed(queue_max=2)
        q = feed.subscribe("r1")
        for index in range(10):
            feed.publish("r1", {"seq": index})
        assert q.qsize() <= 2
        # 队列保留的是最新的事件
        first = await asyncio.wait_for(q.get(), timeout=1)
        assert first["seq"] >= 8
        feed.unsubscribe("r1", q)
        # 未订阅的运行不报错
        feed.publish("ghost", {"seq": 1})

    asyncio.run(scenario())
