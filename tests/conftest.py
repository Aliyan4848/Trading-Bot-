"""Shared fixtures.

Test data is deliberately tiny and hand-checkable: when a test fails, you should
be able to see the expected number by looking at the bars in the test itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(SRC), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from scalper.config import AppConfig, InstrumentSpec, load_config  # noqa: E402


@pytest.fixture
def config_path() -> Path:
    return ROOT / "config" / "config.yaml"


@pytest.fixture
def cfg(config_path: Path) -> AppConfig:
    return load_config(config_path)


@pytest.fixture
def eurusd() -> InstrumentSpec:
    """A EURUSD spec with round numbers: 1 pip = 0.0001, 1 lot = 10 USD/pip."""
    return InstrumentSpec(
        symbol="EURUSD",
        pip_size=0.0001,
        contract_size=100_000,
        pip_value_per_lot=10.0,
        spread_pips=1.0,
        digits=5,
    )


def make_frame(
    prices: list[float],
    *,
    start: str = "2024-01-01 08:00",
    freq: str = "1min",
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    volume: float = 0.0,
) -> pd.DataFrame:
    """Build an OHLC frame from a list of closes (handy for exact-fill tests)."""
    n = len(prices)
    closes = np.array(prices, dtype="float64")
    opens_arr = np.array(opens, dtype="float64") if opens else np.concatenate([[closes[0]], closes[:-1]])
    highs_arr = np.array(highs, dtype="float64") if highs else np.maximum(opens_arr, closes) + 0.0005
    lows_arr = np.array(lows, dtype="float64") if lows else np.minimum(opens_arr, closes) - 0.0005
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC", name="time")
    return pd.DataFrame(
        {
            "open": opens_arr,
            "high": highs_arr,
            "low": lows_arr,
            "close": closes,
            "volume": np.full(n, volume, dtype="float64"),
        },
        index=index,
    )


@pytest.fixture
def flat_frame() -> pd.DataFrame:
    """20 flat bars at 1.1000 — the simplest possible market."""
    return make_frame([1.1000] * 20)


@pytest.fixture
def uptrend_frame() -> pd.DataFrame:
    """A steady 1-pip-per-bar uptrend."""
    return make_frame([1.1000 + i * 0.0001 for i in range(50)])
