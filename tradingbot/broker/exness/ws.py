"""Exness Public Trader API — WebSocket client (official, verified contract).

Two streams:
  /v1/server-events/accounts/{id}/ws/ticks  — live ticks (100-500ms updates)
  /v1/server-events/accounts/{id}/ws/events — transactions, account_state, instruments

Verified contract points (exness-api.com):
  * The handshake is signed exactly like a REST GET request; the ``path``
    field of EXN-DATA must byte-exactly equal the WebSocket connection path.
  * Subscribe commands (one message per event type):
        {"id": "...", "subscribe": {"event": "transactions"}}
        {"id": "...", "subscribe": {"event": "account_state"}}
        {"id": "...", "subscribe": {"event": "instruments", "instruments": [...]}}
        ticks stream: {"id": "...", "subscribe": {"event": "ticks", "instruments": [...]}}
  * After subscribing to ``transactions`` the server immediately sends a
    ``trading_state_snapshot`` — that is the baseline for local state.
  * Envelope of every server message: {"event_type": ..., "payload": {...},
    "operation_id": ... (transaction_event), "event_time": ...}.
  * Tick messages: {"instrument": ..., "bid": ..., "ask": ..., "timestamp": ...}.
  * Server error messages: {"id": ..., "code": ..., "error_message": ...}.
  * NO replay / resume / gap-fill exists. On reconnect the client MUST open a
    new connection, resubscribe, and treat the new trading_state_snapshot as
    the baseline. Slow readers may be disconnected.

This client therefore: reconnects with exponential backoff, resubscribes, and
queues a locally-generated marker into the event queue so the consuming
broker layer knows to re-baseline from a fresh snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import websockets

from tradingbot.broker.exness.signing import RequestSigner
from tradingbot.core.logging import get_logger

log = get_logger("tradingbot.broker.exness.ws")

# Locally-generated markers (never sent to or received from the server):
MARKER_RECONNECTED = "__reconnected__"
MARKER_DISCONNECTED = "__disconnected__"


class ExnessWs:
    """Two independent auto-reconnecting streams with internal queues."""

    def __init__(
        self,
        signer: RequestSigner,
        base_url: str,
        account_id: str,
        instruments: list[str],
        *,
        max_reconnect_s: float = 30.0,
        queue_size: int = 20_000,
        on_state: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._signer = signer
        self._account_id = account_id
        self._instruments = list(instruments)
        self._max_reconnect_s = max_reconnect_s
        self._on_state = on_state
        # https://api.exness.com  ->  wss://api.exness.com
        if base_url.startswith("https://"):
            self._root = "wss://" + base_url[len("https://"):]
        elif base_url.startswith("http://"):
            self._root = "ws://" + base_url[len("http://"):]
        else:
            self._root = base_url
        self._tick_url = f"{self._root}/v1/server-events/accounts/{account_id}/ws/ticks"
        self._event_url = f"{self._root}/v1/server-events/accounts/{account_id}/ws/events"
        self.tick_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.ticks_connected = False
        self.events_connected = False
        self.reconnects_ticks = 0
        self.reconnects_events = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = False

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._stop = False
        self._tasks = [
            asyncio.create_task(self._run_stream("ticks", self._tick_url, self._tick_subscribe(), self.tick_queue)),
            asyncio.create_task(self._run_stream("events", self._event_url, self._event_subscribe(), self.event_queue)),
        ]

    async def stop(self) -> None:
        self._stop = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        self.ticks_connected = False
        self.events_connected = False

    @property
    def connected(self) -> bool:
        return self.ticks_connected and self.events_connected

    def _stream_connected(self, name: str) -> bool:
        return self.ticks_connected if name == "ticks" else self.events_connected

    # ------------------------------------------------------------ subscribe cmds
    def _tick_subscribe(self) -> list[dict[str, Any]]:
        return [
            {"id": str(uuid.uuid4()), "subscribe": {"event": "ticks", "instruments": self._instruments}}
        ]

    def _event_subscribe(self) -> list[dict[str, Any]]:
        return [
            {"id": str(uuid.uuid4()), "subscribe": {"event": "transactions"}},
            {"id": str(uuid.uuid4()), "subscribe": {"event": "account_state"}},
            # Instrument condition updates keep local conditions fresh (volume
            # rules can change intra-session — relevant to execution safety).
            {"id": str(uuid.uuid4()), "subscribe": {"event": "instruments", "instruments": self._instruments}},
        ]

    # --------------------------------------------------------------- run loop
    async def _run_stream(
        self,
        name: str,
        url: str,
        subscribe_msgs: list[dict[str, Any]],
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        # The signed path must be byte-identical to the connection path.
        path = urlparse(url).path
        backoff = 1.0
        ever_connected = False
        while not self._stop:
            established = False
            try:
                headers = self._signer.sign("GET", path, None, "", int(time.time() * 1000))
                async with websockets.connect(url, additional_headers=headers) as ws:
                    for msg in subscribe_msgs:
                        await ws.send(json.dumps(msg, separators=(",", ":")))
                    established = True
                    self._set_connected(name, True)
                    backoff = 1.0
                    if ever_connected:
                        # Came back: consumers re-baseline from the new
                        # trading_state_snapshot that the server sends right
                        # after a successful transactions subscription.
                        self._enqueue_marker(queue, MARKER_RECONNECTED)
                    ever_connected = True
                    log.info("exness ws stream connected", stream=name)
                    async for raw in ws:
                        self._ingest(raw, queue)
                        if self._stop:
                            break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("exness ws stream error", stream=name, exc_info=True)
            finally:
                if established and not self._stop:
                    if self._stream_connected(name):
                        self._set_connected(name, False)
                    self._enqueue_marker(queue, MARKER_DISCONNECTED)
            if self._stop:
                break
            log.warning("exness ws stream reconnecting", stream=name, backoff_s=round(backoff, 2))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_reconnect_s)
            if name == "ticks":
                self.reconnects_ticks += 1
            else:
                self.reconnects_events += 1

    def _set_connected(self, name: str, value: bool) -> None:
        if name == "ticks":
            self.ticks_connected = value
        else:
            self.events_connected = value
        if self._on_state is not None:
            self._on_state(name, value)

    def _enqueue_marker(self, queue: asyncio.Queue[dict[str, Any]], marker: str) -> None:
        try:
            queue.put_nowait({"__marker__": marker})
        except asyncio.QueueFull:
            log.warning("ws marker dropped (queue full)", marker=marker)

    def _ingest(self, raw: str | bytes, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("ws non-json message", raw=str(raw)[:200])
            return
        if not isinstance(data, dict):
            log.warning("ws non-object message", raw=str(raw)[:200])
            return
        # Server error shape: {"id", "code", "error_message"}
        if "error_message" in data and "event_type" not in data:
            log.error("ws server error message", code=data.get("code"),
                      error=data.get("error_message"))
            return
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            # Never block the reader; drop the oldest and warn — the broker
            # layer must treat a queue overflow as "state unknown" and
            # re-baseline, because the server provides no replay.
            try:
                queue.get_nowait()
                queue.put_nowait(data)
            except (asyncio.QueueFull, asyncio.QueueEmpty):
                pass
            log.warning("ws queue full, dropped oldest message")
