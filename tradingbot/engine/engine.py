"""Trading engine: the persistent async loop.

Responsibilities in Phase 2 (grows in later phases):
  * consume broker ticks -> MarketState (bar aggregation) -> event bus
  * persist closed bars to the ``candles`` table (batched, non-blocking)
  * periodic account snapshots -> DB + event bus
  * heartbeat event for the dashboard
  * kill-switch awareness: when the kill switch is on, the engine stops
    publishing *tradeable* state and logs the condition (Phase 5 plugs the
    risk service in front of order submission)

The strategy engine (Phase 4) subscribes to the same event bus / MarketState;
the engine never places orders by itself.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy import insert

from tradingbot.broker.interfaces import BrokerClient
from tradingbot.broker.models import AccountInfo, Candle
from tradingbot.core.config import Settings
from tradingbot.core.events import Event, EventBus
from tradingbot.core.logging import get_logger, log_event
from tradingbot.core.timeutils import utcnow
from tradingbot.db.base import session_scope
from tradingbot.db.models import AccountSnapshot, AppSettings
from tradingbot.db.models import Candle as CandleRow
from tradingbot.engine.market import MarketState

log = get_logger("tradingbot.engine")


class TradingEngine:
    def __init__(self, settings: Settings, bus: EventBus, broker: BrokerClient,
                 session_factory) -> None:
        self.settings = settings
        self.bus = bus
        self.broker = broker
        self.sessions = session_factory
        self.market = MarketState(settings.instruments, settings.bar_timeframes_s,
                                  max_bars=settings.max_bars_kept)
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._bar_batch: list[dict] = []
        self.last_account: AccountInfo | None = None
        self.last_tick_ts: dict[str, str] = {}

    # ------------------------------------------------------------------ control
    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        await self.broker.connect()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._tick_consumer(), name="engine-ticks"),
            asyncio.create_task(self._account_loop(), name="engine-account"),
            asyncio.create_task(self._heartbeat_loop(), name="engine-heartbeat"),
            asyncio.create_task(self._bar_flush_loop(), name="engine-bar-flush"),
        ]
        log_event(log, 20, "engine started",
                  broker=self.broker.name,
                  mode=self.settings.trading_mode.value,
                  instruments=self.settings.instruments,
                  timeframes=self.settings.bar_timeframes_s)
        await self.bus.publish(Event(type="engine_started", data={
            "broker": self.broker.name, "mode": self.settings.trading_mode.value,
            "instruments": self.settings.instruments,
        }))

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self._flush_bars()
        await self.broker.close()
        log_event(log, 20, "engine stopped")

    # ------------------------------------------------------------------- loops
    async def _tick_consumer(self) -> None:
        async for tick in self.broker.subscribe_ticks(self.settings.instruments):
            self.last_tick_ts[tick.instrument] = tick.ts.isoformat()
            closed = self.market.on_tick(tick)
            await self.bus.publish(Event(type="tick", data={
                "instrument": tick.instrument, "bid": tick.bid, "ask": tick.ask,
                "spread": tick.ask - tick.bid, "ts": tick.ts.isoformat(timespec="milliseconds"),
            }))
            for candle in closed:
                self._bar_batch.append(self._candle_row(candle))
                await self.bus.publish(Event(type="bar_closed", data=candle.model_dump(mode="json")))

    async def _account_loop(self) -> None:
        while self._running:
            try:
                info = await self.broker.account_info()
                self.last_account = info
                await self._persist_snapshot(info)
                await self.bus.publish(Event(type="account_state", data=info.model_dump(mode="json")))
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log_event(log, 40, "account snapshot failed", error=str(exc))
            await asyncio.sleep(self.settings.account_snapshot_interval_s)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            settings = await self._get_settings_row()
            await self.bus.publish(Event(type="heartbeat", data={
                "ts": utcnow().isoformat(timespec="milliseconds"),
                "kill_switch": settings.kill_switch,
                "trading_paused": settings.trading_paused,
                "last_tick_ts": dict(self.last_tick_ts),
            }))
            await asyncio.sleep(self.settings.heartbeat_interval_s)

    async def _bar_flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(2.0)
            if self._bar_batch:
                await self._flush_bars()

    # ------------------------------------------------------------------- helpers
    @staticmethod
    def _candle_row(c: Candle) -> dict:
        return {
            "instrument": c.instrument,
            "timeframe_s": c.timeframe_s,
            "ts_open": c.ts_open,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume_ticks": c.volume_ticks,
        }

    async def _flush_bars(self) -> None:
        if not self._bar_batch:
            return
        batch, self._bar_batch = self._bar_batch, []
        async with session_scope(self.sessions) as session:
            await session.execute(insert(CandleRow), batch)

    async def _persist_snapshot(self, info: AccountInfo) -> None:
        async with session_scope(self.sessions) as session:
            session.add(AccountSnapshot(
                account_id=info.account_id, currency=info.currency,
                balance=info.balance, equity=info.equity, free_margin=info.free_margin,
                margin_level=info.margin_level, leverage=info.leverage,
                open_positions=info.open_positions, unrealized_pnl=info.unrealized_pnl,
            ))

    async def _get_settings_row(self) -> AppSettings:
        async with session_scope(self.sessions) as session:
            row = await session.get(AppSettings, AppSettings.row_id())
            if row is None:
                row = AppSettings(id=AppSettings.row_id())
                session.add(row)
            return row
