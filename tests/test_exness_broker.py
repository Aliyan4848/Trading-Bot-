"""ExnessBroker integration tests.

The broker runs against a fake in-process Exness server: REST via
httpx.MockTransport and WS via websockets.serve, sharing one state object.
The fake implements only the verified endpoints and emulates the documented
async flow: 202 ACK on mutations, final state via transaction_event on the WS
events stream, operation_status for polling.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import websockets

from tradingbot.broker.exness.broker import ExnessBroker
from tradingbot.broker.exness.rest import ExnessRestClient
from tradingbot.broker.exness.signing import RequestSigner
from tradingbot.broker.exness.ws import ExnessWs
from tradingbot.broker.interfaces import BrokerError
from tradingbot.broker.models import OperationState, OrderRequest, Side
from tradingbot.core.config import Settings

API_KEY = "EXNTESTKEY0000000001"
SEED_B64 = base64.b64encode(bytes(range(32))).decode()
ACCOUNT_ID = "152706877"

EURUSD_COND = {
    "instrument": "EURUSD", "point_digits": 5, "contract_size": "100000",
    "margin_currency": "USD", "volume_min": "0.01", "volume_max": "100",
    "volume_step": "0.01", "trade_mode": "enabled",
}
GBPUSD_COND = {**EURUSD_COND, "instrument": "GBPUSD"}
XAUUSD_COND = {**EURUSD_COND, "instrument": "XAUUSD", "point_digits": 2, "contract_size": "100"}
CONDITIONS = {"EURUSD": EURUSD_COND, "GBPUSD": GBPUSD_COND, "XAUUSD": XAUUSD_COND}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeExness:
    """Shared fake Exness server state + handlers (REST + WS)."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "next_op": 9000,
            "operations": {},   # op_id -> {"operation_id", "status", "client_request_id"}
            "positions": {},    # pid -> snapshot position dict
            "sltp_orders": [],  # snapshot sltp order dicts
            "acct": {"balance": "10000", "equity": "10000", "used_margin": "0"},
            "opens": [],
            "closes": [],
        }
        self.ws_port = _free_port()
        self.tick_push: asyncio.Queue[Any] = asyncio.Queue()
        self.event_push: asyncio.Queue[Any] = asyncio.Queue()
        self.drop_events_once: bool = False
        self._events_sent: bool = False
        self._stop = asyncio.Event()
        self._server: Any = None
        self._verifying = None  # set by caller

    # ------------------------------------------------------------- REST side
    def rest_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._rest_handler)

    def _rest_handler(self, request: httpx.Request) -> httpx.Response:
        s = self.state
        path, query = request.url.path, dict(request.url.params)
        a = ACCOUNT_ID
        if path == f"/v1/configuration/accounts/{a}/account":
            return httpx.Response(200, json={"account": {"id": a, "currency": "USD", "leverage": "500"}})
        if path == f"/v1/configuration/accounts/{a}/limits":
            return httpx.Response(200, json={
                "limits": {"rest": {"global_account_rate": {"limit": 1000, "window_seconds": 1},
                                    "methods": []}}})
        if path == f"/v1/configuration/accounts/{a}/instruments":
            return httpx.Response(200, json={"instruments": list(CONDITIONS)})
        if "/instruments/" in path and path.endswith("/conditions"):
            inst = path.rsplit("/", 2)[1]
            return httpx.Response(200, json=CONDITIONS.get(inst) or {})
        if path == f"/v1/market-data/accounts/{a}/candles":
            return httpx.Response(200, json={"candles": []})
        if path == f"/v1/history/accounts/{a}/deals":
            return httpx.Response(200, json={
                "items": [{
                    "event_time": "2026-09-20T00:00:01Z",
                    "deal": {"deal_id": "9001", "type": "open", "direction": "buy",
                             "instrument": "EURUSD", "volume": "0.01", "price": "1.08340",
                             "position_id": "7001", "create_time": "2026-09-20T00:00:01Z"},
                }],
                "has_more": False,
            })
        if path == f"/v1/trading/accounts/{a}/snapshot":
            return httpx.Response(200, json=self.snapshot_payload())
        if path.startswith(f"/v1/trading/accounts/{a}/operations/"):
            op_id = path.rsplit("/", 1)[1]
            op = s["operations"].get(op_id)
            if op is None:
                return httpx.Response(404, json={"code": 3014, "error_message": "REQUEST_OPERATION_NOT_FOUND"})
            return httpx.Response(200, json=op)
        if path == f"/v1/trading/accounts/{a}/positions" and request.method == "POST":
            body = json.loads(request.content)
            crid = request.headers.get("EXN-IDEMPOTENCY-KEY", "")
            op_id = str(s["next_op"])
            s["next_op"] += 1
            s["opens"].append({"body": body, "crid": crid})
            s["operations"][op_id] = {"operation_id": op_id, "status": "pending",
                                      "client_request_id": crid}
            return httpx.Response(202, json={"operation_id": op_id, "client_request_id": crid,
                                             "status": "accepted"})
        if path.startswith(f"/v1/trading/accounts/{a}/positions/") and request.method == "DELETE":
            pid = path.rsplit("/", 1)[1]
            if s["positions"].get(pid, {}).get("state") != "open":
                return httpx.Response(400, json={"code": 3013, "error_message": "REQUEST_POSITION_NOT_FOUND"})
            crid = request.headers.get("EXN-IDEMPOTENCY-KEY", "")
            op_id = str(s["next_op"])
            s["next_op"] += 1
            s["closes"].append({"pid": pid, "crid": crid, "query": query})
            s["operations"][op_id] = {"operation_id": op_id, "status": "pending",
                                      "client_request_id": crid}
            return httpx.Response(202, json={"operation_id": op_id, "client_request_id": crid,
                                             "status": "accepted"})
        return httpx.Response(404, json={"code": 3000, "error_message": "REQUEST_INVALID"})

    # ------------------------------------------------------------- WS side
    async def start_ws(self) -> None:
        self._server = await websockets.serve(self._ws_handler, "127.0.0.1", self.ws_port)

    async def stop_ws(self) -> None:
        self._stop.set()
        self.tick_push.put_nowait(None)
        self.event_push.put_nowait(None)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _ws_handler(self, connection: Any) -> None:
        try:
            if connection.request.path.endswith("/ws/ticks"):
                await self._ws_ticks(connection)
            elif connection.request.path.endswith("/ws/events"):
                await self._ws_events(connection)
            else:
                await connection.close(1008, "unknown path")
        except Exception:
            pass

    async def _ws_ticks(self, connection: Any) -> None:
        msg = json.loads(await connection.recv())
        assert msg["subscribe"]["event"] == "ticks"
        await connection.send(json.dumps(
            {"instrument": "EURUSD", "bid": "1.08340", "ask": "1.08355",
             "timestamp": "2026-09-20T00:00:00.100Z"}))
        await self._pump(connection, self.tick_push)

    async def _ws_events(self, connection: Any) -> None:
        while True:
            msg = json.loads(await connection.recv())
            if msg["subscribe"]["event"] == "transactions":
                break
        first = not self._events_sent
        await connection.send(json.dumps(self.snapshot_message()))
        self._events_sent = True
        if self.drop_events_once and first:
            await connection.close()  # simulate server-side drop after baseline
        await self._pump(connection, self.event_push)

    async def _pump(self, connection: Any, queue: asyncio.Queue[Any]) -> None:
        while not self._stop.is_set():
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

    # ------------------------------------------------------------- helpers
    def snapshot_payload(self) -> dict[str, Any]:
        s = self.state
        return {
            "orders": s["sltp_orders"],
            "positions": [p for p in s["positions"].values() if p["state"] == "open"],
            "account_state": s["acct"],
        }

    def snapshot_message(self) -> dict[str, Any]:
        return {"event_type": "trading_state_snapshot", "payload": self.snapshot_payload()}

    def confirm_open(
        self,
        op_id: str,
        crid: str,
        *,
        position_id: str = "7001",
        price: str = "1.08345",
        volume: str = "0.10",
        balance: str = "9995",
    ) -> None:
        s = self.state
        s["positions"][position_id] = {
            "position_id": position_id, "direction": "buy", "instrument": "EURUSD",
            "volume": volume, "open_price": price, "state": "open",
            "create_time": "2026-09-20T00:00:05Z",
        }
        s["sltp_orders"].append({
            "order_id": str(int(position_id) + 100), "position_id": position_id,
            "order_type": "sltp", "state": "placed",
            "stop_loss_price": "1.07900", "take_profit_price": "1.08800",
        })
        s["operations"][op_id] = {"operation_id": op_id, "status": "confirmed",
                                  "client_request_id": crid}
        s["acct"] = {"balance": balance, "equity": balance, "used_margin": "50"}
        self.event_push.put_nowait({
            "event_type": "transaction_event",
            "operation_id": op_id,
            "client_request_id": crid,
            "status": "success",
            "event_time": "2026-09-20T00:00:05Z",
            "payload": {
                "orders": [
                    {"order_id": str(int(position_id) + 50), "order_type": "market",
                     "state": "filled", "direction": "buy", "instrument": "EURUSD",
                     "price": price, "volume": volume, "create_time": "2026-09-20T00:00:05Z"},
                    {"order_id": str(int(position_id) + 100), "position_id": position_id,
                     "order_type": "sltp", "state": "placed", "stop_loss_price": "1.07900",
                     "take_profit_price": "1.08800", "create_time": "2026-09-20T00:00:05Z"},
                ],
                "deals": [
                    {"deal_id": str(int(position_id) + 1000), "order_id": str(int(position_id) + 50),
                     "position_id": position_id, "type": "open", "direction": "buy",
                     "instrument": "EURUSD", "volume": volume, "price": price,
                     "create_time": "2026-09-20T00:00:05Z"},
                ],
                "positions": [dict(s["positions"][position_id])],
                "account_state": dict(s["acct"]),
            },
        })

    def confirm_close(
        self,
        op_id: str,
        crid: str,
        *,
        position_id: str = "7001",
        price: str = "1.08400",
        profit: str = "5.50",
        balance: str = "10000.50",
    ) -> None:
        s = self.state
        s["positions"][position_id]["state"] = "closed"
        s["operations"][op_id] = {"operation_id": op_id, "status": "confirmed",
                                  "client_request_id": crid}
        s["acct"] = {"balance": balance, "equity": balance, "used_margin": "0"}
        self.event_push.put_nowait({
            "event_type": "transaction_event",
            "operation_id": op_id,
            "client_request_id": crid,
            "status": "success",
            "event_time": "2026-09-20T01:00:00Z",
            "payload": {
                "deals": [
                    {"deal_id": "9500", "position_id": position_id, "type": "close",
                     "direction": "sell", "instrument": "EURUSD", "volume": "0.10",
                     "price": price, "profit": {"amount": profit, "currency": "USD"},
                     "create_time": "2026-09-20T01:00:00Z"},
                ],
                "positions": [dict(s["positions"][position_id])],
                "account_state": dict(s["acct"]),
            },
        })

    def reject_open(self, op_id: str, crid: str, *, code: int = 5006,
                    message: str = "TRADING_RULE_INSUFFICIENT_MARGIN") -> None:
        s = self.state
        s["operations"][op_id] = {"operation_id": op_id, "status": "rejected",
                                  "client_request_id": crid}
        self.event_push.put_nowait({
            "event_type": "transaction_event",
            "operation_id": op_id,
            "client_request_id": crid,
            "status": "rejected",
            "error_code": code,
            "error_message": message,
            "event_time": "2026-09-20T02:00:00Z",
            "payload": {},
        })


async def _wait(pred, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def make_settings(tmp_path, exn_op_timeout_s: float = 0.5) -> Settings:
    return Settings(
        _env_file=None,
        broker="exness",
        exn_api_key=API_KEY,
        exn_private_key=SEED_B64,
        exn_account_id=ACCOUNT_ID,
        exn_api_base_url="http://fake.rest.local",
        exn_account_is_demo=True,
        exn_op_timeout_s=exn_op_timeout_s,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
        api_token="test-token",
        log_level="DEBUG",
        instruments=["EURUSD", "GBPUSD", "XAUUSD"],
    )


@pytest.fixture
async def fake(tmp_path):
    f = FakeExness()
    settings = make_settings(tmp_path)
    signer = RequestSigner(API_KEY, SEED_B64)
    rest = ExnessRestClient(signer, "http://fake.rest.local", ACCOUNT_ID,
                            http=f.rest_transport(), timeout_s=2.0)
    ws = ExnessWs(signer, f"http://127.0.0.1:{f.ws_port}", ACCOUNT_ID,
                  ["EURUSD", "GBPUSD", "XAUUSD"], max_reconnect_s=1.0)
    broker = ExnessBroker(settings, rest=rest, ws=ws)
    await f.start_ws()
    yield f, broker
    await broker.close()
    await f.stop_ws()


async def test_connect_account_and_conditions(fake) -> None:
    f, broker = fake
    await broker.connect()
    await _wait(lambda: broker.state_baselines >= 2)  # REST snapshot + WS snapshot
    info = await broker.account_info()
    assert info.account_id == ACCOUNT_ID
    assert info.currency == "USD"
    assert info.balance == 10_000.0
    assert info.leverage == 500
    assert info.open_positions == 0
    cond = await broker.instrument_conditions("XAUUSD")
    assert cond.point_digits == 2
    assert cond.contract_size == 100.0
    assert cond.volume_min == 0.01
    # ticks flow through subscribe_ticks (subscribe first, then a tick arrives)
    gen = broker.subscribe_ticks(["EURUSD"])
    f.tick_push.put_nowait({"instrument": "EURUSD", "bid": "1.08341", "ask": "1.08356",
                            "timestamp": "2026-09-20T00:00:02.100Z"})
    tick = await asyncio.wait_for(anext(gen), timeout=5.0)
    assert tick.instrument == "EURUSD" and tick.bid == 1.08341
    await gen.aclose()
    await broker.close()


async def test_open_close_lifecycle_with_event_confirmation(fake) -> None:
    f, broker = fake
    await broker.connect()
    req = OrderRequest(
        instrument="EURUSD", side=Side.BUY, volume=0.10,
        sl=1.079, tp=1.088, client_request_id="t-open-1", comment="phase3",
    )
    ack = await broker.place_order(req)
    assert ack.state is OperationState.ACCEPTED
    assert ack.operation_id == "9000"
    # body sent to the broker is properly formatted (market: no price key)
    sent = f.state["opens"][-1]
    assert sent["crid"] == "t-open-1"
    assert sent["body"] == {"instrument": "EURUSD", "side": "buy", "volume": "0.1",
                            "stop_loss_price": "1.07900", "take_profit_price": "1.08800",
                            "comment": "phase3"}
    # not final yet
    st = await broker.operation_status("9000")
    assert st.state is OperationState.ACCEPTED
    # platform confirms via transaction_event
    f.confirm_open("9000", "t-open-1")
    await _wait(lambda: broker.settled_status("9000") is not None)
    st = await broker.operation_status("9000")
    assert st.state is OperationState.FILLED
    assert st.position_id == "7001"
    assert st.price == 1.08345
    assert st.volume == 0.10
    # local position state updated from the event payload
    positions = await broker.positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.id == "7001" and pos.side is Side.BUY
    assert pos.open_price == 1.08345 and pos.volume == 0.10
    assert pos.sl == 1.079 and pos.tp == 1.088
    # tick-based unrealized P&L
    f.tick_push.put_nowait({"instrument": "EURUSD", "bid": "1.08400", "ask": "1.08415",
                            "timestamp": "2026-09-20T00:01:00.100Z"})
    await _wait(lambda: broker.last_tick("EURUSD") is not None
                and broker.last_tick("EURUSD").bid == 1.08400)
    pos = (await broker.positions())[0]
    assert pos.unrealized_pnl == pytest.approx((1.08400 - 1.08345) * 0.10 * 100_000)
    # close position
    cack = await broker.close_position("7001", "t-close-1", reason="SHUTDOWN")
    assert cack.state is OperationState.ACCEPTED
    assert cack.operation_id == "9001"
    assert f.state["closes"][-1]["pid"] == "7001"
    assert f.state["closes"][-1]["query"] == {}  # full close: no volume param
    f.confirm_close("9001", "t-close-1")
    await _wait(lambda: broker.settled_status("9001") is not None)
    st_close = await broker.operation_status("9001")
    assert st_close.state is OperationState.CLOSED
    assert st_close.position_id == "7001"
    assert st_close.price == 1.08400
    assert await broker.positions() == []
    info = await broker.account_info()
    assert info.balance == 10_000.50
    assert info.open_positions == 0


async def test_rejection_and_idempotency(fake) -> None:
    f, broker = fake
    await broker.connect()
    req = OrderRequest(instrument="EURUSD", side=Side.SELL, volume=0.05,
                       client_request_id="t-open-r")
    ack = await broker.place_order(req)
    f.reject_open(ack.operation_id, "t-open-r")
    await _wait(lambda: broker.settled_status(ack.operation_id) is not None)
    st = await broker.operation_status(ack.operation_id)
    assert st.error_code == "5006"
    assert st.error_message == "TRADING_RULE_INSUFFICIENT_MARGIN"
    # duplicate with the SAME payload -> original (final) result, no new REST call
    opens_before = len(f.state["opens"])
    again = await broker.place_order(req)
    assert again.operation_id == ack.operation_id
    assert again.state is OperationState.REJECTED
    assert len(f.state["opens"]) == opens_before
    # same crid, DIFFERENT payload -> conflict
    req2 = req.model_copy(update={"volume": 0.09})
    with pytest.raises(BrokerError) as e:
        await broker.place_order(req2)
    assert e.value.code == "IDEMPOTENCY_CONFLICT"


async def test_volume_and_precision_validation(fake) -> None:
    _, broker = fake
    await broker.connect()
    with pytest.raises(BrokerError) as e1:
        await broker.place_order(OrderRequest(instrument="EURUSD", side=Side.BUY,
                                              volume=0.005, client_request_id="t-v-1"))
    assert e1.value.code == "VOLUME_OUT_OF_RANGE"
    with pytest.raises(BrokerError) as e2:
        await broker.place_order(OrderRequest(instrument="EURUSD", side=Side.BUY,
                                              volume=0.015, client_request_id="t-v-2"))
    assert e2.value.code == "VOLUME_STEP"
    with pytest.raises(BrokerError) as e3:
        await broker.place_order(OrderRequest(instrument="EURUSD", side=Side.BUY,
                                              volume=0.1, sl=1.079001,
                                              client_request_id="t-v-3"))
    assert e3.value.code == "PRICE_PRECISION"


async def test_operation_status_polling_fallback_when_event_lost(fake) -> None:
    f, broker = fake
    await broker.connect()
    req = OrderRequest(instrument="EURUSD", side=Side.BUY, volume=0.01,
                       client_request_id="t-poll-1")
    ack = await broker.place_order(req)
    op_id = ack.operation_id
    # NO transaction_event will be sent — only the REST operation state.
    f.state["positions"]["7010"] = {
        "position_id": "7010", "direction": "buy", "instrument": "EURUSD",
        "volume": "0.01", "open_price": "1.08350", "state": "open",
        "create_time": "2026-09-20T03:00:00Z",
    }
    f.state["operations"][op_id] = {"operation_id": op_id, "status": "confirmed",
                                    "client_request_id": "t-poll-1"}
    await _wait(lambda: broker.settled_status(op_id) is not None)
    st = await broker.operation_status(op_id)
    assert st.state is OperationState.FILLED
    assert st.position_id == "7010"
    assert st.price == 1.08350
    assert len(await broker.positions()) == 1


async def test_ws_reconnect_triggers_rebaseline(fake) -> None:
    f, broker = fake
    f.drop_events_once = True
    await broker.connect()
    baselines = broker.state_baselines
    # wait for the drop + reconnect cycle (1s backoff) + REST rebaseline
    await _wait(lambda: broker.state_baselines > baselines, timeout=10.0)
    assert broker.state_baselines >= baselines + 1


async def test_history_deals_mapping(fake) -> None:
    _, broker = fake
    await broker.connect()
    deals = await broker.history_deals(datetime(2026, 9, 19, tzinfo=UTC))
    assert len(deals) == 1
    d = deals[0]
    assert d.id == "9001" and d.kind == "open"
    assert d.side is Side.BUY and d.price == 1.08340
    assert d.position_id == "7001"
