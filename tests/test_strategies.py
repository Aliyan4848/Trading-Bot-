"""Strategy contract: valid signals, no lookahead, sane parameter validation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scalper.data import generate_series
from scalper.strategies import (
    BollingerReversion,
    CompositeStrategy,
    EmaRsiMomentum,
    VwapPullback,
    available_strategies,
    get_strategy,
)

BUILTINS = ["ema_rsi_momentum", "bollinger_reversion", "vwap_pullback"]


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    df = generate_series("EURUSD", bars=30_000, seed=11)
    df.attrs["pip_size"] = 0.0001
    return df


@pytest.mark.parametrize("name", BUILTINS)
def test_every_strategy_is_registered_and_constructible(name: str):
    assert name in available_strategies()
    strategy = get_strategy(name, symbol="EURUSD")
    assert strategy.name == name
    assert strategy.min_bars > 0


@pytest.mark.parametrize("name", BUILTINS)
def test_signal_contract(name: str, frame: pd.DataFrame):
    strategy = get_strategy(name, symbol="EURUSD")
    prepared = strategy.prepare(frame)
    signals = prepared.signals

    assert list(signals.columns) == ["signal", "stop_pips", "tp_pips", "reason"]
    assert signals.index.equals(frame.index)
    assert set(signals["signal"].unique()) <= {-1, 0, 1}

    # Stops are NaN while indicators warm up, then always defined afterwards —
    # so the engine can size any bar it is allowed to trade.
    after_warmup = signals.iloc[strategy.min_bars :]
    assert np.isfinite(after_warmup["stop_pips"].to_numpy()).all()

    active = signals[signals["signal"] != 0]
    if len(active):
        # A signal must always carry a usable stop and a reason.
        assert (active["stop_pips"] > 0).all()
        assert (active["reason"].astype(str).str.len() > 0).all()
        # A target, when present, must at least be positive.
        with_tp = active[active["tp_pips"].notna()]
        assert (with_tp["tp_pips"] > 0).all()


@pytest.mark.parametrize("name", BUILTINS)
def test_no_signal_before_the_warmup(name: str, frame: pd.DataFrame):
    strategy = get_strategy(name, symbol="EURUSD")
    prepared = strategy.prepare(frame.iloc[: strategy.min_bars - 1])
    assert (prepared.signals["signal"] == 0).all()


@pytest.mark.parametrize("name", BUILTINS)
def test_strategies_never_look_ahead(name: str, frame: pd.DataFrame):
    """Recomputing on truncated data must not change any earlier signal.

    This is the guard against an indicator accidentally using centred windows or
    forward fills. It is the cheapest, strongest lookahead test there is.
    """
    strategy = get_strategy(name, symbol="EURUSD")
    full = strategy.prepare(frame).signals
    cut = len(frame) - 2_000
    partial = strategy.prepare(frame.iloc[:cut]).signals

    assert np.array_equal(
        full["signal"].to_numpy()[:cut], partial["signal"].to_numpy()
    ), f"{name} changes past signals when future data is removed"

    a = full["stop_pips"].to_numpy()[:cut]
    b = partial["stop_pips"].to_numpy()[:cut]
    both = np.isfinite(a) & np.isfinite(b)
    assert np.allclose(a[both], b[both]), f"{name} stop distances depend on future data"


@pytest.mark.parametrize("name", BUILTINS)
def test_flat_market_never_signals(name: str):
    """A dead flat market (no range, no trend) must not generate entries."""
    index = pd.date_range("2024-01-02 09:00", periods=2000, freq="1min", tz="UTC", name="time")
    frame = pd.DataFrame(
        {
            "open": 1.1000, "high": 1.1000, "low": 1.1000, "close": 1.1000,
            "volume": 0.0,
        },
        index=index,
    )
    frame.attrs["pip_size"] = 0.0001
    prepared = get_strategy(name, symbol="EURUSD").prepare(frame)
    # ATR is zero, so any momentum/reversion trigger must be filtered out.
    assert prepared.signals["signal"].abs().sum() == 0


@pytest.mark.parametrize("name", BUILTINS)
def test_empty_and_tiny_frames_are_handled(name: str):
    strategy = get_strategy(name, symbol="EURUSD")
    empty = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": []},
        index=pd.DatetimeIndex([], tz="UTC"),
    )
    empty.attrs["pip_size"] = 0.0001
    prepared = strategy.prepare(empty)
    assert len(prepared.signals) == 0


def test_unknown_parameter_is_rejected():
    with pytest.raises(ValueError) as exc:
        get_strategy("ema_rsi_momentum", symbol="EURUSD", not_a_param=5)
    assert "unknown parameter" in str(exc.value).lower()


def test_invalid_parameter_combinations_are_rejected():
    with pytest.raises(ValueError):
        EmaRsiMomentum(symbol="EURUSD", fast_ema=50, slow_ema=20)
    with pytest.raises(ValueError):
        EmaRsiMomentum(symbol="EURUSD", min_atr_pips=10, max_atr_pips=5)
    with pytest.raises(ValueError):
        BollingerReversion(symbol="EURUSD", target="somewhere")
    with pytest.raises(ValueError):
        VwapPullback(symbol="EURUSD", vwap_reset="hourly")


def test_allow_shorts_false_only_produces_longs(frame: pd.DataFrame):
    strategy = EmaRsiMomentum(
        symbol="EURUSD", one_signal_per_cross=False, allow_shorts=False, min_atr_pips=0.1
    )
    signals = strategy.prepare(frame).signals
    assert (signals["signal"] >= 0).all()
    assert (signals["signal"] == 1).any()


def test_momentum_strategy_needs_volatility_to_trade(frame: pd.DataFrame):
    """With an impossible ATR floor, the strategy must stay flat."""
    quiet = EmaRsiMomentum(symbol="EURUSD", min_atr_pips=10_000, max_atr_pips=20_000)
    assert quiet.prepare(frame).signals["signal"].abs().sum() == 0


def test_composite_requires_agreement():
    index = pd.date_range("2024-01-02 09:00", periods=500, freq="1min", tz="UTC", name="time")
    rng = np.random.default_rng(4)
    close = pd.Series(1.1 + np.cumsum(rng.normal(0, 0.0004, 500)), index=index)
    frame = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 0.0003,
            "low": close - 0.0003,
            "close": close,
            "volume": 0.0,
        },
        index=index,
    )
    frame.attrs["pip_size"] = 0.0001

    legs = [
        EmaRsiMomentum(symbol="EURUSD", min_atr_pips=0.1),
        VwapPullback(symbol="EURUSD"),
    ]
    composite = CompositeStrategy(symbol="EURUSD", strategies=legs, min_votes=2)
    prepared = composite.prepare(frame)
    assert len(prepared.signals) == len(frame)
    # With min_votes=2 the composite may only signal where both legs agree.
    leg_signals = [leg.prepare(frame).signals["signal"].to_numpy() for leg in legs]
    agree = (leg_signals[0] != 0) & (leg_signals[1] != 0) & (leg_signals[0] == leg_signals[1])
    assert set(prepared.signals["signal"].to_numpy()[~agree]) <= {0}


def test_composite_needs_at_least_one_leg():
    with pytest.raises(ValueError):
        CompositeStrategy(symbol="EURUSD", strategies=[])


def test_strategy_repr_lists_parameters():
    text = repr(EmaRsiMomentum(symbol="EURUSD"))
    assert "fast_ema" in text and "reward_risk" in text


def test_pip_size_comes_from_frame_attrs():
    """Stop distances must be expressed in the instrument's pips, not hardcoded."""
    df = generate_series("USDJPY", bars=5_000, seed=2, base_price=149.5)
    df.attrs["pip_size"] = 0.01
    prepared = EmaRsiMomentum(symbol="USDJPY", min_atr_pips=0.1).prepare(df)

    df_wrong = df.copy()
    df_wrong.attrs["pip_size"] = 0.0001
    prepared_wrong = EmaRsiMomentum(symbol="USDJPY", min_atr_pips=0.1).prepare(df_wrong)

    a = prepared.signals["stop_pips"].dropna()
    b = prepared_wrong.signals["stop_pips"].dropna()
    assert not a.empty and not b.empty
    # A 100x smaller pip must produce 100x larger pip counts.
    assert a.iloc[-1] * 100 == pytest.approx(b.iloc[-1], rel=1e-6)
