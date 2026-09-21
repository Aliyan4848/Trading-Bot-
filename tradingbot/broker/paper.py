"""Paper broker: synthetic market + simulated fills.

Purpose
-------
Development and testing of the *entire* pipeline (data -> strategy -> risk ->
execution -> dashboard) without broker credentials, plus deterministic tests
of execution semantics.

Semantics deliberately mirror the official Exness API so adapters are
interchangeable:
  * async fills: ``place_order``/``close_position`` return an ACK
    (``accepted``); the final ``filled``/``closed`` state arrives after a
    configurable ack delay and must be verified via ``operation_status``.
  * idempotency: ``(client_request_id, payload_fingerprint)`` — duplicates
    return the original operation and are never re-executed; same key with a
    different payload is rejected (IDEMPOTENCY_CONFLICT).
  * SL/TP are evaluated on every tick; if both levels are reachable in the
    same tick the stop-loss wins (conservative).

Fills are simulated: market orders fill at the current ask (buy) / bid (sell)
plus configured slippage (bps); SL/TP fills are idealized at the exact level
(real brokers can fill worse in fast markets — the backtester models that).

This broker is for development only and is clearly labelled in every log line.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

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

log = get_logger("tradingbot.broker.paper")

#: Synthetic-market parameters per instrument.
PAPER_INSTRUMENTS: dict[str, dict] = {
    "EURUSD": {"start": 1.08500, "spread": 0.00012, "sigma": 1.2e-5, "point_digits": 5, "contract": 100_000.0},
    "GBPUSD": {"start": 1.27000, "spread": 0.00015, "sigma": 1.5e-5, "point_digits": 5, "contract": 100_000.0},
    "XAUUSD": {"start": 2400.00, "spread": 0.35, "sigma": 0.00015, "point_digits": 2, "contract": 100.0},
}


@dataclass
class _InstrumentState:
    conditions: InstrumentConditions
    last_bid: float
    last_ask: float
    rng: random.Random = field(default_factory=random.Random)
    last_ts: datetime = field(default_factory=utcnow)


class PaperBroker(BrokerClient):
    name = "paper"

    def __init__(self, settings: Settings, instruments: dict[str, dict] | None = None) -> None:
        self._settings = settings
        self._instruments_spec = instruments or PAPER_INSTRUMENTS
        self._state: dict[str, _InstrumentState] = {}
        self._queues: list[asyncio.Queue[Tick | None]] = []
        self._tasks: list[asyncio.Task] = []
        self._positions: dict[str, Position] = {}
        self._ops: dict[str, OperationStatus] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}  # key -> (fingerprint, operation_id)
        self._deals: list[Deal] = []
        self._balance = settings.paper_start_balance
        self._stop = asyncio.Event()
        self._connected = False
        self._counter = itertools.count(1)

    # ------------------------------------------------------------------ setup
    def _build_states(self) -> None:
        for inst in self._settings.instruments:
            spec = self._instruments_spec.get(inst)
            if spec is None:
                raise BrokerError(
                    "PAPER_INSTRUMENT_UNKNOWN",
                    f"instrument {inst!r} has no paper-market specification; "
                    f"known: {sorted(self._instruments_spec)}",
                )
            self._state[inst] = _InstrumentState(
                conditions=InstrumentConditions(
                    instrument=inst,
                    point_digits=spec["point_digits"],
                    contract_size=spec["contract"],
                    volume_min=0.01,
                    volume_max=100.0,
                    volume_step=0.01,
                    spread=spec["spread"],
                ),
                last_bid=spec["start"],
                last_ask=spec["start"] + spec["spread"],
                rng=random.Random((self._settings.paper_random_seed or 0) + sum(map(ord, inst))),
            )

    async def connect(self) -> None:
        if self._connected:
            return
        self._build_states()
        self._stop.clear()
        for inst in self._settings.instruments:
            self._tasks.append(asyncio.create_task(self._tick_loop(inst), name=f"paper-tick-{inst}"))
        self._connected = True
        log.info(
            "PAPER broker connected (SYNTHETIC MARKET, SIMULATED FILLS — development only)",
            instruments=list(self._settings.instruments),
            balance=self._balance,
        )

    async def close(self) -> None:
        self._stop.set()
        for q in self._queues:
            q.put_nowait(None)
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._queues.clear()
        self._connected = False
        log.info("PAPER broker closed")

    # ------------------------------------------------------------- market data
    async def _tick_loop(self, instrument: str) -> None:
        st = self._state[instrument]
        spec = self._instruments_spec[instrument]
        interval = self._settings.tick_interval_ms / 1000.0
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            if self._stop.is_set():
                break
            now = utcnow()
            dt = max((now - st.last_ts).total_seconds(), 1e-6)
            st.last_ts = now
            # geometric random walk, zero drift
            z = st.rng.gauss(0.0, 1.0)
            price = st.last_bid * math.exp(spec["sigma"] * math.sqrt(dt) * z)
            bid = round(price, st.conditions.point_digits)
            ask = round(bid + spec["spread"], st.conditions.point_digits)
            st.last_bid, st.last_ask = bid, ask
            tick = Tick(instrument=instrument, bid=bid, ask=ask, ts=now)
            self._emit(tick)
            self._check_sl_tp(tick)

    def _emit(self, tick: Tick) -> None:
        for q in self._queues:
            try:
                q.put_nowait(tick)
            except asyncio.QueueFull:  # pragma: no cover - bounded consumer
                try:
                    q.get_nowait()
                    q.put_nowait(tick)
                except asyncio.QueueFull:
                    pass

    async def force_price(self, instrument: str, bid: float, ask: float) -> Tick:
        """Test hook: publish a deterministic tick (bypasses the random walk)."""
        st = self._state[instrument]
        st.last_bid, st.last_ask = bid, ask
        tick = Tick(instrument=instrument, bid=bid, ask=ask, ts=utcnow())
        self._emit(tick)
        self._check_sl_tp(tick)
        return tick

    async def account_info(self) -> AccountInfo:
        await self._require_connected()
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        equity = self._balance + unrealized
        used = self._used_margin()
        free_margin = equity - used
        margin_level = (equity / used * 100.0) if used > 0 else None
        return AccountInfo(
            account_id=f"paper-{self._settings.environment}",
            currency="USD",
            balance=round(self._balance, 2),
            equity=round(equity, 2),
            free_margin=round(free_margin, 2),
            margin_level=round(margin_level, 2) if margin_level is not None else None,
            leverage=self._settings.paper_leverage,
            open_positions=len(self._positions),
            unrealized_pnl=round(unrealized, 2),
            ts=utcnow(),
        )

    async def instrument_conditions(self, instrument: str) -> InstrumentConditions:
        await self._require_connected()
        st = self._state.get(instrument)
        if st is None:
            raise BrokerError("MARKET_INSTRUMENT_NOT_FOUND", instrument)
        return st.conditions

    async def positions(self) -> list[Position]:
        await self._require_connected()
        return list(self._positions.values())

    def subscribe_ticks(self, instruments: list[str]) -> AsyncIterator[Tick]:
        wanted = set(instruments)

        async def _stream() -> AsyncIterator[Tick]:
            q: asyncio.Queue[Tick | None] = asyncio.Queue(maxsize=4096)
            self._queues.append(q)
            try:
                while True:
                    tick = await q.get()
                    if tick is None:
                        break
                    if tick.instrument in wanted:
                        yield tick
            finally:
                self._queues.remove(q)

        return _stream()

    # ------------------------------------------------------------------ orders
    async def place_order(self, req: OrderRequest) -> OperationStatus:
        await self._require_connected()
        fingerprint = req.payload_fingerprint()

        # Idempotency (mirrors Exness EXN-IDEMPOTENCY-KEY semantics).
        if req.client_request_id in self._idempotency:
            fp, op_id = self._idempotency[req.client_request_id]
            if fp == fingerprint:
                log.info("PAPER duplicate order idempotently returned", client_request_id=req.client_request_id)
                return self._ops[op_id].model_copy(deep=True)
            return OperationStatus(
                operation_id="",
                client_request_id=req.client_request_id,
                state=OperationState.REJECTED,
                error_code="IDEMPOTENCY_CONFLICT",
                error_message="same client_request_id with a different payload",
                ts=utcnow(),
            )

        # Synchronous validation (before the ACK, like the real API).
        err = self._validate(req)
        if err is not None:
            op_id = self._next_id()
            status = OperationStatus(
                operation_id=op_id,
                client_request_id=req.client_request_id,
                state=OperationState.REJECTED,
                error_code=err[0],
                error_message=err[1],
                ts=utcnow(),
            )
            self._ops[op_id] = status
            self._idempotency[req.client_request_id] = (fingerprint, op_id)
            return status

        op_id = self._next_id()
        status = OperationStatus(
            operation_id=op_id,
            client_request_id=req.client_request_id,
            state=OperationState.ACCEPTED,
            ts=utcnow(),
        )
        self._ops[op_id] = status
        self._idempotency[req.client_request_id] = (fingerprint, op_id)
        asyncio.create_task(self._execute_open(op_id, req), name=f"paper-fill-{op_id}")
        log.info("PAPER order accepted", op_id=op_id, instrument=req.instrument,
                 side=req.side.value, volume=req.volume, sl=req.sl, tp=req.tp)
        return status

    async def close_position(self, position_id: str, client_request_id: str,
                             reason: str = "MANUAL") -> OperationStatus:
        await self._require_connected()
        if client_request_id in self._idempotency:
            fp, op_id = self._idempotency[client_request_id]
            if fp == f"close:{position_id}:{reason}":
                return self._ops[op_id].model_copy(deep=True)
            return OperationStatus(
                operation_id="",
                client_request_id=client_request_id,
                state=OperationState.REJECTED,
                error_code="IDEMPOTENCY_CONFLICT",
                error_message="same client_request_id with a different payload",
                ts=utcnow(),
            )
        pos = self._positions.get(position_id)
        if pos is None:
            raise BrokerError("POSITION_NOT_FOUND", position_id)
        op_id = self._next_id()
        status = OperationStatus(
            operation_id=op_id,
            client_request_id=client_request_id,
            state=OperationState.ACCEPTED,
            ts=utcnow(),
        )
        self._ops[op_id] = status
        self._idempotency[client_request_id] = (f"close:{position_id}:{reason}", op_id)
        asyncio.create_task(self._execute_close(op_id, position_id, reason), name=f"paper-close-{op_id}")
        log.info("PAPER close accepted", op_id=op_id, position_id=position_id, reason=reason)
        return status

    async def operation_status(self, operation_id: str) -> OperationStatus:
        await self._require_connected()
        status = self._ops.get(operation_id)
        if status is None:
            raise BrokerError("OPERATION_NOT_FOUND", operation_id)
        return status.model_copy(deep=True)

    async def history_deals(self, since: datetime | None = None) -> list[Deal]:
        await self._require_connected()
        if since is None:
            return list(self._deals)
        return [d for d in self._deals if d.ts >= since]

    # ------------------------------------------------------------ order engine
    def _validate(self, req: OrderRequest) -> tuple[str, str] | None:
        st = self._state.get(req.instrument)
        if st is None:
            return ("MARKET_INSTRUMENT_NOT_FOUND", req.instrument)
        c = st.conditions
        if not (c.volume_min - 1e-12 <= req.volume <= c.volume_max + 1e-12):
            return ("TRADING_RULE_VOLUME_TOO_SMALL" if req.volume < c.volume_min else "TRADING_RULE_VOLUME_TOO_LARGE",
                    f"volume {req.volume} outside [{c.volume_min}, {c.volume_max}]")
        steps = req.volume / c.volume_step
        if abs(steps - round(steps)) > 1e-9:
            return ("TRADING_RULE_INVALID_VOLUME_STEP", f"volume {req.volume} not a multiple of {c.volume_step}")
        if req.sl is not None and req.tp is not None:
            if req.side is Side.BUY and not (req.sl < st.last_bid < st.last_ask < req.tp):
                return ("TRADING_RULE_INVALID_PRICE_LEVELS", "buy requires sl < market < tp")
            if req.side is Side.SELL and not (req.tp < st.last_bid < st.last_ask < req.sl):
                return ("TRADING_RULE_INVALID_PRICE_LEVELS", "sell requires tp < market < sl")
        return None

    def _slip(self, price: float) -> float:
        return price * self._settings.paper_slippage_bps / 10_000.0

    async def _execute_open(self, op_id: str, req: OrderRequest) -> None:
        await asyncio.sleep(self._settings.paper_ack_delay_s)
        st = self._state[req.instrument]
        try:
            if req.side is Side.BUY:
                fill = round(st.last_ask + self._slip(st.last_ask), st.conditions.point_digits)
            else:
                fill = round(st.last_bid - self._slip(st.last_bid), st.conditions.point_digits)
            pos_id = f"P{self._next_int()}"
            pos = Position(
                id=pos_id,
                instrument=req.instrument,
                side=req.side,
                volume=req.volume,
                open_price=fill,
                sl=req.sl,
                tp=req.tp,
                open_ts=utcnow(),
            )
            self._positions[pos_id] = pos
            self._record_deal(
                operation_id=op_id,
                position_id=pos_id,
                instrument=req.instrument,
                side=req.side,
                volume=req.volume,
                price=fill,
                kind="open",
                reason=req.comment,
            )
            status = self._ops[op_id]
            status.state = OperationState.FILLED
            status.price = fill
            status.volume = req.volume
            status.position_id = pos_id
            status.ts = utcnow()
            log.info("PAPER position opened", op_id=op_id, position_id=pos_id,
                     instrument=req.instrument, side=req.side.value, volume=req.volume, price=fill)
        except Exception as exc:  # pragma: no cover - defensive
            self._ops[op_id].state = OperationState.REJECTED
            self._ops[op_id].error_code = "INTERNAL_ERROR"
            self._ops[op_id].error_message = str(exc)

    async def _execute_close(self, op_id: str, position_id: str, reason: str) -> None:
        await asyncio.sleep(self._settings.paper_ack_delay_s)
        closed = self._close_at_market(position_id, reason)
        status = self._ops[op_id]
        status.state = OperationState.CLOSED
        status.price = closed.price
        status.position_id = position_id
        status.ts = utcnow()

    def _close_at_market(self, position_id: str, reason: str) -> Deal:
        pos = self._positions.pop(position_id, None)
        if pos is None:  # already closed (e.g. SL fired first)
            raise BrokerError("POSITION_NOT_FOUND", position_id)
        st = self._state[pos.instrument]
        if pos.side is Side.BUY:
            exit_price = round(st.last_bid - self._slip(st.last_bid), st.conditions.point_digits)
        else:
            exit_price = round(st.last_ask + self._slip(st.last_ask), st.conditions.point_digits)
        return self._settle(pos, exit_price, reason, operation_id=None)

    def _check_sl_tp(self, tick: Tick) -> None:
        for pos in list(self._positions.values()):
            if pos.instrument != tick.instrument:
                continue
            if pos.side is Side.BUY:
                if pos.sl is not None and tick.bid <= pos.sl:
                    self._settle(pos, pos.sl, "SL")  # SL wins if both reachable
                elif pos.tp is not None and tick.ask >= pos.tp:
                    self._settle(pos, pos.tp, "TP")
            else:
                if pos.sl is not None and tick.ask >= pos.sl:
                    self._settle(pos, pos.sl, "SL")
                elif pos.tp is not None and tick.bid <= pos.tp:
                    self._settle(pos, pos.tp, "TP")

    def _settle(self, pos: Position, exit_price: float, reason: str,
                operation_id: str | None = None) -> Deal:
        self._positions.pop(pos.id, None)
        direction = 1.0 if pos.side is Side.BUY else -1.0
        contract = self._state[pos.instrument].conditions.contract_size
        gross = (exit_price - pos.open_price) * direction * pos.volume * contract
        commission = 0.0
        self._balance += gross - commission
        deal = self._record_deal(
            operation_id=operation_id,
            position_id=pos.id,
            instrument=pos.instrument,
            side=pos.side,
            volume=pos.volume,
            price=exit_price,
            kind="close",
            pnl=round(gross - commission, 2),
            reason=reason,
        )
        log.info("PAPER position closed", position_id=pos.id, reason=reason,
                 exit_price=exit_price, pnl=deal.pnl, balance=round(self._balance, 2))
        return deal

    def _record_deal(self, operation_id: str | None, position_id: str, instrument: str,
                     side: Side, volume: float, price: float, kind: Literal["open", "close"],
                     pnl: float | None = None, reason: str | None = None) -> Deal:
        deal = Deal(
            id=f"D{self._next_int()}",
            operation_id=operation_id,
            position_id=position_id,
            instrument=instrument,
            side=side,
            volume=volume,
            price=price,
            pnl=pnl,
            kind=kind,
            ts=utcnow(),
            reason=reason,
        )
        self._deals.append(deal)
        return deal

    def _used_margin(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            notional = pos.volume * self._state[pos.instrument].conditions.contract_size * pos.open_price
            total += notional / self._settings.paper_leverage
        return total

    # ------------------------------------------------------------------ utils
    def _next_id(self) -> str:
        return f"OP{self._next_int()}"

    def _next_int(self) -> int:
        return next(self._counter)

    async def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerError("BROKER_NOT_CONNECTED", "call connect() first")
