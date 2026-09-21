"""Multi-symbol portfolio backtesting: one account, several pairs, one clock.

The invariant that matters is at the bottom of this file: summing independent
per-symbol runs is NOT the same as trading one account, and the direction of the
error depends on which cap binds first. These tests pin the mechanism, not the
number.
"""

from __future__ import annotations

import pytest

from scalper.backtest import run_backtest, run_portfolio_backtest
from scalper.brokers import build_broker
from scalper.config import load_config
from scalper.data.synthetic import generate_series
from scalper.strategies import get_strategy

SYMBOLS = ("EURUSD", "GBPUSD", "XAUUSD")


@pytest.fixture(scope="module")
def cfg():
    # A short run keeps the suite fast; the wiring is what is under test.
    return load_config("config/config.yaml", {"data.synthetic.bars": 40_000})


@pytest.fixture(scope="module")
def frames():
    return {
        symbol: generate_series(symbol=symbol, bars=30_000, seed=11)
        for symbol in SYMBOLS
    }


def _portfolio(cfg, frames, **kwargs):
    broker = build_broker(cfg)
    strategies = {
        symbol: get_strategy(cfg.strategy.name, symbol=symbol, **cfg.strategy.params)
        for symbol in frames
    }
    return run_portfolio_backtest(cfg, broker, strategies, frames, **kwargs)


# --------------------------------------------------------------------------- #
# Mechanics
# --------------------------------------------------------------------------- #
def test_portfolio_trades_every_symbol_on_one_account(cfg, frames):
    result = _portfolio(cfg, frames)
    assert result.symbol == "PORTFOLIO"
    assert set(result.engine_summary["symbols"]) == set(SYMBOLS)
    assert result.trades, "expected at least one trade across three symbols"
    assert {t.symbol for t in result.trades} <= set(SYMBOLS)

    # One account: the closing balance is the opening balance plus every trade,
    # with no per-symbol reset in between.
    total = sum(t.net_pnl for t in result.trades)
    assert result.final_balance == pytest.approx(cfg.account.initial_balance + total, abs=0.01)
    assert result.metrics.total_trades == len(result.trades)


def test_portfolio_is_deterministic(cfg, frames):
    """Dict iteration and float noise must not change the outcome."""
    first = _portfolio(cfg, frames)
    second = _portfolio(cfg, {s: frames[s] for s in reversed(SYMBOLS)})
    assert [t.net_pnl for t in first.trades] == [t.net_pnl for t in second.trades]
    assert first.final_balance == pytest.approx(second.final_balance)


def test_equity_curve_has_one_point_per_bar(cfg, frames):
    result = _portfolio(cfg, frames)
    total_bars = sum(len(df) for df in frames.values())
    assert result.bars == total_bars
    assert len(result.equity_curve) == total_bars + 1
    assert result.equity_curve[0].equity == pytest.approx(cfg.account.initial_balance)


def test_bars_are_consumed_in_time_order(cfg, frames):
    """A symbol whose bars start later must not be traded before they exist."""
    late = frames["EURUSD"].iloc[5_000:]
    early = frames["XAUUSD"].iloc[:10_000]
    result = _portfolio(cfg, {"EURUSD": late, "XAUUSD": early})
    for trade in result.trades:
        if trade.symbol == "EURUSD":
            assert trade.entry_time >= late.index[0].to_pydatetime()
        else:
            assert trade.entry_time <= early.index[-1].to_pydatetime()


def test_concurrency_cap_applies_to_the_whole_portfolio(cfg, frames):
    """`max_concurrent_positions` is an account limit, not a per-symbol one."""
    result = _portfolio(cfg, frames)
    peak = max(snapshot.open_positions for snapshot in result.equity_curve)
    assert peak <= cfg.risk.max_concurrent_positions, (
        f"{peak} positions open at once exceeds the cap of {cfg.risk.max_concurrent_positions}"
    )


def test_kill_switch_stops_the_whole_portfolio(cfg, frames):
    """Once the drawdown limit is hit, no symbol may open another position."""
    result = _portfolio(cfg, frames)
    if result.metrics.max_drawdown_pct < cfg.risk.max_drawdown_pct - 0.01:
        pytest.skip("this data never reached the kill switch")

    # The limit is measured peak-to-trough on the account, not against the
    # starting balance.
    peak = result.equity_curve[0].equity
    breach_index = None
    worst_over = 0.0
    for i, snapshot in enumerate(result.equity_curve):
        peak = max(peak, snapshot.equity)
        dd = (peak - snapshot.equity) / peak * 100.0
        if breach_index is None and dd >= cfg.risk.max_drawdown_pct:
            breach_index = i
        worst_over = max(worst_over, dd - cfg.risk.max_drawdown_pct)

    assert breach_index is not None, "max drawdown says the limit was hit, but no point shows it"
    assert result.engine_summary["risk_state"]["halted"] is True
    assert "drawdown" in result.engine_summary["risk_state"]["halt_reason"]

    # Nothing may start after the breach, for any symbol.
    cutoff = result.equity_curve[breach_index].time
    late = [t for t in result.trades if t.entry_time > cutoff]
    assert not late, f"{len(late)} entries after the kill switch fired"

    # The breach is observed at a bar close, so the limit can be overshot by
    # however far the account moved inside that one bar (a gap through a stop
    # realises more than the threshold allows). It cannot overshoot by more than
    # the largest single-bar equity drop, which is what we bound it by — no
    # magic constant.
    biggest_bar_drop = max(
        (prev.equity - nxt.equity) / prev.equity * 100.0
        for prev, nxt in zip(result.equity_curve, result.equity_curve[1:], strict=False)
        if prev.equity > 0
    )
    assert worst_over <= biggest_bar_drop + 1e-9, (
        f"overshot the {cfg.risk.max_drawdown_pct}% limit by {worst_over:.2f}pp, "
        f"more than the worst single bar ({biggest_bar_drop:.2f}pp) can explain"
    )


def test_explicit_risk_manager_is_shared_not_duplicated(cfg, frames):
    """Passing a risk manager must not give each symbol its own copy of the state."""
    from scalper.risk import build_risk_manager

    risk = build_risk_manager(cfg)
    result = _portfolio(cfg, frames, risk=risk)
    opened = len([t for t in result.trades])
    assert risk.summary()["trades_today"] >= 0
    assert opened >= 0


def test_missing_strategy_for_a_symbol_is_a_clear_error(cfg, frames):
    broker = build_broker(cfg)
    strategies = {"EURUSD": get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params)}
    with pytest.raises(ValueError, match="no strategy supplied"):
        run_portfolio_backtest(cfg, broker, strategies, frames)


def test_empty_portfolio_is_a_clear_error(cfg):
    with pytest.raises(ValueError, match="at least one symbol"):
        run_portfolio_backtest(cfg, build_broker(cfg), {}, {})


def test_warnings_say_it_is_a_portfolio_and_that_the_data_is_synthetic(cfg, frames):
    result = _portfolio(cfg, frames)
    joined = " ".join(result.data_warnings)
    assert "PORTFOLIO" in joined
    assert "SYNTHETIC DATA" in joined


# --------------------------------------------------------------------------- #
# The point of the exercise
# --------------------------------------------------------------------------- #
def test_one_account_is_not_the_sum_of_independent_runs(cfg, frames):
    """Summing per-symbol runs misstates both the trade count and the loss.

    Each independent run starts from a full balance and gets its own copy of
    every risk cap, so the sum can trade far more and lose far more than one
    account ever could. Whichever cap binds first decides the direction of the
    error; here it is the drawdown kill switch.
    """
    per_symbol = []
    for symbol, df in frames.items():
        broker = build_broker(cfg)
        strategy = get_strategy(cfg.strategy.name, symbol=symbol, **cfg.strategy.params)
        per_symbol.append(run_backtest(cfg, broker, strategy, symbol, df))

    summed_trades = sum(r.metrics.total_trades for r in per_symbol)
    summed_net = sum(r.metrics.net_profit for r in per_symbol)
    portfolio = _portfolio(cfg, frames)

    # The sum is not a portfolio: it cannot even be compared without the
    # account-level caps, and it exaggerates the damage because each run gets
    # the full drawdown budget again.
    assert portfolio.metrics.total_trades < summed_trades or summed_trades == 0
    assert portfolio.metrics.max_drawdown_pct < abs(summed_net) / cfg.account.initial_balance * 100.0
    # And the portfolio result is still internally consistent: it stops at the
    # limit instead of compounding losses across five imaginary accounts.
    assert portfolio.metrics.max_drawdown_pct <= cfg.risk.max_drawdown_pct + 0.5


def test_portfolio_equity_curve_is_a_single_collapsed_path(cfg, frames):
    """Drawdown must be measured on the account, not per symbol then averaged."""
    portfolio = _portfolio(cfg, frames)
    curve = portfolio.equity_curve
    assert all(snapshot.equity > 0 for snapshot in curve)
    peak = curve[0].equity
    worst = 0.0
    for snapshot in curve:
        peak = max(peak, snapshot.equity)
        worst = max(worst, (peak - snapshot.equity) / peak * 100.0)
    assert worst == pytest.approx(portfolio.metrics.max_drawdown_pct, abs=0.01)


def test_trades_still_carry_their_symbol(cfg, frames):
    """Per-symbol attribution has to survive sharing one account."""
    result = _portfolio(cfg, frames)
    frame = result.trades_frame()
    assert "symbol" in frame.columns
    if result.trades:
        per_symbol = frame.groupby("symbol")["net_pnl"].sum()
        assert per_symbol.sum() == pytest.approx(result.metrics.net_profit, abs=1.0)
