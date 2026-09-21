"""WebSocket client tests against a fake in-process Exness WS server.

The fake server verifies the handshake exactly like the real API: the EXN-DATA
path must byte-exactly equal the connection path and EXN-SIGN must verify with
the Ed25519 public key. It then emulates the documented protocol: subscribe
commands first, then a trading_state_snapshot on the events stream and an
initial tick on the ticks stream.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from typing import Any

import pytest
import websockets
from nacl.signing import SigningKey, VerifyKey

from tradingbot.broker.exness.signing import RequestSigner
from tradingbot.broker.exness.ws import MARKER_RECONNECTED, ExnessWs

API_KEY = "EXNTESTKEY0000000001"
SEED = base64.b64encode(bytes(range(32)))
ACCOUNT_ID = "152706877"

TICKS_PATH = f"/v1/server-events/accounts/{ACCOUNT_ID}/ws/ticks"
EVENTS_PATH = f"/v1/server-events/accounts/{ACCOUNT_ID}/ws/events"

SNAPSHOT = {
    "event_type": "trading_state_snapshot",
    "payload": {
        "orders": [],
        "positions": [{"position_id": "7001", "direction": "buy", "instrument": "EURUSD",
                       "volume": "0.10", "open_price": "1.08340", "state": "open",
                       "create_time": "2026-09-20T00:00:00Z"}],
        "account_state": {"balance": "10000", "equity": "10000", "used_margin": "0"},
    },
}


def _first_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeWsServer:
    def __init__(self, verifying: VerifyKey) -> None:
        self.verifying = verifying
        self.port = _first_free_port()
        self.tick_conns = 0
        self.event_conns = 0
        self.second_event_conn = asyncio.Event()
        self.tick_push: asyncio.Queue[Any] = asyncio.Queue()
        self.event_push: asyncio.Queue[Any] = asyncio.Queue()
        self.drop_after_first_snapshot: bool = False
        self._stop_evt = asyncio.Event()
        self.seen_tick_subscribe = asyncio.Event()
        self.seen_event_subscribe = asyncio.Event()
        self.tick_instruments: list[str] | None = None
        self.event_subscriptions: list[str] = []
        self._server: Any = None

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, "127.0.0.1", self.port)

    async def stop(self) -> None:
        self._stop_evt.set()
        self.tick_push.put_nowait(None)
        self.event_push.put_nowait(None)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------ verification
    def _verify_handshake(self, connection: Any, path: str) -> None:
        headers = connection.request.headers
        data_b64 = headers["EXN-DATA"]
        sign_b64 = headers["EXN-SIGN"]
        raw = base64.urlsafe_b64decode(data_b64 + "=" * (-len(data_b64) % 4))
        sig = base64.urlsafe_b64decode(sign_b64 + "=" * (-len(sign_b64) % 4))
        self.verifying.verify(raw, sig)
        data = json.loads(raw)
        assert data["method"] == "GET"
        assert data["path"] == path  # byte-exact connection path requirement
        assert headers["EXN-API-KEY"] == API_KEY
        assert headers["EXN-IDEMPOTENCY-KEY"] == ""  # empty for signed GETs

    # --------------------------------------------------------------- handlers
    async def _handler(self, connection: Any) -> None:
        path = connection.request.path
        try:
            self._verify_handshake(connection, path)
            if path == TICKS_PATH:
                await self._handle_ticks(connection)
            elif path == EVENTS_PATH:
                await self._handle_events(connection)
            else:
                await connection.close(1008, "unknown path")
        except Exception:
            await connection.close(1008, "protocol error")

    async def _handle_ticks(self, connection: Any) -> None:
        self.tick_conns += 1
        msg = json.loads(await connection.recv())
        assert msg["subscribe"]["event"] == "ticks"
        self.tick_instruments = msg["subscribe"]["instruments"]
        self.seen_tick_subscribe.set()
        # server sends the last tick per instrument right after subscribing
        await connection.send(json.dumps(
            {"instrument": "EURUSD", "bid": "1.08340", "ask": "1.08355",
             "timestamp": "2026-09-20T00:00:00.100Z"}
        ))
        await self._pump(connection, self.tick_push)

    async def _handle_events(self, connection: Any) -> None:
        self.event_conns += 1
        subs: set[str] = set()
        while not {"transactions", "account_state"} <= subs:
            msg = json.loads(await asyncio.wait_for(connection.recv(), timeout=5.0))
            if "subscribe" not in msg:
                continue
            ev = msg["subscribe"]["event"]
            self.event_subscriptions.append(ev)
            subs.add(ev)
        self.seen_event_subscribe.set()
        if self.event_conns == 1:
            self.second_event_conn.clear()
        if self.event_conns >= 2:
            self.second_event_conn.set()
        await connection.send(json.dumps(SNAPSHOT))
        if self.drop_after_first_snapshot and self.event_conns == 1:
            await connection.close()  # simulate a server-side drop
        await self._pump(connection, self.event_push)

    async def _pump(self, connection: Any, queue: asyncio.Queue[Any]) -> None:
        while not self._stop_evt.is_set():
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=0.2)
            except TimeoutError:
                continue
            if msg is None:
                return
            try:
                await connection.send(json.dumps(msg))
            except Exception:
                return


@pytest.fixture
async def server():
    srv = FakeWsServer(SigningKey(base64.b64decode(SEED)).verify_key)
    await srv.start()
    yield srv
    await srv.stop()


async def _wait(pred, timeout: float = 6.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_handshake_signed_and_streams_flow(server: FakeWsServer) -> None:
    signer = RequestSigner(API_KEY, SEED.decode())
    ws = ExnessWs(signer, server.base_url(), ACCOUNT_ID, ["EURUSD"], max_reconnect_s=2.0)
    await ws.start()
    try:
        await _wait(lambda: ws.ticks_connected and ws.events_connected)
        # ticks: initial tick per instrument arrives immediately after subscribe
        await _wait(lambda: not ws.tick_queue.empty())
        tick = ws.tick_queue.get_nowait()
        assert tick == {"instrument": "EURUSD", "bid": "1.08340", "ask": "1.08355",
                        "timestamp": "2026-09-20T00:00:00.100Z"}
        assert server.tick_instruments == ["EURUSD"]
        # events: trading_state_snapshot right after transactions subscribe
        await _wait(lambda: not ws.event_queue.empty())
        snap = ws.event_queue.get_nowait()
        assert snap["event_type"] == "trading_state_snapshot"
        assert "transactions" in server.event_subscriptions
        assert "account_state" in server.event_subscriptions
        # subsequent messages flow through
        server.event_push.put_nowait(
            {"event_type": "account_state_event",
             "payload": {"account_state": {"balance": "10001", "equity": "10001",
                                           "used_margin": "0"}}}
        )
        server.tick_push.put_nowait(
            {"instrument": "EURUSD", "bid": "1.08341", "ask": "1.08356",
             "timestamp": "2026-09-20T00:00:01.100Z"}
        )
        await _wait(lambda: ws.event_queue.qsize() >= 1 and ws.tick_queue.qsize() >= 1)
        assert ws.event_queue.get_nowait()["event_type"] == "account_state_event"
        assert ws.tick_queue.get_nowait()["bid"] == "1.08341"
    finally:
        await ws.stop()
    assert not ws.ticks_connected and not ws.events_connected


async def test_reconnect_resubscribes_and_marks(server: FakeWsServer) -> None:
    server.drop_after_first_snapshot = True
    signer = RequestSigner(API_KEY, SEED.decode())
    ws = ExnessWs(signer, server.base_url(), ACCOUNT_ID, ["EURUSD"], max_reconnect_s=1.0)
    await ws.start()
    try:
        await _wait(lambda: ws.events_connected)
        await _wait(lambda: not ws.event_queue.empty())  # first snapshot
        # server drops the connection; client must reconnect and resubscribe
        await _wait(lambda: server.second_event_conn.is_set(), timeout=8.0)
        # drain the queue: expect disconnect/reconnect markers and the fresh
        # trading_state_snapshot baseline sent on the new connection
        markers: list[str] = []
        snapshots = 0
        while not ws.event_queue.empty():
            m = ws.event_queue.get_nowait()
            if "__marker__" in m:
                markers.append(m["__marker__"])
            elif m.get("event_type") == "trading_state_snapshot":
                snapshots += 1
        assert MARKER_RECONNECTED in markers
        assert snapshots >= 1
        assert server.event_conns >= 2
        assert "transactions" in server.event_subscriptions  # resubscribed again
    finally:
        await ws.stop()
