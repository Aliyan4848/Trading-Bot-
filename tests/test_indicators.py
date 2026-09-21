"""Indicator correctness, including the edge cases that break naive versions."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scalper import indicators as ind


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


# -----------------------------------------------------------------------------
# Moving averages
# -----------------------------------------------------------------------------
def test_sma_matches_hand_computation():
    s = series([1, 2, 3, 4, 5])
    out = ind.sma(s, 3)
    assert pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_ema_is_seeded_with_the_sma_like_metatrader():
    s = series([1, 2, 3, 4, 5, 6])
    out = ind.ema(s, 3)
    # First value is the SMA of the first 3 -> 2.0
    assert out.iloc[2] == pytest.approx(2.0)
    # Then EMA_t = a*x_t + (1-a)*EMA_{t-1} with a = 2/(3+1) = 0.5
    assert out.iloc[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)
    assert out.iloc[4] == pytest.approx(0.5 * 5 + 0.5 * 3.0)


def test_ema_does_not_publish_values_before_the_period():
    out = ind.ema(series(list(range(10))), 5)
    assert out.iloc[:4].isna().all()
    assert out.iloc[4:].notna().all()


def test_ema_of_a_constant_series_is_that_constant():
    out = ind.ema(series([2.5] * 30), 7)
    assert out.dropna().nunique() == 1
    assert out.dropna().iloc[0] == pytest.approx(2.5)


# -----------------------------------------------------------------------------
# RSI
# -----------------------------------------------------------------------------
def test_rsi_bounds_and_monotonic_cases():
    rising = series([float(i) for i in range(1, 60)])
    falling = series([float(i) for i in range(60, 1, -1)])
    flat = series([5.0] * 40)

    assert ind.rsi(rising, 14).iloc[-1] == pytest.approx(100.0)
    assert ind.rsi(falling, 14).iloc[-1] == pytest.approx(0.0)
    assert ind.rsi(flat, 14).iloc[-1] == pytest.approx(50.0)


def test_rsi_never_leaves_zero_to_one_hundred():
    rng = np.random.default_rng(3)
    prices = series(1.1 + np.cumsum(rng.normal(0, 0.0005, 500)))
    out = ind.rsi(prices, 14).dropna()
    assert out.between(0.0, 100.0).all()
    assert len(out) > 400


def test_rsi_matches_wilders_reference():
    """Known-good RSI values for a textbook input series."""
    data = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
        45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    ]
    out = ind.rsi(series(data), 14)
    # Hand-computed from Wilder's definition (see the docstring of
    # wilder_smooth): the seed uses the first 14 changes, giving
    #   avg gain = 3.34/14 = 0.23857, avg loss = 1.40/14 = 0.10
    #   RS = 2.3857 -> RSI = 70.46
    assert out.iloc[13] == pytest.approx(70.46, abs=0.01)
    # The seed value propagates unchanged to the next bar (a 0.00 change scales
    # both averages by 13/14), then the -0.28 bar gives
    #   avg gain = 0.22153*13/14 = 0.20571, avg loss = (0.092857*13+0.28)/14 = 0.10622
    #   RS = 1.93653 -> RSI = 65.946
    # These two steps are what prove the recursion matches Wilder, not just the seed.
    assert out.iloc[14] == pytest.approx(70.46, abs=0.01)
    assert out.iloc[15] == pytest.approx(65.946, abs=0.01)


# -----------------------------------------------------------------------------
# ATR / volatility
# -----------------------------------------------------------------------------
def test_atr_equals_the_high_low_range_when_there_are_no_gaps():
    close = series([1.1000] * 30)
    high = close + 0.0010
    low = close - 0.0010
    out = ind.atr(high, low, close, 14)
    # True range is a constant 20 pips, so the ATR must converge to it.
    assert out.iloc[-1] == pytest.approx(0.0020)


def test_true_range_accounts_for_gaps():
    close = series([1.1000, 1.1100])
    high = series([1.1010, 1.1110])
    low = series([1.0990, 1.1090])
    tr = ind.true_range(high, low, close)
    # Second bar: |high - prev close| = 0.0110 beats the 0.0020 range.
    assert tr.iloc[1] == pytest.approx(0.0110)


def test_realized_volatility_is_positive_and_scales_with_noise():
    rng = np.random.default_rng(11)
    calm = series(1.1 + np.cumsum(rng.normal(0, 0.0001, 400)))
    wild = series(1.1 + np.cumsum(rng.normal(0, 0.0010, 400)))
    calm_vol = ind.realized_volatility(calm, 20).dropna().mean()
    wild_vol = ind.realized_volatility(wild, 20).dropna().mean()
    assert calm_vol > 0 and wild_vol > calm_vol * 3


# -----------------------------------------------------------------------------
# Bands / channels
# -----------------------------------------------------------------------------
def test_bollinger_band_geometry():
    s = series([1.0, 2.0, 3.0, 4.0, 5.0])
    middle, upper, lower, bandwidth = ind.bollinger_bands(s, 5, 2.0)
    assert middle.iloc[-1] == pytest.approx(3.0)
    std = s.std(ddof=0)
    assert upper.iloc[-1] == pytest.approx(3.0 + 2 * std)
    assert lower.iloc[-1] == pytest.approx(3.0 - 2 * std)
    assert bandwidth.iloc[-1] == pytest.approx((upper.iloc[-1] - lower.iloc[-1]) / 3.0 * 100)


def test_bollinger_bands_handle_a_dead_flat_market():
    """Zero variance must not produce NaN bands or a divide-by-zero."""
    s = series([1.1] * 25)
    middle, upper, lower, bandwidth = ind.bollinger_bands(s, 20, 2.0)
    assert upper.iloc[-1] == pytest.approx(lower.iloc[-1]) == pytest.approx(1.1)
    assert bandwidth.iloc[-1] == pytest.approx(0.0)


# -----------------------------------------------------------------------------
# ADX
# -----------------------------------------------------------------------------
def test_adx_is_high_in_a_trend_and_low_in_chop():
    n = 200
    trend_close = series(np.linspace(1.1, 1.14, n))
    rng = np.random.default_rng(5)
    chop_close = series(1.1 + rng.normal(0, 0.0002, n))

    def adx_of(close: pd.Series) -> float:
        high = close + 0.0003
        low = close - 0.0003
        return float(ind.adx(high, low, close, 14)[0].dropna().iloc[-1])

    assert adx_of(trend_close) > 50
    assert adx_of(chop_close) < 30


# -----------------------------------------------------------------------------
# VWAP
# -----------------------------------------------------------------------------
def test_vwap_without_volume_falls_back_to_the_mean_price():
    close = series([1.0, 2.0, 3.0])
    out = ind.vwap(close, close, close, series([0.0, 0.0, 0.0]))
    assert out.iloc[-1] == pytest.approx(2.0)


def test_vwap_weights_by_volume():
    high = low = close = series([1.0, 3.0])
    volume = series([1.0, 3.0])
    out = ind.vwap(high, low, close, volume)
    assert out.iloc[-1] == pytest.approx((1.0 * 1 + 3.0 * 3) / 4.0)


def test_rolling_vwap_resets_each_session():
    index = pd.date_range("2024-01-01 23:50", periods=30, freq="1min", tz="UTC")
    close = pd.Series(np.arange(1.0, 31.0), index=index)
    volume = pd.Series(1.0, index=index)
    out = ind.rolling_vwap(close, close, close, volume, 1, reset_daily=True)
    # The first bar of the new day must equal its own price, not a running mean.
    day2 = out[out.index.normalize() == pd.Timestamp("2024-01-02", tz="UTC")]
    assert day2.iloc[0] == pytest.approx(close.iloc[len(close) - len(day2)])
    assert day2.iloc[0] < day2.iloc[-1]


# -----------------------------------------------------------------------------
# Crosses and lookahead safety
# -----------------------------------------------------------------------------
def test_crossed_above_only_fires_on_the_cross_bar():
    slow = series([2.0] * 6)
    fast = series([1.0, 1.0, 2.0, 3.0, 3.0, 3.0])
    out = ind.crossed_above(fast, slow)
    assert list(out) == [False, False, False, True, False, False]


def test_crossed_below_mirrors_crossed_above():
    slow = series([2.0] * 6)
    fast = series([3.0, 3.0, 2.0, 1.0, 1.0, 1.0])
    out = ind.crossed_below(fast, slow)
    assert list(out) == [False, False, False, True, False, False]


def test_indicators_never_use_future_data():
    """Truncating the series must not change any earlier value."""
    rng = np.random.default_rng(17)
    close = series(1.1 + np.cumsum(rng.normal(0, 0.0004, 300)))
    high, low = close + 0.0002, close - 0.0002

    full = {
        "ema": ind.ema(close, 21),
        "rsi": ind.rsi(close, 14),
        "atr": ind.atr(high, low, close, 14),
        "adx": ind.adx(high, low, close, 14)[0],
        "bb": ind.bollinger_bands(close, 20)[1],
    }
    cut = 200
    partial = {
        "ema": ind.ema(close.iloc[:cut], 21),
        "rsi": ind.rsi(close.iloc[:cut], 14),
        "atr": ind.atr(high.iloc[:cut], low.iloc[:cut], close.iloc[:cut], 14),
        "adx": ind.adx(high.iloc[:cut], low.iloc[:cut], close.iloc[:cut], 14)[0],
        "bb": ind.bollinger_bands(close.iloc[:cut], 20)[1],
    }
    for name, full_series in full.items():
        a = full_series.iloc[:cut].to_numpy()
        b = partial[name].to_numpy()
        both_valid = ~np.isnan(a) & ~np.isnan(b)
        assert both_valid.sum() > 100, name
        assert np.allclose(a[both_valid], b[both_valid], atol=1e-12), name
