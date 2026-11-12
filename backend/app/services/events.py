from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Any

from ..db import Database, utc_now


@dataclass
class EventMessage:
    id: int
    event_type: str
    payload: dict[str, Any]
    created_at: str


class EventBus:
    def __init__(self, database: Database) -> None:
        self.db = database
        self._subscribers: dict[str, set[asyncio.Queue[EventMessage]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    def publish(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> int:
        with self.db.connection() as connection:
            event = self.record(connection, run_id, event_type, payload or {})
        self.broadcast(run_id, event)
        return event.id

    @staticmethod
    def record(
        connection: Connection, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> EventMessage:
        created = utc_now()
        event_id = connection.execute(
            "INSERT INTO events (run_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, event_type, json.dumps(payload, ensure_ascii=False), created),
        ).lastrowid
        return EventMessage(int(event_id), event_type, payload, created)

    def broadcast(self, run_id: str, event: EventMessage) -> None:
        for queue in list(self._subscribers.get(run_id, set())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, run_id: str) -> asyncio.Queue[EventMessage]:
        queue: asyncio.Queue[EventMessage] = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue[EventMessage]) -> None:
        async with self._lock:
            self._subscribers[run_id].discard(queue)

    def replay(self, run_id: str, after: int = 0) -> list[EventMessage]:
        rows = self.db.query_all(
            "SELECT id, event_type, payload_json, created_at FROM events "
            "WHERE run_id = ? AND id > ? ORDER BY id ASC LIMIT 2000",
            (run_id, after),
        )
        return [
            EventMessage(
                id=row["id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"] or "{}"),
                created_at=row["created_at"],
            )
            for row in rows
        ]
