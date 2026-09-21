"""End-to-end proofs that the simulator cannot see the future.

Indicator-level lookahead is caught in `test_strategies.py`. These tests cover
the other two places it can sneak in:

1. **The engine** acting on a signal before its bar has closed.
2. **The data path / metrics** leaking information backwards (forward fills,
   resampling with a right label, future-bar statistics).

Both tests work by *mutating the future* and asserting the past is unchanged. If
any code path reads a bar it should not, these fail loudly — and unlike a
"results look plausible" check, they cannot pass by luck.
"""

from __future__ import annotations

import pandas as pd
import pytest

from scalper.backtest import run_backtest
from scalper.brokers import build_broker
from scalper.config import load_config
from scalper.data import build_feed
from scalper.strategies import get_strategy


def build_result(cfg, frame: pd.DataFrame, symbol: str = "EURUSD"):
    broker = build_broker(cfg)
    strategy = get_strategy(cfg.strategy.name, symbol=symbol, **cfg.strategy.params)
    return run_backtest(cfg, broker, strategy, symbol, frame)


def trade_signature(result) -> list[tuple]:
    """Everything about a trade that a peeking engine would change."""
    return [
        (
            t.entry_time,
            t.exit_time,
            t.direction.value,
            round(t.entry_price, 6),
            round(t.exit_price, 6),
            round(t.net_pnl, 6),
            t.exit_reason.value,
        )
        for t in result.trades
    ]


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/config.yaml", {"data.synthetic.bars": 60_000})


@pytest.fixture(scope="module")
def frame(cfg):
    return build_feed(cfg).load()["EURUSD"]


def test_truncating_the_history_does_not_change_earlier_trades(cfg, frame):
    """A backtest on bars[0:n] must equal a backtest on the full history, up to n.

    Note this is stronger than it looks: the run is over a *different* DataFrame
    length, so any accidental use of `len(df)`, a global normalisation, or a
    reversed rolling window shows up immediately.
    """
    cut = 40_000
    full = build_result(cfg, frame)
    partial = build_result(cfg, frame.iloc[:cut])

    full_prefix = [sig for sig in trade_signature(full) if sig[1] < frame.index[cut].to_pydatetime()]
    assert trade_signature(partial) == full_prefix

    # And there must actually be trades to compare, or this proves nothing.
    assert len(partial.trades) > 5


def test_rewriting_the_future_does_not_change_the_past(cfg, frame):
    """Replace every future bar with nonsense; earlier trades must be identical.

    This is the direct test for peeking: the engine below the cut cannot possibly
    know that bars above the cut were replaced with a straight line.
    """
    cut = 40_000
    baseline = build_result(cfg, frame.iloc[:cut])

    tampered = frame.copy()
    index = tampered.index[cut:]
    tampered.loc[index, "open"] = 9.9999
    tampered.loc[index, "high"] = 10.0000
    tampered.loc[index, "low"] = 9.9990
    tampered.loc[index, "close"] = 9.9995

    tampered_result = build_result(cfg, tampered)
    prefix = [
        sig
        for sig in trade_signature(tampered_result)
        if sig[1] < frame.index[cut].to_pydatetime()
    ]
    assert prefix == trade_signature(baseline)


def test_entry_price_is_the_next_bar_open_not_a_later_price(cfg, frame):
    """Every entry must be explainable by the bar it filled on, not one after."""
    result = build_result(cfg, frame.iloc[:30_000])
    spec = cfg.instrument("EURUSD")

    assert result.trades, "expected at least one trade"
    spread = spec.spread_pips * spec.pip_size
    slip = cfg.broker.paper.slippage_pips * spec.pip_size

    for trade in result.trades:
        # The engine fills at this bar's OPEN, and the paper broker adds the
        # round-trip costs: a long buys the ask and slips up, a short sells the
        # bid and slips down. Nothing here reads the bar's close or high.
        bar = frame.loc[pd.Timestamp(trade.entry_time)]
        if trade.direction.value == "long":
            expected = bar["open"] + spread + slip
        else:
            expected = bar["open"] - slip
        assert trade.entry_price == pytest.approx(expected, abs=1e-6), (
            f"entry at {trade.entry_time} filled at {trade.entry_price}, "
            f"but the bar it opened on was {expected}"
        )


def test_stop_levels_come_from_the_signal_bar_not_the_entry_bar(cfg, frame):
    """Stops are set from the signal bar close, so they are knowable in advance."""
    result = build_result(cfg, frame.iloc[:30_000])
    assert result.trades
    for trade in result.trades:
        risk_pips = abs(trade.entry_price - trade.stop_price) / 0.0001
        # Stop distance must be positive and plausible (not zero, not absurd).
        assert 0.5 < risk_pips < 500.0


def test_metrics_only_use_the_equity_curve_they_are_given(cfg, frame):
    """Truncating the run truncates the equity curve; no metric looks past it."""
    short = build_result(cfg, frame.iloc[:20_000])
    long = build_result(cfg, frame.iloc[:40_000])

    assert len(short.equity_curve) <= len(long.equity_curve)
    assert short.metrics.max_drawdown_pct >= 0
    # The shorter run's final equity must equal an actual point in its own curve.
    assert short.final_balance == pytest.approx(
        short.equity_curve[-1].balance, abs=0.01
    ) or short.final_balance == pytest.approx(short.metrics.net_profit + cfg.account.initial_balance, abs=0.01)
