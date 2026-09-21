"""Risk manager gates, and the metric maths."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from scalper.config import InstrumentSpec, RiskConfig
from scalper.metrics import compute_metrics, drawdown_series, monthly_returns
from scalper.models import AccountSnapshot, Direction, ExitReason, Trade
from scalper.risk import RiskManager

T0 = datetime(2024, 1, 2, 9, 0, tzinfo=timezone.utc)
SPEC = InstrumentSpec("EURUSD", 0.0001, 100_000, 10.0, 1.0, 5)


def trade(pnl: float, r: float = 0.0, *, minutes: int = 10, reason: ExitReason = ExitReason.TAKE_PROFIT,
          commission: float = 0.0) -> Trade:
    return Trade(
        symbol="EURUSD",
        direction=Direction.LONG,
        lots=0.1,
        entry_price=1.1000,
        entry_time=T0,
        exit_price=1.1000 + pnl / 1000,
        exit_time=T0 + timedelta(minutes=minutes),
        stop_price=1.0990,
        take_profit_price=1.1020,
        gross_pnl=pnl,
        commission=commission,
        net_pnl=pnl,
        pips=pnl / 10.0,
        r_multiple=r,
        exit_reason=reason,
        bars_held=minutes,
    )


# -----------------------------------------------------------------------------
# RiskManager
# -----------------------------------------------------------------------------
def make_risk(**kwargs) -> RiskManager:
    defaults = dict(
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=3.0,
        max_drawdown_pct=10.0,
        max_concurrent_positions=2,
        max_trades_per_day=5,
        min_lot=0.01,
        max_lot=5.0,
        lot_step=0.01,
        max_spread_pips=2.0,
        min_seconds_between_trades=0,
    )
    defaults.update(kwargs)
    return RiskManager(RiskConfig(**defaults), initial_balance=10_000.0)


def test_sizing_is_linear_in_risk_and_inverse_in_stop():
    rm = make_risk(risk_per_trade_pct=1.0)
    # 1% of 10,000 = 100 USD; 10 pip stop, 10 USD/pip -> 1.0 lot
    assert rm.size(equity=10_000, spec=SPEC, stop_pips=10).lots == pytest.approx(1.0)
    # Double the stop -> half the size (same money at risk)
    assert rm.size(equity=10_000, spec=SPEC, stop_pips=20).lots == pytest.approx(0.5)
    # Double the equity -> double the size
    assert rm.size(equity=20_000, spec=SPEC, stop_pips=10).lots == pytest.approx(2.0)


def test_sizing_respects_max_lot():
    rm = make_risk(risk_per_trade_pct=50.0, max_lot=5.0)
    assert rm.size(equity=1_000_000, spec=SPEC, stop_pips=1).lots == pytest.approx(5.0)


def test_sizing_rejects_a_zero_stop():
    rm = make_risk()
    decision = rm.size(equity=10_000, spec=SPEC, stop_pips=0)
    assert not decision.allowed
    assert "zero" in decision.reason


def test_check_blocks_on_concurrency_then_allows_again():
    rm = make_risk(max_concurrent_positions=2)
    rm.reset_day(T0.date(), 10_000)
    assert rm.check(now=T0, equity=10_000, open_positions=1).allowed
    blocked = rm.check(now=T0, equity=10_000, open_positions=2)
    assert not blocked.allowed and "concurrent" in blocked.reason


def test_check_blocks_when_spread_is_too_wide():
    rm = make_risk(max_spread_pips=1.0)
    rm.reset_day(T0.date(), 10_000)
    decision = rm.check(now=T0, equity=10_000, open_positions=0, spread_pips=2.5)
    assert not decision.allowed and "spread" in decision.reason


def test_daily_loss_limit_uses_the_day_start_balance():
    rm = make_risk(max_daily_loss_pct=2.0)
    rm.reset_day(T0.date(), 10_000)
    assert not rm.daily_loss_hit

    rm.on_trade_closed(-150.0, T0)
    assert not rm.daily_loss_hit  # 1.5% < 2%
    rm.on_trade_closed(-60.0, T0)
    assert rm.daily_loss_hit      # 2.1% >= 2%
    assert not rm.check(now=T0, equity=9_790, open_positions=0).allowed


def test_daily_counters_reset_on_a_new_day():
    rm = make_risk(max_trades_per_day=1, max_daily_loss_pct=1.0)
    rm.reset_day(T0.date(), 10_000)
    rm.on_trade_opened(T0)
    rm.on_trade_closed(-200.0, T0)
    assert not rm.check(now=T0, equity=9_800, open_positions=0).allowed

    tomorrow = T0 + timedelta(days=1)
    rm.reset_day(tomorrow.date(), 9_800)
    assert rm.check(now=tomorrow, equity=9_800, open_positions=0).allowed


def test_drawdown_kill_switch_is_sticky_until_resumed():
    rm = make_risk(max_drawdown_pct=10.0)
    rm.on_equity(10_000.0)
    rm.on_equity(9_500.0)
    assert not rm.state.halted
    rm.on_equity(8_900.0)  # -11% from the 10,000 peak
    assert rm.state.halted
    assert "drawdown" in rm.state.halt_reason
    assert not rm.check(now=T0, equity=8_900, open_positions=0).allowed

    rm.resume()
    assert rm.check(now=T0, equity=8_900, open_positions=0).allowed


def test_drawdown_uses_the_peak_not_the_start():
    rm = make_risk(max_drawdown_pct=10.0)
    rm.on_equity(10_000)
    rm.on_equity(12_000)   # new peak
    rm.on_equity(10_900)   # -9.2% from the peak, +9% vs start
    assert not rm.state.halted
    rm.on_equity(10_700)   # -10.8% from the peak
    assert rm.state.halted


def test_summary_reports_the_daily_budget_state():
    rm = make_risk(max_daily_loss_pct=4.0)
    rm.reset_day(T0.date(), 10_000)
    rm.on_trade_closed(-100.0, T0)
    summary = rm.summary()
    assert summary["daily_loss_pct"] == pytest.approx(1.0)
    assert summary["daily_loss_limit_pct"] == 4.0
    assert summary["daily_loss_hit"] is False
    assert summary["day_realized_pnl"] == -100.0


# -----------------------------------------------------------------------------
# Metrics
# -----------------------------------------------------------------------------
def test_metrics_on_a_known_trade_set():
    trades = [trade(100.0, 1.0), trade(100.0, 1.0), trade(-50.0, -0.5), trade(-50.0, -0.5)]
    m = compute_metrics(trades, initial_balance=10_000)

    assert m.total_trades == 4
    assert m.wins == 2 and m.losses == 2
    assert m.win_rate_pct == pytest.approx(50.0)
    assert m.net_profit == pytest.approx(100.0)
    assert m.profit_factor == pytest.approx(200.0 / 100.0)
    assert m.expectancy == pytest.approx(25.0)
    assert m.expectancy_r == pytest.approx(0.25)
    assert m.average_win == pytest.approx(100.0)
    assert m.average_loss == pytest.approx(-50.0)
    assert m.payoff_ratio == pytest.approx(2.0)
    assert m.largest_win == pytest.approx(100.0)
    assert m.largest_loss == pytest.approx(-50.0)


def test_profit_factor_is_infinite_without_losses():
    m = compute_metrics([trade(10.0, 1.0)], initial_balance=1_000)
    assert m.profit_factor == float("inf")
    assert m.to_dict()["profit_factor"] is None  # JSON-safe


def test_streak_counting_breaks_on_scratch_trades():
    trades = [trade(10, 1), trade(10, 1), trade(0, 0), trade(10, 1), trade(-5, -1), trade(-5, -1)]
    m = compute_metrics(trades)
    assert m.max_consecutive_wins == 2   # the 0-P&L trade breaks the run
    assert m.max_consecutive_losses == 2


def test_drawdown_is_measured_on_equity_not_balance():
    curve = [
        AccountSnapshot(T0 + timedelta(minutes=i), balance=10_000.0, equity=e)
        for i, e in enumerate([10_000, 10_500, 9_800, 9_900, 10_600])
    ]
    m = compute_metrics([], curve, initial_balance=10_000)
    # Peak 10,500 -> trough 9,800 = 700 (6.67%)
    assert m.max_drawdown == pytest.approx(700.0)
    assert m.max_drawdown_pct == pytest.approx(700 / 10_500 * 100, abs=0.01)
    assert m.net_profit == pytest.approx(600.0)


def test_time_in_market_and_max_dd_duration():
    curve = [
        AccountSnapshot(T0 + timedelta(days=i), balance=10_000, equity=e, open_positions=p)
        for i, (e, p) in enumerate([(10_000, 0), (9_000, 1), (9_500, 1), (9_800, 0), (10_200, 0)])
    ]
    m = compute_metrics([], curve, initial_balance=10_000)
    assert m.time_in_market_pct == pytest.approx(40.0)  # 2 of 5 bars in a position
    # Peak on day 0 (10,000), not reclaimed until day 4 -> 4 days underwater.
    assert m.max_drawdown_duration_days == pytest.approx(4.0)


def test_cagr_is_suppressed_for_short_backtests():
    """Annualising a few minutes of data is arithmetic, not information."""
    curve = [
        AccountSnapshot(T0 + timedelta(minutes=i), 10_000, 10_000 + i * 5)
        for i in range(10)
    ]
    m = compute_metrics([], curve, initial_balance=10_000)
    assert m.cagr_pct == 0.0
    assert m.return_pct > 0  # the raw return is still reported


def test_cagr_is_reported_for_long_backtests():
    curve = [
        AccountSnapshot(T0 + timedelta(days=i), 10_000, 10_000 * (1.0005 ** i))
        for i in range(400)
    ]
    m = compute_metrics([], curve, initial_balance=10_000)
    assert m.cagr_pct > 0


def test_empty_inputs_produce_zeroed_metrics():
    m = compute_metrics([], [])
    assert m.total_trades == 0
    assert m.net_profit == 0.0
    assert m.sharpe == 0.0
    assert m.profit_factor == 0.0
    assert isinstance(m.to_dict(), dict)


def test_commission_is_separated_from_gross():
    trades = [trade(100.0, 1.0, commission=7.0), trade(-50.0, -0.5, commission=3.0)]
    m = compute_metrics(trades, initial_balance=10_000)
    assert m.total_commission == pytest.approx(10.0)
    assert m.commission_per_trade == pytest.approx(5.0)


def test_sharpe_is_positive_for_a_rising_equity_curve():
    equity = 10_000 * np.cumprod(1 + np.full(1_000, 0.00001))
    curve = [AccountSnapshot(T0 + timedelta(minutes=i), balance=float(e), equity=float(e))
             for i, e in enumerate(equity)]
    m = compute_metrics([], curve, initial_balance=10_000, bars_per_year=374_400)
    assert m.sharpe > 0
    assert m.return_pct > 0
    assert m.max_drawdown_pct == pytest.approx(0.0)


def test_sharpe_is_negative_for_a_falling_curve():
    equity = 10_000 * np.cumprod(1 + np.full(1_000, -0.00001))
    curve = [AccountSnapshot(T0 + timedelta(minutes=i), balance=float(e), equity=float(e))
             for i, e in enumerate(equity)]
    m = compute_metrics([], curve, initial_balance=10_000, bars_per_year=374_400)
    assert m.sharpe < 0
    assert m.max_drawdown_pct > 0


def test_drawdown_series_and_monthly_returns():
    curve = [
        AccountSnapshot(datetime(2024, 1, 15, tzinfo=timezone.utc), 10_000, 10_000),
        AccountSnapshot(datetime(2024, 1, 31, tzinfo=timezone.utc), 10_000, 11_000),
        AccountSnapshot(datetime(2024, 2, 29, tzinfo=timezone.utc), 10_000, 10_450),
    ]
    dd = drawdown_series(curve)
    assert list(dd.columns) == ["equity", "peak", "drawdown", "drawdown_pct"]
    assert dd["drawdown"].max() == pytest.approx(550.0)

    monthly = monthly_returns(curve)
    assert len(monthly) == 1
    assert monthly.iloc[0]["return_pct"] == pytest.approx(-5.0, abs=0.01)


def test_monthly_returns_does_not_invent_future_month_ends():
    """Regression: a curve ending mid-month must not report the last month as 0.00%.

    The old implementation resampled with ``freq="ME"``, which forward-fills the
    final equity to the month end. On an active index that is harmless; on a
    timezone-aware one it can collapse every bucket, reporting a losing run as a
    row of flat months — including the month the loss actually happened in.
    """
    curve = [
        AccountSnapshot(datetime(2024, 1, 1, tzinfo=timezone.utc), 10_000, 10_000),
        AccountSnapshot(datetime(2024, 1, 20, tzinfo=timezone.utc), 10_000, 9_500),
        AccountSnapshot(datetime(2024, 2, 10, tzinfo=timezone.utc), 10_000, 9_280),
        # The curve simply stops on 22 March: no 31 March snapshot exists.
        AccountSnapshot(datetime(2024, 3, 22, tzinfo=timezone.utc), 10_000, 9_280),
    ]
    monthly = monthly_returns(curve)
    assert list(monthly["month"]) == [2, 3], "first month needs a baseline to compare against"

    feb = monthly[monthly["month"] == 2].iloc[0]["return_pct"]
    mar = monthly[monthly["month"] == 3].iloc[0]["return_pct"]
    assert feb == pytest.approx(-2.3158, abs=0.01)   # 9,280 / 9,500 - 1
    assert mar == pytest.approx(0.0, abs=1e-9)       # equity genuinely flat in March


def test_monthly_returns_measures_the_first_month_against_the_initial_balance():
    """A backtest that opens and closes inside one month still reports that month."""
    curve = [
        AccountSnapshot(datetime(2024, 1, 1, tzinfo=timezone.utc), 10_000, 10_000),
        AccountSnapshot(datetime(2024, 1, 31, 23, 59, tzinfo=timezone.utc), 10_000, 8_857.26),
    ]
    monthly = monthly_returns(curve, initial_balance=10_000)
    assert len(monthly) == 1
    assert monthly.iloc[0]["month"] == 1
    assert monthly.iloc[0]["return_pct"] == pytest.approx(-11.4274, abs=0.01)


def test_headline_is_human_readable():
    m = compute_metrics([trade(100.0, 1.0), trade(-50.0, -0.5)], initial_balance=10_000)
    text = m.headline()
    assert "trades" in text and "PF" in text and "maxDD" in text
