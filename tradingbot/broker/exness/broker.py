"""Exness Public Trader API broker adapter.

Implements :class:`~tradingbot.broker.interfaces.BrokerClient` on the official
Exness API (REST + WebSocket), with the semantics the rest of the system
already assumes:

  * mutating calls return an ACK (``accepted``) synchronously; the FINAL state
    is verified through the ``transaction_event`` WebSocket stream, with a
    REST ``operation_status`` polling fallback (operations are retained 24h).
  * idempotency: every mutation carries a client-generated
    ``EXN-IDEMPOTENCY-KEY`` (= ``client_request_id``); duplicates are safe.
  * execution is only recorded as filled after the platform's own
    ``success`` event or a ``confirmed`` operation status — the ACK alone
    never counts as executed.

State model: local position/account state is rebuilt from
``trading_state_snapshot`` messages (sent by the server right after
subscribing to transactions, and re-sent on every (re)connect because the WS
stream has no replay). ``transaction_event`` payloads then update it
incrementally. On any WS gap we refetch the REST snapshot to re-baseline.

Pending-order endpoints (place/modify/cancel) are deliberately not used in
this phase; their contract is verified separately before Phase 6.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from tradingbot.broker.exness.rest import ExnessApiError, ExnessRestClient
from tradingbot.broker.exness.signing import RequestSigner
from tradingbot.broker.exness.ws import MARKER_RECONNECTED, ExnessWs
from tradingbot.broker.interfaces import BrokerClient, BrokerError
from tradingbot.broker.models import (
    AccountInfo,
    Deal,
    InstrumentConditions,
    OperationState,
    OperationStatus,
    OrderRequest,
    Position,
    Side,
    Tick,
)
from tradingbot.core.config import Settings
from tradingbot.core.logging import get_logger
from tradingbot.core.timeutils import utcnow

log = get_logger("tradingbot.broker.exness")

_TICK_SENTINEL = object()


def _dec(value: Any) -> Decimal | None:
    """Exness sends all numbers as plain decimal strings."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _f(value: Any) -> float | None:
    d = _dec(value)
    return None if d is None else float(d)


def _ts(value: Any) -> datetime:
    if value is None:
        return utcnow()
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _fmt(dec: Decimal) -> str:
    """Plain decimal string for the API (no exponent, no thousands)."""
    return format(dec.normalize(), "f")


def _fmt_price(dec: Decimal, point_digits: int) -> str:
    q = dec.quantize(Decimal(1).scaleb(-point_digits))
    return format(q, "f")


def _parse_conditions(raw: dict[str, Any]) -> InstrumentConditions:
    def _dflt(name: str, default: float) -> float:
        v = _f(raw.get(name))
        return default if v is None else v

    return InstrumentConditions(
        instrument=str(raw.get("instrument", "")),
        point_digits=int(raw.get("point_digits", 5)),
        contract_size=_dflt("contract_size", 100_000.0),
        currency=str(raw.get("margin_currency") or raw.get("currency") or "USD"),
        volume_min=_dflt("volume_min", 0.01),
        volume_max=_dflt("volume_max", 100.0),
        volume_step=_dflt("volume_step", 0.01),
        spread=_dflt("spread_typical", 0.0),
    )


@dataclass
class _Pending:
    client_request_id: str
    operation_id: str
    kind: str  # "open" | "close"
    position_id: str | None
    ack_ts: float
    future: asyncio.Future[OperationStatus]


class ExnessBroker(BrokerClient):
    """Official Exness Public Trader API adapter (demo account required)."""

    name = "exness"

    def __init__(
        self,
        settings: Settings,
        rest: ExnessRestClient | None = None,
        ws: ExnessWs | None = None,
    ) -> None:
        if not settings.exn_api_key or not settings.exn_private_key or not settings.exn_account_id:
            raise BrokerError(
                "EXNESS_CONFIG_MISSING",
                "EXN_API_KEY / EXN_PRIVATE_KEY / EXN_ACCOUNT_ID must all be set for BROKER=exness.",
            )
        base_url = (settings.exn_api_base_url or "https://api.exness.com").rstrip("/")
        self._settings = settings
        self._instruments = list(settings.instruments)
        self._rest = rest or ExnessRestClient(
            RequestSigner(settings.exn_api_key, settings.exn_private_key),
            base_url,
            settings.exn_account_id,
            timeout_s=settings.exn_rest_timeout_s,
        )
        self._ws = ws or ExnessWs(
            RequestSigner(settings.exn_api_key, settings.exn_private_key),
            base_url,
            settings.exn_account_id,
            self._instruments,
            max_reconnect_s=settings.exn_ws_reconnect_max_s,
        )
        # local state
        self._conditions: dict[str, InstrumentConditions] = {}
        self._raw_conditions: dict[str, dict[str, Any]] = {}
        self._positions: dict[str, Position] = {}
        self._last_tick: dict[str, Tick] = {}
        self._account_currency: str = "USD"
        self._leverage: int = 0
        self._account_state: dict[str, float] = {}  # balance/equity/used_margin
        self._pending_by_op: dict[str, _Pending] = {}
        self._op_by_crid: dict[str, str] = {}
        self._final_by_op: dict[str, OperationStatus] = {}
        self._seen: dict[str, tuple[str, str]] = {}  # crid -> (fingerprint, op_id)
        self._tick_subs: list[tuple[set[str], asyncio.Queue[Any]]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._connected = False
        self._closed = False
        self.state_baselines = 0

    # ------------------------------------------------------------------ setup
    async def connect(self) -> None:
        if self._connected:
            return
        self._connected = True
        log.info(
            "exness connecting (DEMO per EXN_ACCOUNT_IS_DEMO=true)",
            account=self._settings.exn_account_id,
            instruments=self._instruments,
        )
        try:
            account_raw = await self._rest.get_account()
            self._extract_account_info(account_raw)
            await self._rest.get_limits()  # also configures the rate limiter
            for inst in self._instruments:
                raw = await self._rest.get_instrument_conditions(inst)
                self._apply_conditions(raw)
            snap = await self._rest.get_snapshot()
            self._apply_snapshot(snap)
            await self._ws.start()
            self._tasks = [
                asyncio.create_task(self._event_loop()),
                asyncio.create_task(self._tick_loop()),
                asyncio.create_task(self._watchdog()),
            ]
            log.info(
                "exness connected",
                positions=len(self._positions),
                currency=self._account_currency,
                leverage=self._leverage,
            )
        except Exception:
            await self._cleanup_after_failed_connect()
            raise

    async def _cleanup_after_failed_connect(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        if self._ws.ticks_connected or self._ws.events_connected:
            await self._ws.stop()
        self._connected = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sub in list(self._tick_subs):
            with contextlib.suppress(asyncio.QueueFull):
                sub[1].put_nowait(_TICK_SENTINEL)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        await self._ws.stop()
        await self._rest.close()
        self._connected = False
        log.info("exness broker closed")

    # ------------------------------------------------------------- conditions
    def _apply_conditions(self, raw: dict[str, Any]) -> None:
        cond = _parse_conditions(raw)
        if cond.instrument:
            self._conditions[cond.instrument] = cond
            self._raw_conditions[cond.instrument] = raw

    def _condition(self, instrument: str) -> InstrumentConditions:
        cond = self._conditions.get(instrument)
        if cond is None:
            raise BrokerError("NO_CONDITIONS", f"no instrument conditions cached for {instrument}")
        return cond

    # ------------------------------------------------------------ state apply
    def _extract_account_info(self, raw: Any) -> None:
        data = raw.get("account", raw) if isinstance(raw, dict) else {}
        if not isinstance(data, dict):
            data = {}
        self._account_currency = str(data.get("currency") or self._account_currency)
        lev = _f(data.get("leverage"))
        if lev:
            self._leverage = int(lev)
        log.info("exness account details", fields=sorted(str(k) for k in data))

    def _apply_snapshot(self, snap: dict[str, Any]) -> None:
        """Replace local state from a full trading_state_snapshot payload."""
        positions: dict[str, Position] = {}
        sltp: dict[str, tuple[float | None, float | None]] = {}
        for order in snap.get("orders", []):
            if order.get("order_type") == "sltp" and order.get("position_id"):
                sltp[str(order["position_id"])] = (
                    _f(order.get("stop_loss_price")),
                    _f(order.get("take_profit_price")),
                )
        for p in snap.get("positions", []):
            if p.get("state") != "open":
                continue
            pid = str(p.get("position_id", ""))
            if not pid:
                continue
            sl, tp = sltp.get(pid, (None, None))
            positions[pid] = Position(
                id=pid,
                instrument=str(p.get("instrument", "")),
                side=Side(str(p.get("direction", "buy"))),
                volume=_f(p.get("volume")) or 0.0,
                open_price=_f(p.get("open_price")) or 0.0,
                sl=sl,
                tp=tp,
                open_ts=_ts(p.get("create_time")),
            )
        self._positions = positions
        acct = snap.get("account_state") or {}
        self._apply_account_state(acct)
        self.state_baselines += 1
        log.info("exness state baseline applied", positions=len(positions),
                 baselines=self.state_baselines)

    def _apply_account_state(self, acct: dict[str, Any]) -> None:
        if not acct:
            return
        for key in ("balance", "equity", "used_margin"):
            v = _f(acct.get(key))
            if v is not None:
                self._account_state[key] = v
        lev = _f(acct.get("leverage"))
        if lev:
            self._leverage = int(lev)

    def _apply_payload_entities(self, payload: dict[str, Any]) -> None:
        """Incremental update from a transaction_event payload."""
        acct = payload.get("account_state")
        if acct:
            self._apply_account_state(acct)
        for p in payload.get("positions", []):
            pid = str(p.get("position_id", ""))
            if not pid:
                continue
            if p.get("state") == "open":
                self._positions[pid] = Position(
                    id=pid,
                    instrument=str(p.get("instrument", "")),
                    side=Side(str(p.get("direction", "buy"))),
                    volume=_f(p.get("volume")) or 0.0,
                    open_price=_f(p.get("open_price")) or 0.0,
                    sl=None,
                    tp=None,
                    open_ts=_ts(p.get("create_time")),
                )
            elif p.get("state") == "closed":
                self._positions.pop(pid, None)
        for order in payload.get("orders", []):
            if order.get("order_type") == "sltp" and order.get("position_id"):
                pid = str(order["position_id"])
                if pid in self._positions:
                    pos = self._positions[pid]
                    pos.sl = _f(order.get("stop_loss_price")) if _f(order.get("stop_loss_price")) is not None else pos.sl
                    pos.tp = _f(order.get("take_profit_price")) if _f(order.get("take_profit_price")) is not None else pos.tp

    # ------------------------------------------------------------- event loop
    async def _event_loop(self) -> None:
        assert self._ws is not None
        while True:
            msg = await self._ws.event_queue.get()
            try:
                marker = msg.get("__marker__")
                if marker == MARKER_RECONNECTED:
                    await self._rebaseline()
                    continue
                etype = msg.get("event_type")
                if etype == "trading_state_snapshot":
                    self._apply_snapshot(msg.get("payload") or {})
                elif etype == "transaction_event":
                    self._handle_transaction_event(msg)
                elif etype == "account_state_event":
                    self._apply_account_state((msg.get("payload") or {}).get("account_state") or {})
                elif etype == "instrument_event":
                    payload = msg.get("payload") or {}
                    if payload.get("action") != "del":
                        self._apply_conditions(payload)
                else:
                    log.debug("exness unhandled event", event_type=etype,
                              keys=sorted(str(k) for k in msg))
            except Exception:
                log.exception("exness event handling error", detail=str(msg)[:500])

    async def _rebaseline(self) -> None:
        """WS stream gap detected — refetch the REST snapshot as new baseline."""
        try:
            snap = await self._rest.get_snapshot()
            self._apply_snapshot(snap)
        except ExnessApiError as e:
            log.error("exness rebaseline failed", code=e.code, detail=e.message)

    def _handle_transaction_event(self, msg: dict[str, Any]) -> None:
        op_id = msg.get("operation_id")
        crid = msg.get("client_request_id")
        status = msg.get("status")
        payload = msg.get("payload") or {}
        self._apply_payload_entities(payload)

        pending: _Pending | None = None
        if op_id:
            pending = self._pending_by_op.get(str(op_id))
        if pending is None and crid:
            known_op = self._op_by_crid.get(str(crid))
            if known_op:
                pending = self._pending_by_op.get(known_op)

        if status == "success":
            final = self._build_success_status(msg, payload, pending)
        elif status in ("rejected", "failed"):
            final = OperationStatus(
                operation_id=str(op_id or ""),
                client_request_id=str(crid or ""),
                state=OperationState.REJECTED,
                error_code=str(msg.get("error_code") or ""),
                error_message=str(msg.get("error_message") or f"processing {status}"),
                ts=_ts(msg.get("event_time")),
            )
        else:
            log.warning("exness unknown transaction_event status", status=status,
                        op_id=op_id, crid=crid)
            return

        if pending is not None:
            if pending.kind == "close" and final.state is OperationState.FILLED:
                final = final.model_copy(update={"state": OperationState.CLOSED})
            self._settle(str(final.operation_id), final, pending)
            log.info("exness operation final (via event)", op_id=final.operation_id,
                     crid=pending.client_request_id, state=final.state.value,
                     position_id=final.position_id, price=final.price,
                     error=final.error_code)
        else:
            # Platform-generated (SL/TP/close-by/balance) — state already applied.
            log.info("exness transaction event (no pending)", op_id=op_id, status=status,
                     error=final.error_code)

    def _settle(self, op_id: str, final: OperationStatus, pend: _Pending | None = None) -> None:
        """Cache the final status, drop pending state, resolve the future."""
        if not op_id:
            return
        self._final_by_op[op_id] = final
        if pend is None:
            pend = self._pending_by_op.pop(op_id, None)
        else:
            self._pending_by_op.pop(op_id, None)
        if pend is not None and not pend.future.done():
            pend.future.set_result(final)

    def settled_status(self, operation_id: str) -> OperationStatus | None:
        """Final status if the operation has settled locally, else None."""
        return self._final_by_op.get(operation_id)

    def _build_success_status(
        self, msg: dict[str, Any], payload: dict[str, Any], pending: _Pending | None
    ) -> OperationStatus:
        deal: dict[str, Any] | None = None
        position_id: str | None = pending.position_id if pending else None
        wanted = "close" if (pending and pending.kind == "close") else "open"
        for d in payload.get("deals", []):
            if d.get("type") == wanted and deal is None:
                deal = d
        if deal is None:
            for d in payload.get("deals", []):
                if d.get("type") in ("open", "close"):
                    deal = d
                    break
        if position_id is None:
            for p in payload.get("positions", []):
                if p.get("state") == "open" and p.get("position_id"):
                    position_id = str(p["position_id"])
        return OperationStatus(
            operation_id=str(msg.get("operation_id") or ""),
            client_request_id=str(msg.get("client_request_id") or ""),
            state=OperationState.FILLED,
            price=_f(deal.get("price")) if deal else None,
            volume=_f(deal.get("volume")) if deal else None,
            position_id=position_id,
            ts=_ts(msg.get("event_time")),
        )

    # --------------------------------------------------------------- tick loop
    async def _tick_loop(self) -> None:
        assert self._ws is not None
        while True:
            msg = await self._ws.tick_queue.get()
            if "__marker__" in msg:
                log.info("exness tick stream marker", marker=msg.get("__marker__"))
                continue
            tick = self._parse_tick(msg)
            if tick is None:
                continue
            self._last_tick[tick.instrument] = tick
            for instruments, q in list(self._tick_subs):
                if tick.instrument not in instruments:
                    continue
                try:
                    q.put_nowait(tick)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                        q.put_nowait(tick)
                    except (asyncio.QueueFull, asyncio.QueueEmpty):
                        pass
                    log.warning("tick subscriber queue full, dropped tick", instrument=tick.instrument)

    @staticmethod
    def _parse_tick(msg: dict[str, Any]) -> Tick | None:
        instrument = msg.get("instrument")
        bid = _f(msg.get("bid"))
        ask = _f(msg.get("ask"))
        if not instrument or bid is None or ask is None:
            log.warning("exness malformed tick", detail=str(msg)[:200])
            return None
        return Tick(instrument=str(instrument), bid=bid, ask=ask, ts=_ts(msg.get("timestamp")))

    # ---------------------------------------------------------------- watchdog
    async def _watchdog(self) -> None:
        """Poll operation_status for ACKs whose event never arrived.

        The WS transaction_event is the primary confirmation path; this is the
        documented fallback (operations retained 24h). Runs only while there
        is pending work, so the idle cost is one sleep per cycle.
        """
        timeout_s = self._settings.exn_op_timeout_s
        while True:
            await asyncio.sleep(2.0)
            now = time.monotonic()
            for op_id, pend in list(self._pending_by_op.items()):
                if now - pend.ack_ts < timeout_s:
                    continue
                try:
                    status = await self._poll_operation(op_id)
                except ExnessApiError as e:
                    if e.code in (3014, "3014"):  # REQUEST_OPERATION_NOT_FOUND
                        self._fail_pending(op_id, "REQUEST_OPERATION_NOT_FOUND",
                                           "operation not found (expired or unknown)")
                    else:
                        log.warning("exness op poll failed, will retry", op_id=op_id,
                                    code=e.code, detail=e.message)
                    continue
                if status.state in (OperationState.FILLED, OperationState.CLOSED, OperationState.REJECTED):
                    self._settle(op_id, status)
                    log.info("exness operation final (via polling)", op_id=op_id,
                             state=status.state.value, error=status.error_code)

    async def _poll_operation(self, op_id: str) -> OperationStatus:
        data = await self._rest.get_operation_status(op_id)
        pend = self._pending_by_op.get(op_id)
        status = str(data.get("status", ""))
        if status == "pending":
            return OperationStatus(
                operation_id=op_id,
                client_request_id=pend.client_request_id if pend else "",
                state=OperationState.ACCEPTED,
                ts=utcnow(),
            )
        if status == "confirmed":
            # Operation status has no fill details: reconcile from the
            # snapshot (positions/deals) for price + position id. Applying the
            # snapshot also refreshes local state (it is the authoritative
            # current state per the API docs).
            snap = await self._rest.get_snapshot()
            self._apply_snapshot(snap)
            return self._status_from_snapshot(op_id, pend, snap)
        if status in ("rejected", "failed"):
            return OperationStatus(
                operation_id=op_id,
                client_request_id=str(data.get("client_request_id") or ""),
                state=OperationState.REJECTED,
                error_code=str(data.get("error_code") or status.upper()),
                error_message=str(data.get("error_message") or ""),
                ts=_ts(data.get("updated_at")),
            )
        return OperationStatus(
            operation_id=op_id,
            client_request_id=pend.client_request_id if pend else "",
            state=OperationState.UNKNOWN,
            error_message=f"unexpected operation status: {status}",
            ts=utcnow(),
        )

    def _status_from_snapshot(
        self, op_id: str, pend: _Pending | None, snap: dict[str, Any]
    ) -> OperationStatus:
        position_id = pend.position_id if pend else None
        if position_id is None and pend is not None and pend.kind == "open":
            # find the position created for this op: newest open position
            open_pos = [p for p in snap.get("positions", []) if p.get("state") == "open"]
            if open_pos:
                open_pos.sort(key=lambda p: str(p.get("create_time", "")), reverse=True)
                position_id = str(open_pos[0].get("position_id", "")) or None
        price = None
        if position_id:
            for p in snap.get("positions", []):
                if str(p.get("position_id", "")) == position_id:
                    price = _f(p.get("open_price"))
                    break
        state = OperationState.FILLED if pend and pend.kind == "open" else OperationState.CLOSED
        return OperationStatus(
            operation_id=op_id,
            client_request_id=pend.client_request_id if pend else "",
            state=state,
            price=price,
            position_id=position_id,
            ts=utcnow(),
        )

    def _fail_pending(self, op_id: str, code: str, message: str) -> None:
        pend = self._pending_by_op.pop(op_id, None)
        if pend is None:
            return
        final = OperationStatus(
            operation_id=op_id,
            client_request_id=pend.client_request_id,
            state=OperationState.REJECTED,
            error_code=code,
            error_message=message,
            ts=utcnow(),
        )
        self._final_by_op[op_id] = final
        if not pend.future.done():
            pend.future.set_result(final)

    # ------------------------------------------------------------ interface: ticks
    def subscribe_ticks(self, instruments: list[str]) -> AsyncIterator[Tick]:
        if self._closed:
            raise BrokerError("BROKER_CLOSED", "broker is closed")
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=10_000)
        self._tick_subs.append((set(instruments), q))
        return self._tick_stream(q)

    async def _tick_stream(self, q: asyncio.Queue[Any]) -> AsyncIterator[Tick]:
        try:
            while True:
                item = await q.get()
                if item is _TICK_SENTINEL:
                    return
                yield item
        finally:
            self._tick_subs = [s for s in self._tick_subs if s[1] is not q]

    # ----------------------------------------------------------- interface: info
    async def account_info(self) -> AccountInfo:
        balance = self._account_state.get("balance", 0.0)
        equity = self._account_state.get("equity", balance)
        used = self._account_state.get("used_margin", 0.0)
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return AccountInfo(
            account_id=self._settings.exn_account_id or "",
            currency=self._account_currency,
            balance=balance,
            equity=equity,
            free_margin=max(equity - used, 0.0),
            margin_level=(equity / used * 100.0) if used > 0 else None,
            leverage=self._leverage,
            open_positions=len(self._positions),
            unrealized_pnl=unrealized,
            ts=utcnow(),
        )

    async def instrument_conditions(self, instrument: str) -> InstrumentConditions:
        if instrument not in self._conditions:
            raw = await self._rest.get_instrument_conditions(instrument)
            self._apply_conditions(raw)
        return self._condition(instrument)

    async def positions(self) -> list[Position]:
        out = []
        for pos in self._positions.values():
            out.append(self._with_pnl(pos))
        return out

    def _with_pnl(self, pos: Position) -> Position:
        tick = self._last_tick.get(pos.instrument)
        if tick is None or pos.volume <= 0:
            pos.unrealized_pnl = 0.0
            return pos
        cond = self._conditions.get(pos.instrument)
        contract = cond.contract_size if cond else 100_000.0
        if pos.side is Side.BUY:
            pos.unrealized_pnl = (tick.bid - pos.open_price) * pos.volume * contract
        else:
            pos.unrealized_pnl = (pos.open_price - tick.ask) * pos.volume * contract
        return pos

    # ---------------------------------------------------------- interface: orders
    def _validate_order(self, req: OrderRequest) -> tuple[Decimal, str]:
        cond = self._condition(req.instrument)
        raw = self._raw_conditions.get(req.instrument, {})
        if raw.get("trade_mode") == "trading_disabled":
            raise BrokerError("TRADING_DISABLED", f"{req.instrument} is trading_disabled")
        vol = Decimal(str(req.volume))
        vmin, vmax, vstep = Decimal(str(cond.volume_min)), Decimal(str(cond.volume_max)), Decimal(str(cond.volume_step))
        if vol < vmin or vol > vmax:
            raise BrokerError("VOLUME_OUT_OF_RANGE",
                              f"volume {req.volume} outside [{cond.volume_min}, {cond.volume_max}]")
        if vstep > 0 and vol % vstep != 0:
            raise BrokerError("VOLUME_STEP",
                              f"volume {req.volume} not a multiple of step {cond.volume_step}")
        for label, price in (("price", req.price), ("sl", req.sl), ("tp", req.tp)):
            if price is not None:
                exp = Decimal(str(price)).as_tuple().exponent
                if isinstance(exp, int) and exp < -cond.point_digits:
                    raise BrokerError("PRICE_PRECISION",
                                      f"{label} has more than {cond.point_digits} decimal digits")
        return vol, _fmt_price(Decimal(str(req.price)), cond.point_digits) if req.price is not None else ""

    def _idempotency_check(self, req: OrderRequest, fingerprint: str) -> str | None:
        """Returns the original operation_id if this crid+payload was seen."""
        prev = self._seen.get(req.client_request_id)
        if prev is None:
            return None
        prev_fp, prev_op = prev
        if prev_fp != fingerprint:
            raise BrokerError(
                "IDEMPOTENCY_CONFLICT",
                f"client_request_id {req.client_request_id} already used with a different payload",
            )
        return prev_op

    async def place_order(self, req: OrderRequest) -> OperationStatus:
        self._require_open()
        vol, price_str = self._validate_order(req)
        fingerprint = req.payload_fingerprint()
        existing_op = self._idempotency_check(req, fingerprint)
        if existing_op is not None:
            final = self._final_by_op.get(existing_op)
            if final is not None:
                return final
            return OperationStatus(
                operation_id=existing_op,
                client_request_id=req.client_request_id,
                state=OperationState.ACCEPTED,
                ts=utcnow(),
            )

        cond = self._condition(req.instrument)
        body_kwargs: dict[str, str | None] = {
            "price": price_str or None,
            "stop_loss_price": _fmt_price(Decimal(str(req.sl)), cond.point_digits) if req.sl is not None else None,
            "take_profit_price": _fmt_price(Decimal(str(req.tp)), cond.point_digits) if req.tp is not None else None,
            "comment": req.comment,
        }
        log.info("exness open_position", instrument=req.instrument, side=req.side.value,
                 volume=_fmt(vol), price=price_str or "market", crid=req.client_request_id)
        try:
            ack = await self._rest.open_position(
                req.instrument, req.side.value, _fmt(vol),
                client_request_id=req.client_request_id, **body_kwargs,
            )
        except ExnessApiError as e:
            raise BrokerError(str(e.code) or "EXNESS_ERROR", e.message) from e

        op_id = str(ack.get("operation_id") or "")
        if not op_id:
            raise BrokerError("BAD_ACK", f"open_position ACK missing operation_id: {ack}")
        future: asyncio.Future[OperationStatus] = asyncio.get_running_loop().create_future()
        self._pending_by_op[op_id] = _Pending(
            client_request_id=req.client_request_id,
            operation_id=op_id,
            kind="open",
            position_id=None,
            ack_ts=time.monotonic(),
            future=future,
        )
        self._op_by_crid[req.client_request_id] = op_id
        self._seen[req.client_request_id] = (fingerprint, op_id)
        log.info("exness open_position ACK", op_id=op_id, crid=req.client_request_id)
        return OperationStatus(
            operation_id=op_id,
            client_request_id=req.client_request_id,
            state=OperationState.ACCEPTED,
            ts=utcnow(),
        )

    async def close_position(self, position_id: str, client_request_id: str,
                             reason: str = "MANUAL") -> OperationStatus:
        self._require_open()
        if position_id not in self._positions:
            # Double-check against the broker before failing locally.
            try:
                snap = await self._rest.get_snapshot()
                self._apply_snapshot(snap)
            except ExnessApiError:
                pass
        if position_id not in self._positions:
            raise BrokerError("POSITION_NOT_FOUND", f"no open position {position_id}")

        prev = self._seen.get(client_request_id)
        if prev is not None:
            prev_fp, prev_op = prev
            if prev_fp != f"close:{position_id}":
                raise BrokerError("IDEMPOTENCY_CONFLICT",
                                  f"client_request_id {client_request_id} already used with a different payload")
            final = self._final_by_op.get(prev_op)
            if final is not None:
                return final
            return OperationStatus(
                operation_id=prev_op, client_request_id=client_request_id,
                state=OperationState.ACCEPTED, ts=utcnow(),
            )

        log.info("exness close_position", position_id=position_id, reason=reason,
                 crid=client_request_id)
        try:
            ack = await self._rest.close_position(position_id, client_request_id)
        except ExnessApiError as e:
            raise BrokerError(str(e.code) or "EXNESS_ERROR", e.message) from e

        op_id = str(ack.get("operation_id") or "")
        if not op_id:
            raise BrokerError("BAD_ACK", f"close_position ACK missing operation_id: {ack}")
        future: asyncio.Future[OperationStatus] = asyncio.get_running_loop().create_future()
        self._pending_by_op[op_id] = _Pending(
            client_request_id=client_request_id,
            operation_id=op_id,
            kind="close",
            position_id=position_id,
            ack_ts=time.monotonic(),
            future=future,
        )
        self._op_by_crid[client_request_id] = op_id
        self._seen[client_request_id] = (f"close:{position_id}", op_id)
        log.info("exness close_position ACK", op_id=op_id, crid=client_request_id)
        return OperationStatus(
            operation_id=op_id,
            client_request_id=client_request_id,
            state=OperationState.ACCEPTED,
            ts=utcnow(),
        )

    _FINAL_STATES = (
        OperationState.FILLED,
        OperationState.CLOSED,
        OperationState.REJECTED,
    )

    async def operation_status(self, operation_id: str) -> OperationStatus:
        final = self._final_by_op.get(operation_id)
        if final is not None:
            return final
        pend = self._pending_by_op.get(operation_id)
        if pend is not None and not pend.future.done():
            elapsed = time.monotonic() - pend.ack_ts
            if elapsed < self._settings.exn_op_timeout_s:
                return OperationStatus(
                    operation_id=operation_id,
                    client_request_id=pend.client_request_id,
                    state=OperationState.ACCEPTED,
                    ts=utcnow(),
                )
            # Timeout elapsed without an event: poll REST (documented fallback).
            try:
                status = await self._poll_operation(operation_id)
            except ExnessApiError:
                return OperationStatus(
                    operation_id=operation_id,
                    client_request_id=pend.client_request_id,
                    state=OperationState.ACCEPTED,
                    ts=utcnow(),
                )
            if status.state in self._FINAL_STATES:
                self._settle(operation_id, status, pend)
            return status
        # Unknown to us (or already settled before this instance): ask broker.
        try:
            status = await self._poll_operation(operation_id)
        except ExnessApiError as e:
            if e.code in (3014, "3014"):
                return OperationStatus(
                    operation_id=operation_id, client_request_id="",
                    state=OperationState.UNKNOWN,
                    error_code="REQUEST_OPERATION_NOT_FOUND",
                    error_message="operation unknown or expired (24h retention)",
                    ts=utcnow(),
                )
            raise BrokerError(str(e.code) or "EXNESS_ERROR", e.message) from e
        if status.state in self._FINAL_STATES:
            self._settle(operation_id, status)
        return status

    # ----------------------------------------------------------- interface: history
    async def history_deals(self, since: datetime | None = None) -> list[Deal]:
        self._require_open()
        if since is None:
            since = utcnow() - timedelta(hours=24)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        deals: list[Deal] = []
        cursor: str | None = None
        for _ in range(10):  # hard cap: 10 pages x 200
            data = await self._rest.get_deals_history(
                from_dt=since, limit=200, cursor=cursor,
            )
            for item in data.get("items", []):
                deal = item.get("deal") or {}
                dtype = deal.get("type")
                if dtype not in ("open", "close"):
                    continue
                if not deal.get("instrument") or not deal.get("price"):
                    continue
                direction = deal.get("direction")
                if direction not in ("buy", "sell"):
                    continue
                profit = (deal.get("profit") or {}).get("amount")
                deals.append(Deal(
                    id=str(deal.get("deal_id", "")),
                    operation_id=None,
                    position_id=str(deal.get("position_id", "") or ""),
                    instrument=str(deal["instrument"]),
                    side=Side(str(direction)),
                    volume=_f(deal.get("volume")) or 0.0,
                    price=_f(deal["price"]) or 0.0,
                    pnl=_f(profit),
                    commission=_f(deal.get("commission")) or 0.0,
                    kind="open" if dtype == "open" else "close",
                    ts=_ts(item.get("event_time") or deal.get("create_time")),
                ))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        deals.sort(key=lambda d: d.ts)
        return deals

    # ------------------------------------------------------------------ helpers
    def _require_open(self) -> None:
        if self._closed:
            raise BrokerError("BROKER_CLOSED", "broker is closed")
        if not self._connected:
            raise BrokerError("NOT_CONNECTED", "broker is not connected")

    # ------------------------------------------------------------- test helpers
    def last_tick(self, instrument: str) -> Tick | None:
        return self._last_tick.get(instrument)

    def pending_count(self) -> int:
        return len(self._pending_by_op)
