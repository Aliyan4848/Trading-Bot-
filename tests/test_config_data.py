"""Config loading/validation and the data layer."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from conftest import make_frame

from scalper.config import (
    TIMEFRAME_MINUTES,
    AppConfig,
    InstrumentSpec,
    bars_per_year,
    is_live_allowed,
    load_config,
    load_env_file,
)
from scalper.data import (
    CsvFeed,
    DataError,
    SyntheticFeed,
    build_feed,
    generate_series,
    infer_pip_size,
    normalize_ohlc,
    resample_bars,
    validate_dataframe,
)
from scalper.data.csv_feed import write_csv


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
def test_loads_the_shipped_config(config_path: Path):
    cfg = load_config(config_path)
    assert isinstance(cfg, AppConfig)
    assert cfg.broker.mode in ("paper", "mt5")
    assert cfg.instruments and cfg.symbols
    assert cfg.strategy.name
    assert cfg.risk.risk_per_trade_pct > 0


def test_instrument_lookup_is_case_insensitive(config_path: Path):
    cfg = load_config(config_path)
    assert cfg.instrument("eurusd").symbol == "EURUSD"


def test_unknown_symbol_raises_with_helpful_message(config_path: Path):
    cfg = load_config(config_path)
    with pytest.raises(KeyError) as exc:
        cfg.instrument("NOPEUSD")
    assert "instruments" in str(exc.value)


def test_unknown_key_is_rejected(tmp_path: Path, config_path: Path):
    raw = yaml.safe_load(config_path.read_text())
    raw["risk"]["not_a_real_setting"] = 1
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.dump(raw))

    with pytest.raises(ValueError) as exc:
        load_config(bad)
    assert "not_a_real_setting" in str(exc.value)


def test_overrides_apply_without_touching_the_file(config_path: Path):
    cfg = load_config(
        config_path,
        {
            "risk.risk_per_trade_pct": 1.25,
            "strategy.name": "vwap_pullback",
            "data.synthetic.bars": 1000,
        },
    )
    assert cfg.risk.risk_per_trade_pct == 1.25
    assert cfg.strategy.name == "vwap_pullback"
    assert cfg.data.synthetic.bars == 1000


def test_overrides_can_replace_the_instrument_list(config_path: Path):
    cfg = load_config(
        config_path,
        {"instruments": [{"symbol": "EURUSD", "pip_size": 0.0001, "pip_value_per_lot": 10.0}]},
    )
    assert cfg.symbols == ["EURUSD"]


def test_invalid_values_are_caught(tmp_path: Path, config_path: Path):
    raw = yaml.safe_load(config_path.read_text())

    raw["risk"]["risk_per_trade_pct"] = 0
    bad = tmp_path / "zero_risk.yaml"
    bad.write_text(yaml.dump(raw))
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        load_config(bad)

    raw = yaml.safe_load(config_path.read_text())
    raw["data"]["timeframe"] = "M7"
    bad2 = tmp_path / "bad_tf.yaml"
    bad2.write_text(yaml.dump(raw))
    with pytest.raises(ValueError, match="timeframe"):
        load_config(bad2)


def test_missing_config_file_is_a_clear_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_bars_per_year_matches_the_timeframe():
    assert bars_per_year("M1") == pytest.approx(374_400)
    assert bars_per_year("M5") == pytest.approx(374_400 / 5)
    assert bars_per_year("H1") == pytest.approx(374_400 / 60)
    with pytest.raises(ValueError):
        bars_per_year("M7")
    assert set(TIMEFRAME_MINUTES) >= {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}


def test_instrument_pip_helpers():
    spec = InstrumentSpec("EURUSD", 0.0001, 100_000, 10.0, 1.0, 5)
    assert spec.pips(0.0010) == pytest.approx(10.0)
    assert spec.price_from_pips(10) == pytest.approx(0.0010)
    assert spec.value_of_pips(10, 0.5) == pytest.approx(50.0)


def test_live_flag_requires_the_exact_optin(monkeypatch):
    monkeypatch.delenv("SCALPER_ALLOW_LIVE", raising=False)
    assert is_live_allowed() is False
    monkeypatch.setenv("SCALPER_ALLOW_LIVE", "no")
    assert is_live_allowed() is False
    monkeypatch.setenv("SCALPER_ALLOW_LIVE", "YES")
    assert is_live_allowed() is True


def test_env_file_parsing(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "MT5_LOGIN=12345\n"
        'MT5_SERVER="Broker-Demo"\n'
        "\n"
        "SCALPER_ALLOW_LIVE=yes\n"
        "  WEIRD = spaced  \n"
    )
    monkeypatch.delenv("MT5_LOGIN", raising=False)
    loaded = load_env_file(env)
    assert loaded["MT5_LOGIN"] == "12345"
    assert loaded["MT5_SERVER"] == "Broker-Demo"
    assert os.environ["SCALPER_ALLOW_LIVE"] == "yes"

    assert load_env_file(tmp_path / "missing.env") == {}


# -----------------------------------------------------------------------------
# Data normalisation
# -----------------------------------------------------------------------------
def test_normalize_renames_common_aliases():
    raw = pd.DataFrame(
        {
            "Date": ["2024-01-01 10:00", "2024-01-01 10:01"],
            "Open": [1.1, 1.1],
            "High": [1.2, 1.2],
            "Low": [1.0, 1.0],
            "Close": [1.15, 1.15],
            "TickVol": [10, 12],
        }
    )
    out = normalize_ohlc(raw, "EURUSD")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.tz is not None
    assert out["volume"].iloc[0] == 10


def test_normalize_handles_mt5_date_and_time_columns():
    raw = pd.DataFrame(
        {
            "<DATE>": ["2024.03.05", "2024.03.05"],
            "<TIME>": ["08:00:00", "08:01:00"],
            "<OPEN>": [1.1, 1.1],
            "<HIGH>": [1.2, 1.2],
            "<LOW>": [1.0, 1.0],
            "<CLOSE>": [1.15, 1.15],
            "<VOL>": [5, 6],
        }
    )
    out = normalize_ohlc(raw)
    assert str(out.index[0]) == "2024-03-05 08:00:00+00:00"


def test_normalize_handles_unix_epoch_seconds():
    raw = pd.DataFrame({"time": [1704067200, 1704067260], "open": [1, 1], "high": [2, 2],
                        "low": [0.5, 0.5], "close": [1.5, 1.5]})
    out = normalize_ohlc(raw)
    assert str(out.index[0]) == "2024-01-01 00:00:00+00:00"


def test_normalize_localises_naive_timestamps_to_the_configured_zone():
    raw = pd.DataFrame({"time": ["2024-01-01 08:00"], "open": [1.1], "high": [1.1],
                        "low": [1.1], "close": [1.1]})
    utc = normalize_ohlc(raw, tz="UTC")
    ny = normalize_ohlc(raw, tz="America/New_York")
    assert utc.index[0].tzinfo is not None
    assert (ny.index[0] - utc.index[0]).total_seconds() == 5 * 3600


def test_normalize_repairs_impossible_ohlc_rows():
    raw = pd.DataFrame(
        {
            "time": ["2024-01-01 10:00", "2024-01-01 10:01"],
            "open": [1.10, 1.10],
            "high": [1.05, 1.20],   # high below the body: impossible
            "low": [1.00, 1.00],
            "close": [1.15, 1.15],
        }
    )
    out = normalize_ohlc(raw)
    assert (out["high"] >= out[["open", "close"]].max(axis=1)).all()
    assert (out["low"] <= out[["open", "close"]].min(axis=1)).all()


def test_normalize_drops_duplicates_and_sorts():
    raw = pd.DataFrame(
        {
            "time": ["2024-01-01 10:01", "2024-01-01 10:00", "2024-01-01 10:01"],
            "open": [1.1, 1.1, 9.9],
            "high": [1.2, 1.2, 9.9],
            "low": [1.0, 1.0, 9.9],
            "close": [1.1, 1.1, 9.9],
        }
    )
    out = normalize_ohlc(raw)
    assert len(out) == 2
    assert out.index.is_monotonic_increasing
    assert out["close"].iloc[-1] == pytest.approx(9.9)  # last duplicate wins


def test_normalize_errors_are_actionable():
    with pytest.raises(DataError, match="timestamp"):
        normalize_ohlc(pd.DataFrame({"a": [1], "open": [1], "high": [1], "low": [1], "close": [1]}))
    with pytest.raises(DataError, match="OHLC"):
        normalize_ohlc(pd.DataFrame({"time": ["2024-01-01"], "price": [1.0]}))
    with pytest.raises(DataError):
        normalize_ohlc(pd.DataFrame())


def test_validation_warns_about_short_and_gappy_history():
    frame = make_frame([1.1] * 5)
    warnings = validate_dataframe(frame, "EURUSD")
    assert any("too short" in w for w in warnings)

    # A 5-day hole should be flagged, but a normal weekend should not.
    good = make_frame(list(np.linspace(1.1, 1.11, 200)), start="2024-01-01")
    assert not any("gap" in w for w in validate_dataframe(good, "EURUSD"))


def test_resample_aggregates_correctly():
    frame = make_frame([1.1000, 1.1010, 1.0990, 1.1020, 1.1030],
                       highs=[1.1010] * 5, lows=[1.0990] * 5,
                       start="2024-01-01 00:00")
    out = resample_bars(frame, "M5")
    assert len(out) == 1
    row = out.iloc[0]
    assert row["open"] == pytest.approx(1.1000)
    assert row["close"] == pytest.approx(1.1030)
    assert row["high"] == pytest.approx(1.1010)
    assert row["low"] == pytest.approx(1.0990)


def test_csv_round_trip(tmp_path: Path, config_path: Path):
    cfg = load_config(config_path, {"data.synthetic.bars": 2000})
    frame = build_feed(cfg).load()["EURUSD"]
    path = write_csv(frame, tmp_path / "EURUSD_M1.csv")
    back = CsvFeed(path=str(path), symbols=["EURUSD"]).load()["EURUSD"]

    assert len(back) == len(frame)
    assert np.allclose(back["close"].to_numpy(), frame["close"].to_numpy())
    assert back.index.equals(frame.index)


def test_csv_feed_supports_a_symbol_placeholder(tmp_path: Path):
    for symbol in ("EURUSD", "GBPUSD"):
        frame = make_frame([1.1, 1.1001, 1.1002] * 10)
        write_csv(frame, tmp_path / f"{symbol}_M1.csv")
    feed = CsvFeed(path=str(tmp_path / "{symbol}_M1.csv"), symbols=["EURUSD", "GBPUSD"])
    frames = feed.load()
    assert set(frames) == {"EURUSD", "GBPUSD"}


def test_csv_feed_reports_a_missing_file_clearly(tmp_path: Path):
    with pytest.raises(DataError) as exc:
        CsvFeed(path=str(tmp_path / "nope.csv"), symbols=["EURUSD"]).load()
    assert "not found" in str(exc.value)


def test_synthetic_generation_is_deterministic_and_valid():
    a = generate_series("EURUSD", bars=5000, seed=42)
    b = generate_series("EURUSD", bars=5000, seed=42)
    c = generate_series("EURUSD", bars=5000, seed=43)

    pd.testing.assert_frame_equal(a, b)
    assert not a["close"].equals(c["close"])

    assert (a["high"] >= a[["open", "close"]].max(axis=1) - 1e-12).all()
    assert (a["low"] <= a[["open", "close"]].min(axis=1) + 1e-12).all()
    assert (a["high"] >= a["low"]).all()
    assert (a[["open", "high", "low", "close"]] > 0).all().all()
    assert a.index.tz is not None
    assert a.index.is_monotonic_increasing
    assert not a.index.duplicated().any()


def test_synthetic_respects_the_fx_calendar():
    frame = generate_series("EURUSD", bars=20_000, seed=1)
    # No Saturday bars at all, and Sunday only after the 21:00 reopen.
    saturdays = frame.index[frame.index.dayofweek == 5]
    assert len(saturdays) == 0
    sundays = frame.index[frame.index.dayofweek == 6]
    assert all(ts.hour >= 21 for ts in sundays)


def test_synthetic_prices_stay_in_a_plausible_range():
    """Regression: drift scaled by regime length compounded EURUSD to 27.0.

    A generator that produces absurd price levels quietly invalidates every
    pip-based number downstream (stop distances, position sizes, P&L).
    """
    frame = generate_series("EURUSD", bars=200_000, seed=7, base_price=1.0850)
    low, high, final = frame["close"].min(), frame["close"].max(), frame["close"].iloc[-1]

    assert 0.5 * 1.0850 < low < 1.5 * 1.0850
    assert 0.5 * 1.0850 < high < 1.5 * 1.0850
    assert 0.5 * 1.0850 < final < 1.5 * 1.0850


def test_synthetic_realized_volatility_is_believable():
    """The generator's annual_vol setting should roughly show up in the data."""
    frame = generate_series("EURUSD", bars=200_000, seed=7, base_price=1.0850, annual_vol=0.08)
    returns = np.log(frame["close"]).diff().dropna()
    annualised = float(returns.std()) * np.sqrt(374_400)
    # Fat tails and session seasonality push this above the nominal 8%, but it
    # must stay in the same order of magnitude.
    assert 0.04 < annualised < 0.30


def test_synthetic_feed_produces_uncorrelated_symbols():
    feed = SyntheticFeed(["EURUSD", "GBPUSD"], bars=5000, seed=3)
    frames = feed.load()
    assert set(frames) == {"EURUSD", "GBPUSD"}
    corr = frames["EURUSD"]["close"].pct_change().corr(frames["GBPUSD"]["close"].pct_change())
    assert abs(corr) < 0.5  # different seeds -> not clones of each other


def test_build_feed_selects_the_configured_source(config_path: Path):
    cfg = load_config(config_path, {"data.source": "synthetic", "data.synthetic.bars": 500})
    frames = build_feed(cfg).load()
    assert cfg.symbols[0] in frames

    cfg_csv = load_config(config_path, {"data.source": "csv", "data.csv.path": "data/{symbol}_M1.csv"})
    assert build_feed(cfg_csv).__class__.__name__ == "CsvFeed"

    with pytest.raises(ValueError):
        build_feed(load_config(config_path, {"data.source": "telepathy"}))


def test_infer_pip_size_guesses_by_price_magnitude():
    assert infer_pip_size(1.1050) == pytest.approx(0.0001)
    assert infer_pip_size(149.50) == pytest.approx(0.01)
    assert infer_pip_size(2050.0) == pytest.approx(0.1)
