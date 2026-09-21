"""Market state: tick -> 5s/10s/60s bar aggregation correctness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tradingbot.broker.models import Tick
from tradingbot.engine.market import MarketState


def _tick(ts: datetime, bid: float, ask: float) -> Tick:
    return Tick(instrument="EURUSD", bid=bid, ask=ask, ts=ts)


def test_bar_ohlc_and_close_boundary() -> None:
    state = MarketState(["EURUSD"], [5], max_bars=100)
    base = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)

    closed = state.on_tick(_tick(base, 1.0000, 1.0010))
    assert closed == []
    closed += state.on_tick(_tick(base + timedelta(seconds=2), 1.0020, 1.0030))
    closed += state.on_tick(_tick(base + timedelta(seconds=4), 0.9990, 1.0000))
    assert closed == []

    # tick at exactly +5s opens the next bucket and finalizes the first bar
    closed += state.on_tick(_tick(base + timedelta(seconds=5), 1.0005, 1.0015))
    assert len(closed) == 1
    bar = closed[0]
    assert bar.timeframe_s == 5
    assert bar.open == 1.0010
    assert bar.high == 1.0030
    assert bar.low == 0.9990
    assert bar.close == 1.0000
    assert bar.volume_ticks == 3
    assert bar.ts_open == base


def test_multiple_timeframes_close_independently() -> None:
    state = MarketState(["EURUSD"], [5, 10], max_bars=100)
    base = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)
    closed: list = []
    for i in range(6):
        closed += state.on_tick(_tick(base + timedelta(seconds=i), 1.0, 1.0001))
    # at i=5 the 5s bar closes; the 10s bar is still open
    tf5 = [c for c in closed if c.timeframe_s == 5]
    tf10 = [c for c in closed if c.timeframe_s == 10]
    assert len(tf5) == 1
    assert len(tf10) == 0


def test_unknown_instrument_ignored() -> None:
    state = MarketState(["EURUSD"], [5])
    base = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)
    assert state.on_tick(Tick(instrument="XYZUSD", bid=1.0, ask=1.0001, ts=base)) == []


def test_bars_bounded() -> None:
    state = MarketState(["EURUSD"], [5], max_bars=10)
    base = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)
    for i in range(0, 130, 5):
        state.on_tick(_tick(base + timedelta(seconds=i), 1.0, 1.0001))
    assert len(state.markets["EURUSD"].recent_bars(5, 1000)) == 10
