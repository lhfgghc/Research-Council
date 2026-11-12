"""In-memory live feed forwarding streaming model deltas to the frontend.

Deltas are intentionally not persisted: they are transient render material
between one model call starting and its final message landing. Everything
durable already goes through the EventBus and the messages table.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class LiveFeed:
    def __init__(self, queue_max: int = 800) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._queue_max = queue_max

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_max)
        self._subs[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        self._subs.get(run_id, set()).discard(queue)
        if not self._subs.get(run_id):
            self._subs.pop(run_id, None)

    def publish(self, run_id: str, payload: dict[str, Any]) -> None:
        for queue in list(self._subs.get(run_id, set())):
            if queue.full():
                # 丢弃最旧的一条，保证慢消费者不阻塞生成过程
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, set()))
