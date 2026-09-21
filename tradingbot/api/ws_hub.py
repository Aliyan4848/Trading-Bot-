"""WebSocket hub: fans the in-process event bus out to dashboard clients.

Each connection owns one bus subscriber queue; a forwarding task drains it.
Slow clients drop their own oldest events (bounded queue) — the trading loop
is never blocked by dashboard consumers.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import WebSocket

from tradingbot.core.events import EventBus
from tradingbot.core.logging import get_logger

log = get_logger("tradingbot.api.ws")


class ConnectionManager:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._clients: set[WebSocket] = set()
        self._tasks: dict[int, asyncio.Task] = {}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        queue = self._bus.subscribe()
        self._tasks[id(ws)] = asyncio.create_task(self._forward(ws, queue), name="ws-forward")
        log.info("dashboard client connected", clients=len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        task = self._tasks.pop(id(ws), None)
        if task is not None:
            task.cancel()
        log.info("dashboard client disconnected", clients=len(self._clients))

    async def _forward(self, ws: WebSocket, queue: asyncio.Queue) -> None:
        try:
            while True:
                event = await queue.get()
                await ws.send_json(event.to_dict())
        except (asyncio.CancelledError, RuntimeError, Exception):  # noqa: BLE001
            pass

    async def broadcast_now(self, payload: dict) -> None:
        """Send a payload to all connected clients right now (e.g. kill switch ack)."""
        for ws in list(self._clients):
            with contextlib.suppress(Exception):  # pragma: no cover
                await ws.send_json(payload)
