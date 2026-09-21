"""Market data sources."""

from __future__ import annotations

import os
from typing import Any

from .base import (
    DataError,
    DataFeed,
    FrameFeed,
    build_timeline,
    forward_fill_bars,
    infer_pip_size,
    normalize_ohlc,
    resample_bars,
    validate_dataframe,
)
from .csv_feed import CsvFeed, MultiFileCsvFeed, write_csv
from .synthetic import SyntheticFeed, generate_series

__all__ = [
    "DataError",
    "DataFeed",
    "FrameFeed",
    "build_timeline",
    "forward_fill_bars",
    "infer_pip_size",
    "normalize_ohlc",
    "resample_bars",
    "validate_dataframe",
    "CsvFeed",
    "MultiFileCsvFeed",
    "write_csv",
    "SyntheticFeed",
    "generate_series",
    "build_feed",
]


def build_feed(cfg: Any) -> DataFeed:
    """Construct the configured data feed.

    Kept as a factory function so the import of a Windows-only package (MT5)
    never happens unless it is actually used.
    """
    source = str(cfg.data.source).lower()
    symbols = cfg.symbols
    timeframe = str(cfg.data.timeframe).upper()

    if source == "synthetic":
        synth = cfg.data.synthetic
        return SyntheticFeed(
            symbols=synth.symbols or symbols,
            bars=synth.bars,
            start=synth.start,
            seed=synth.seed,
            annual_vol=synth.annual_vol,
            trend_strength=synth.trend_strength,
            regime_flip_bars=synth.regime_flip_bars,
            spread_pips=synth.spread_pips,
        )

    if source == "csv":
        return CsvFeed(
            path=cfg.data.csv.path,
            symbols=symbols,
            timestamp_column=cfg.data.csv.timestamp_column,
            tz=cfg.data.csv.tz,
        )

    if source == "mt5":
        from .mt5_feed import Mt5Feed  # lazy: Windows-only dependency

        login = os.environ.get("MT5_LOGIN")
        return Mt5Feed(
            symbols=symbols,
            timeframe=timeframe,
            bars=cfg.data.mt5.bars,
            login=int(login) if login else None,
            password=os.environ.get("MT5_PASSWORD"),
            server=os.environ.get("MT5_SERVER"),
            path=os.environ.get("MT5_PATH"),
        )

    raise ValueError(
        f"Unknown data.source {source!r}. Use one of: synthetic, csv, mt5."
    )
