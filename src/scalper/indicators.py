"""Technical indicators implemented in numpy/pandas.

Deliberately dependency-free (no TA-Lib / pandas-ta): TA-Lib needs a C library,
and pandas-ta is unmaintained. Everything here is vectorised and unit-tested
against hand-computed reference values in `tests/test_indicators.py`.

Convention: indicators return a Series aligned to the input index, with the
first `period - 1` values as NaN. They never look at future bars.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "sma",
    "ema",
    "rsi",
    "atr",
    "true_range",
    "wilder_smooth",
    "bollinger_bands",
    "vwap",
    "rolling_vwap",
    "adx",
    "stochastic",
    "rolling_high",
    "rolling_low",
    "crossed_above",
    "crossed_below",
    "slope",
    "realized_volatility",
]


# -----------------------------------------------------------------------------
# Moving averages
# -----------------------------------------------------------------------------
def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (Wilder-compatible recursion, seeded by SMA).

    Seeding with the SMA of the first `period` values matches MetaTrader's
    built-in `iMA` output, which keeps backtest numbers comparable with what you
    see on an MT5 chart.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    values = pd.Series(series, dtype="float64")
    if len(values) < period:
        return pd.Series(np.nan, index=values.index, dtype="float64")

    alpha = 2.0 / (period + 1.0)
    out = np.full(len(values), np.nan, dtype="float64")
    seed = float(values.iloc[:period].mean())
    out[period - 1] = seed
    arr = values.to_numpy(dtype="float64", copy=False)
    for i in range(period, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=values.index, dtype="float64")


def slope(series: pd.Series, period: int = 1) -> pd.Series:
    """Change over `period` bars (simple momentum)."""
    return series.diff(period)


# -----------------------------------------------------------------------------
# Volatility
# -----------------------------------------------------------------------------
def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA), seeded by the SMA of the first `period` values.

    This is the detail that makes RSI/ATR/ADX match MetaTrader, TradingView and
    Wilder's own tables. ``pandas.ewm(alpha=1/period, adjust=False)`` starts the
    recursion from the *first* observation instead, which shifts the early values
    by several points — enough to change a signal.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    values = pd.Series(series, dtype="float64")
    if len(values) < period:
        return pd.Series(np.nan, index=values.index, dtype="float64")

    arr = values.to_numpy(dtype="float64", copy=False)
    out = np.full(len(arr), np.nan, dtype="float64")
    out[period - 1] = np.nanmean(arr[:period])
    for i in range(period, len(arr)):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return pd.Series(out, index=values.index, dtype="float64")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing (RMA)."""
    return wilder_smooth(true_range(high, low, close), period)


def realized_volatility(close: pd.Series, period: int = 20, annualization: float = 374_400.0) -> pd.Series:
    """Annualised stdev of log returns. Useful as a regime filter."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(period, min_periods=period).std() * np.sqrt(annualization)


# -----------------------------------------------------------------------------
# Oscillators
# -----------------------------------------------------------------------------
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder). Range 0-100."""
    values = pd.Series(series, dtype="float64")
    delta = values.diff()
    gain = delta.clip(lower=0.0).fillna(0.0)
    loss = (-delta).clip(lower=0.0).fillna(0.0)

    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # Monotonic rises (avg_loss == 0) -> RSI 100; monotonic falls -> 0.
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss > 0)), 0.0)
    # Flat series -> neutral
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out


def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14, d_period: int = 3
) -> tuple[pd.Series, pd.Series]:
    """Stochastic oscillator %K and %D."""
    lowest = low.rolling(k_period, min_periods=k_period).min()
    highest = high.rolling(k_period, min_periods=k_period).max()
    span = (highest - lowest).replace(0.0, np.nan)
    k = 100.0 * (close - lowest) / span
    return k, k.rolling(d_period, min_periods=d_period).mean()


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Average Directional Index with +DI / -DI.

    ADX > ~25 indicates a trending market, which is the regime scalping
    momentum strategies need. Below ~20, mean reversion is usually safer.
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    atr_ = wilder_smooth(true_range(high, low, close), period)
    plus_dm_s = wilder_smooth(plus_dm, period)
    minus_dm_s = wilder_smooth(minus_dm, period)

    plus_di = 100.0 * plus_dm_s / atr_.replace(0.0, np.nan)
    minus_di = 100.0 * minus_dm_s / atr_.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return wilder_smooth(dx.fillna(0.0), period), plus_di, minus_di


# -----------------------------------------------------------------------------
# Bands / channels
# -----------------------------------------------------------------------------
def bollinger_bands(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands. Returns (middle, upper, lower, bandwidth%)."""
    middle = sma(series, period)
    std = series.rolling(period, min_periods=period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    bandwidth = (upper - lower) / middle.replace(0.0, np.nan) * 100.0
    return middle, upper, lower, bandwidth


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).max()


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).min()


# -----------------------------------------------------------------------------
# VWAP
# -----------------------------------------------------------------------------
def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Cumulative VWAP over the whole series (typical price weighted by volume)."""
    typical = (high + low + close) / 3.0
    vol = volume.astype("float64")
    if float(vol.fillna(0).abs().sum()) == 0.0:
        # FX spot has no real volume: fall back to an equal-weighted average so
        # VWAP degrades gracefully instead of returning NaN everywhere.
        vol = pd.Series(1.0, index=close.index)
    cum_vol = vol.cumsum()
    cum_pv = (typical * vol).cumsum()
    return cum_pv / cum_vol.replace(0.0, np.nan)


def rolling_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int,
    reset_daily: bool = True,
) -> pd.Series:
    """VWAP that resets each session (the version intraday traders actually use)."""
    typical = (high + low + close) / 3.0
    vol = volume.astype("float64")
    if float(vol.fillna(0).abs().sum()) == 0.0:
        vol = pd.Series(1.0, index=close.index)

    pv = typical * vol
    index = pd.Index(close.index)
    if reset_daily and isinstance(index, pd.DatetimeIndex):
        groups = pd.Series(index.normalize(), index=index)
    else:
        groups = pd.Series(np.zeros(len(index)), index=index)

    cum_vol = vol.groupby(groups).cumsum()
    cum_pv = pv.groupby(groups).cumsum()
    result = cum_pv / cum_vol.replace(0.0, np.nan)

    if not reset_daily:
        # Without a session reset, require a full lookback before publishing.
        valid = pd.Series(np.arange(len(result)), index=result.index) >= (period - 1)
        result = result.where(valid)
    return result


# -----------------------------------------------------------------------------
# Cross helpers
# -----------------------------------------------------------------------------
def crossed_above(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True on the bar where `fast` crosses from at-or-below to above `slow`."""
    prev = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
    return prev.fillna(False).astype(bool)


def crossed_below(fast: pd.Series, slow: pd.Series) -> pd.Series:
    prev = (fast.shift(1) >= slow.shift(1)) & (fast < slow)
    return prev.fillna(False).astype(bool)
