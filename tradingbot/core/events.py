"""In-process async event bus.

Publish/subscribe over asyncio queues. Subscribers:
  * WebSocket hub (dashboard real-time updates)
  * structured log writer
  * (Phase 5) risk event recorder

Delivery is best-effort with a bounded queue: slow consumers drop their own
oldest events rather than blocking the trading loop.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradingbot.core.timeutils import utcnow


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=utcnow)
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "ts": self.ts.isoformat(timespec="milliseconds"), "data": self.data}


class EventBus:
    def __init__(self, max_queue: int = 2000, history: int = 500) -> None:
        self._queues: list[asyncio.Queue[Event]] = []
        self._history: deque[Event] = deque(maxlen=history)
        self._max_queue = max_queue
        self._counter = itertools.count(1)
        self.started_at = utcnow()

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max_queue)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    async def publish(self, event: Event) -> None:
        event.id = next(self._counter)
        self._history.append(event)
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # bounded: drop this consumer's oldest event
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except asyncio.QueueFull:  # pragma: no cover
                    pass

    def recent(self, n: int = 100) -> list[Event]:
        items = list(self._history)
        return items[-n:]

    async def close(self) -> None:
        for q in self._queues:
            self.unsubscribe(q)
