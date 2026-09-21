"""Performance metrics.

Deliberately opinionated about a few things, because these are the numbers that
separate a real edge from a pretty equity curve:

  * **Profit factor and expectancy are reported in R as well as currency.** R
    (risk multiples) is comparable across symbols, account sizes and time; money
    is not.
  * **Drawdown is measured on the equity curve**, not on closed trades, so open
    positions and intraday swings count.
  * **Sharpe/Sortino use per-bar equity returns** annualised by the timeframe's
    bar count. Per-trade Sharpe would need trade durations to annualise
    correctly and is silently wrong for scalpers holding minutes.
  * **Time in market** is reported. A scalper that is flat 95% of the time has
    different risk than one that is always exposed, even at identical returns.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from .models import AccountSnapshot, Trade

#: Below this much elapsed history, CAGR is noise amplified by the exponent.
MIN_YEARS_FOR_CAGR = 30.0 / 365.25


@dataclass
class PerformanceMetrics:
    # --- returns ------------------------------------------------------------
    net_profit: float = 0.0
    return_pct: float = 0.0
    cagr_pct: float = 0.0
    # --- risk ---------------------------------------------------------------
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration_days: float = 0.0
    volatility_annual_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    # --- trades -------------------------------------------------------------
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    expectancy_r: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    payoff_ratio: float = 0.0
    average_r: float = 0.0
    total_r: float = 0.0
    # --- streaks / behaviour ------------------------------------------------
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    average_bars_held: float = 0.0
    average_duration_minutes: float = 0.0
    total_commission: float = 0.0
    commission_per_trade: float = 0.0
    time_in_market_pct: float = 0.0
    trades_per_day: float = 0.0
    exits_by_reason: dict[str, int] = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in out.items():
            if isinstance(value, float):
                out[key] = None if (math.isnan(value) or math.isinf(value)) else round(value, 4)
        return out

    # -- convenience ---------------------------------------------------------
    @property
    def is_profitable(self) -> bool:
        return self.net_profit > 0

    def headline(self) -> str:
        return (
            f"net {self.net_profit:+,.2f} ({self.return_pct:+.2f}%) | "
            f"{self.total_trades} trades | win {self.win_rate_pct:.1f}% | "
            f"PF {self.profit_factor:.2f} | expectancy {self.expectancy_r:+.3f}R | "
            f"maxDD {self.max_drawdown_pct:.2f}% | Sharpe {self.sharpe:.2f}"
        )


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0 or math.isnan(denominator) or math.isnan(numerator):
        return default
    return numerator / denominator


def compute_metrics(
    trades: Sequence[Trade],
    equity_curve: Sequence[AccountSnapshot] | None = None,
    *,
    initial_balance: float = 10_000.0,
    bars_per_year: float = 374_400.0,
    risk_free_rate: float = 0.0,
) -> PerformanceMetrics:
    """Compute the full metric set from trades and (optionally) an equity curve."""
    m = PerformanceMetrics(exits_by_reason={})
    m.total_trades = len(trades)

    if trades:
        pnls = np.array([t.net_pnl for t in trades], dtype="float64")
        rs = np.array([t.r_multiple for t in trades], dtype="float64")
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        breakeven = pnls[pnls == 0]

        m.wins, m.losses, m.breakeven = len(wins), len(losses), len(breakeven)
        m.win_rate_pct = _safe_div(len(wins), m.total_trades) * 100.0
        gross_profit = float(wins.sum())
        gross_loss = float(-losses.sum())
        m.profit_factor = _safe_div(gross_profit, gross_loss, float("inf") if gross_profit else 0.0)
        m.expectancy = float(pnls.mean())
        m.expectancy_r = float(rs.mean())
        m.average_r = m.expectancy_r
        m.total_r = float(rs.sum())
        m.average_win = float(wins.mean()) if len(wins) else 0.0
        m.average_loss = float(losses.mean()) if len(losses) else 0.0
        m.largest_win = float(pnls.max())
        m.largest_loss = float(pnls.min())
        m.payoff_ratio = _safe_div(m.average_win, abs(m.average_loss))
        m.total_commission = float(sum(t.commission for t in trades))
        m.commission_per_trade = _safe_div(m.total_commission, m.total_trades)
        m.average_bars_held = float(np.mean([t.bars_held for t in trades]))
        m.average_duration_minutes = float(np.mean([t.duration_minutes for t in trades]))
        m.max_consecutive_wins, m.max_consecutive_losses = _streaks(pnls)

        reasons: dict[str, int] = {}
        for trade in trades:
            key = str(trade.exit_reason)
            reasons[key] = reasons.get(key, 0) + 1
        m.exits_by_reason = dict(sorted(reasons.items(), key=lambda kv: -kv[1]))

        m.net_profit = float(pnls.sum())

    # --- equity-based metrics ------------------------------------------------
    if equity_curve:
        equities = np.array([s.equity for s in equity_curve], dtype="float64")
        times = [s.time for s in equity_curve]
        open_positions = np.array([s.open_positions for s in equity_curve], dtype="float64")

        if not m.total_trades:
            m.net_profit = float(equities[-1] - initial_balance)

        peak = np.maximum.accumulate(equities)
        drawdowns = peak - equities
        dd_pct = np.divide(drawdowns, peak, out=np.zeros_like(drawdowns), where=peak > 0) * 100.0
        m.max_drawdown = float(drawdowns.max())
        m.max_drawdown_pct = float(dd_pct.max())
        m.max_drawdown_duration_days = _max_dd_duration(times, drawdowns)

        m.return_pct = _safe_div(equities[-1] - initial_balance, initial_balance) * 100.0

        per_bar_returns = np.diff(equities) / np.where(equities[:-1] == 0, np.nan, equities[:-1])
        per_bar_returns = per_bar_returns[np.isfinite(per_bar_returns)]
        if len(per_bar_returns) > 2:
            mean_ret = float(per_bar_returns.mean())
            std_ret = float(per_bar_returns.std(ddof=1))
            m.volatility_annual_pct = std_ret * math.sqrt(bars_per_year) * 100.0
            rf_per_bar = risk_free_rate / bars_per_year
            m.sharpe = _safe_div((mean_ret - rf_per_bar) * bars_per_year, std_ret * math.sqrt(bars_per_year))
            downside = per_bar_returns[per_bar_returns < 0]
            if len(downside) > 1:
                downside_std = float(downside.std(ddof=1))
                m.sortino = _safe_div(
                    (mean_ret - rf_per_bar) * bars_per_year, downside_std * math.sqrt(bars_per_year)
                )

        # Annualised return over the actual elapsed time.
        #
        # Only reported once there is enough history to annualise honestly: from
        # a two-day sample, any CAGR is an artefact of the exponent, not a
        # forecast (and exponentiating a few minutes to a year overflows).
        if len(times) >= 2:
            years = (times[-1] - times[0]).total_seconds() / (365.25 * 86_400)
            growth = _safe_div(equities[-1], initial_balance)
            if growth > 0 and years >= MIN_YEARS_FOR_CAGR:
                with np.errstate(over="ignore"):
                    m.cagr_pct = (growth ** (1.0 / years) - 1.0) * 100.0
                if not math.isfinite(m.cagr_pct):
                    m.cagr_pct = 0.0

        m.calmar = _safe_div(m.cagr_pct, m.max_drawdown_pct)
        m.time_in_market_pct = float((open_positions > 0).mean() * 100.0)
        if len(times) >= 2:
            days = max((times[-1] - times[0]).total_seconds() / 86_400, 1e-9)
            m.trades_per_day = _safe_div(m.total_trades, days)

    if not equity_curve and not trades:
        return m

    if not math.isfinite(m.profit_factor):
        m.profit_factor = float("inf")
    return m


def _streaks(pnls: np.ndarray) -> tuple[int, int]:
    """Longest run of wins and of losses (a 0-P&L trade breaks both)."""
    best_win = best_loss = cur_win = cur_loss = 0
    for pnl in pnls:
        if pnl > 0:
            cur_win += 1
            cur_loss = 0
        elif pnl < 0:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = cur_loss = 0
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return best_win, best_loss


def _max_dd_duration(times: Sequence[Any], drawdowns: np.ndarray) -> float:
    """Longest stretch (in days) spent below a previous equity peak."""
    if len(drawdowns) < 2:
        return 0.0
    longest = 0.0
    peak_time = times[0]
    in_dd = False
    for i, dd in enumerate(drawdowns):
        if dd > 0 and not in_dd:
            in_dd = True
            peak_time = times[i - 1] if i > 0 else times[0]
        elif dd == 0 and in_dd:
            in_dd = False
            longest = max(longest, (times[i] - peak_time).total_seconds() / 86_400)
    if in_dd:
        longest = max(longest, (times[-1] - peak_time).total_seconds() / 86_400)
    return longest


def drawdown_series(equity_curve: Iterable[AccountSnapshot]) -> pd.DataFrame:
    """Peak-to-trough drawdown over time, for plotting."""
    snapshots = list(equity_curve)
    if not snapshots:
        return pd.DataFrame(columns=["equity", "peak", "drawdown", "drawdown_pct"])
    frame = pd.DataFrame(
        {"time": [s.time for s in snapshots], "equity": [s.equity for s in snapshots]}
    ).set_index("time")
    frame["peak"] = frame["equity"].cummax()
    frame["drawdown"] = frame["peak"] - frame["equity"]
    frame["drawdown_pct"] = frame["drawdown"] / frame["peak"].replace(0, np.nan) * 100.0
    return frame


def monthly_returns(
    equity_curve: Iterable[AccountSnapshot], initial_balance: float | None = None
) -> pd.DataFrame:
    """Percentage return per calendar month (a fast sanity check on stability).

    Uses an explicit year/month groupby rather than `resample("ME")`: resample on
    a timezone-aware index silently collapses buckets under some pandas versions,
    which reports every month as 0.00% — worse than no report at all.

    When `initial_balance` is supplied the first month is measured against it
    instead of being dropped, so a one-month backtest still reports something.
    """
    snapshots = list(equity_curve)
    if not snapshots:
        return pd.DataFrame(columns=["year", "month", "return_pct"])

    frame = pd.DataFrame(
        {"time": [s.time for s in snapshots], "equity": [s.equity for s in snapshots]}
    ).set_index("time")
    grouped = frame["equity"].groupby([frame.index.year, frame.index.month]).last()
    grouped.index.names = ["year", "month"]

    rows = []
    previous: float | None = initial_balance
    for (year, month), equity in grouped.items():
        if previous:
            rows.append(
                {"year": int(year), "month": int(month), "return_pct": (equity / previous - 1.0) * 100.0}
            )
        previous = float(equity)
    return pd.DataFrame(rows, columns=["year", "month", "return_pct"])


def trades_histogram(trades: Sequence[Trade], bins: int = 30) -> tuple[np.ndarray, np.ndarray]:
    """Histogram of trade P&L in R multiples (shows whether the edge is real)."""
    if not trades:
        return np.array([]), np.array([])
    rs = np.array([t.r_multiple for t in trades], dtype="float64")
    return np.histogram(rs, bins=bins)
