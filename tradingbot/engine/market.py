"""Market state: tick processing and client-side bar aggregation.

The broker provides ticks (and, from Phase 3+, native candles >= 1m). For the
5–10 s analysis horizon the engine aggregates ticks into 5 s / 10 s bars here.
Candle closes are published as ``bar_closed`` events and persisted to the
``candles`` table (used by the dashboard chart and later backtesting).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tradingbot.broker.models import Candle, Tick
from tradingbot.core.logging import get_logger

log = get_logger("tradingbot.engine.market")


@dataclass
class _PendingBar:
    ts_open: datetime
    open: float
    high: float
    low: float
    close: float
    ticks: int = 0


@dataclass
class InstrumentMarket:
    instrument: str
    last_tick: Tick | None = None
    bars: dict[int, deque[Candle]] = field(default_factory=dict)
    _pending: dict[int, _PendingBar] = field(default_factory=dict)

    def init_timeframes(self, timeframes: list[int], max_bars: int) -> None:
        for tf in timeframes:
            self.bars[tf] = deque(maxlen=max_bars)

    def on_tick(self, tick: Tick, timeframes: list[int]) -> list[Candle]:
        """Update state; returns the list of candles that just closed."""
        self.last_tick = tick
        closed: list[Candle] = []
        for tf in timeframes:
            bar_start = _floor(tick.ts, tf)
            pending = self._pending.get(tf)
            if pending is not None and bar_start > pending.ts_open:
                closed.append(self._finalize(tf, pending))
            if pending is None or bar_start > pending.ts_open:
                self._pending[tf] = _PendingBar(
                    ts_open=bar_start,
                    open=tick.ask,  # mid would be fairer; ask for consistency with entry side
                    high=tick.ask,
                    low=tick.bid,
                    close=tick.ask,
                    ticks=1,
                )
            else:
                pending.high = max(pending.high, tick.ask)
                pending.low = min(pending.low, tick.bid)
                pending.close = tick.ask
                pending.ticks += 1
        return closed

    def _finalize(self, tf: int, pending: _PendingBar) -> Candle:
        candle = Candle(
            instrument=self.instrument,
            timeframe_s=tf,
            ts_open=pending.ts_open,
            open=pending.open,
            high=pending.high,
            low=pending.low,
            close=pending.close,
            volume_ticks=pending.ticks,
        )
        self.bars[tf].append(candle)
        return candle

    def recent_bars(self, timeframe_s: int, n: int = 100) -> list[Candle]:
        items = list(self.bars[timeframe_s])
        return items[-n:]


def _floor(dt: datetime, seconds: int) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    delta = dt - epoch
    total = int(delta.total_seconds())
    floored = total - (total % seconds)
    return epoch + timedelta(seconds=floored)


class MarketState:
    def __init__(self, instruments: list[str], timeframes: list[int], max_bars: int = 500) -> None:
        self.timeframes = list(timeframes)
        self.markets: dict[str, InstrumentMarket] = {
            inst: InstrumentMarket(instrument=inst) for inst in instruments
        }
        for m in self.markets.values():
            m.init_timeframes(self.timeframes, max_bars)

    def on_tick(self, tick: Tick) -> list[Candle]:
        m = self.markets.get(tick.instrument)
        if m is None:
            return []
        return m.on_tick(tick, self.timeframes)

    def snapshot(self) -> dict:
        out: dict[str, dict] = {}
        for inst, m in self.markets.items():
            tick = m.last_tick
            out[inst] = {
                "last_tick": tick.model_dump(mode="json") if tick else None,
                "spread": (tick.ask - tick.bid) if tick else None,
                "last_bar": {
                    tf: (b[-1].model_dump(mode="json") if b else None)
                    for tf, b in m.bars.items()
                },
            }
        return out

    def unused_check(self) -> None:  # keep import of math referenced for future drift checks
        _ = math
