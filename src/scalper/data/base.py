"""Data feed interface plus the OHLC validation used by every data source."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("open", "high", "low", "close")


class DataError(RuntimeError):
    """Raised when market data is unusable (bad OHLC, gaps, no history)."""


class DataFeed(ABC):
    """A source of OHLC bars.

    Implementations return a mapping of ``symbol -> DataFrame`` where the frame
    is indexed by a tz-aware UTC ``DatetimeIndex`` and has columns
    ``open, high, low, close, volume``.
    """

    @abstractmethod
    def load(self) -> dict[str, pd.DataFrame]:
        """Return validated bars per symbol."""

    def describe(self) -> str:  # pragma: no cover - cosmetic
        return self.__class__.__name__


# -----------------------------------------------------------------------------
# Validation / normalisation helpers
# -----------------------------------------------------------------------------
def normalize_ohlc(
    df: pd.DataFrame,
    symbol: str = "",
    timestamp_column: str | None = None,
    tz: str = "UTC",
    drop_bad_rows: bool = True,
) -> pd.DataFrame:
    """Coerce an arbitrary OHLC frame into the canonical schema.

    - Renames common column aliases (Date/Time/TickVol/Vol...).
    - Parses timestamps (accepts MT5's separate ``<DATE>``/``<TIME>`` columns).
    - Localises to UTC, sorts, and drops duplicate bars.
    - Fixes small OHLC inconsistencies and drops rows that are unsalvageable.
    """
    if df is None or len(df) == 0:
        raise DataError(f"No rows to normalise for {symbol or 'data'}")

    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]

    lower_map = {c.lower(): c for c in out.columns}

    def find(*names: str) -> str | None:
        for name in names:
            if name in lower_map:
                return lower_map[name]
        return None

    # --- timestamp -----------------------------------------------------------
    time_col = timestamp_column or find(
        "time", "timestamp", "datetime", "date_time", "gmt time", "date"
    )
    date_col = find("date", "<date>")
    clock_col = find("<time>", "time", "ticktime")

    combined = None
    if date_col and clock_col and clock_col != date_col:
        # MT5 CSV export: <DATE> and <TIME> live in separate columns.
        stamp = out[date_col].astype(str).str.strip() + " " + out[clock_col].astype(str).str.strip()
        candidate = pd.to_datetime(stamp, format="mixed", dayfirst=False, errors="coerce")
        if candidate.notna().mean() > 0.5:
            combined = candidate

    if combined is not None:
        index = combined
    elif time_col:
        raw = out[time_col]
        if pd.api.types.is_numeric_dtype(raw):
            # Unix epoch seconds (MT5 returns seconds since 1970 UTC).
            index = pd.to_datetime(raw, unit="s", errors="coerce", utc=True).dt.tz_localize(None)
        else:
            index = pd.to_datetime(raw, format="mixed", dayfirst=False, errors="coerce")
    else:
        if isinstance(out.index, pd.DatetimeIndex):
            index = pd.Series(out.index, index=out.index)
        else:
            raise DataError(
                f"Could not find a timestamp column in {list(out.columns)} for {symbol or 'data'}"
            )

    index = pd.DatetimeIndex(index)
    if index.tz is None:
        index = index.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    else:
        index = index.tz_convert("UTC")

    out.index = index
    out = out.loc[~out.index.isna()]

    # --- price columns -------------------------------------------------------
    rename: dict[str, str] = {}
    for canonical, aliases in {
        "open": ("open", "o", "<open>", "bidopen", "askopen"),
        "high": ("high", "h", "<high>", "bidhigh", "askhigh"),
        "low": ("low", "l", "<low>", "bidlow", "asklow"),
        "close": ("close", "c", "<close>", "bidclose", "askclose", "last"),
        "volume": ("volume", "vol", "<vol>", "tickvol", "tickvolume", "real volume"),
    }.items():
        found = find(*aliases)
        if found:
            rename[found] = canonical

    out = out.rename(columns=rename)

    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        raise DataError(
            f"{symbol or 'data'} is missing required OHLC column(s) {missing}. "
            f"Found: {list(out.columns)}"
        )
    if "volume" not in out.columns:
        out["volume"] = 0.0

    out = out[["open", "high", "low", "close", "volume"]].astype("float64")
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]

    # --- sanity --------------------------------------------------------------
    out = out.dropna(subset=list(REQUIRED_COLUMNS))
    out = out[(out[["open", "high", "low", "close"]] > 0).all(axis=1)]
    if out.empty:
        raise DataError(f"{symbol or 'data'}: no valid rows after cleaning")

    if not drop_bad_rows:
        return out

    body_high = out[["open", "close"]].max(axis=1)
    body_low = out[["open", "close"]].min(axis=1)
    bad = (out["high"] < body_high) | (out["low"] > body_low) | (out["high"] < out["low"])
    if bad.any():
        # Nudge the wick to at least cover the body rather than discarding real
        # price action (common with aggregated/rounded broker exports).
        out.loc[bad, "high"] = out.loc[bad, ["high", "open", "close"]].max(axis=1)
        out.loc[bad, "low"] = out.loc[bad, ["low", "open", "close"]].min(axis=1)

    out.index.name = "time"
    if symbol:
        out.attrs["symbol"] = symbol
    return out


def validate_dataframe(df: pd.DataFrame, symbol: str, max_gap_multiplier: float = 200.0) -> list[str]:
    """Return a list of human-readable data-quality warnings (never raises)."""
    warnings: list[str] = []
    if df.empty:
        warnings.append(f"{symbol}: empty dataset")
        return warnings

    if not isinstance(df.index, pd.DatetimeIndex):
        warnings.append(f"{symbol}: index is not a DatetimeIndex")
        return warnings

    if not df.index.is_monotonic_increasing:
        warnings.append(f"{symbol}: timestamps are not sorted ascending")

    n_dupes = int(df.index.duplicated().sum())
    if n_dupes:
        warnings.append(f"{symbol}: {n_dupes} duplicate timestamps")

    if len(df) > 2:
        deltas = pd.Series(df.index).diff().dropna().dt.total_seconds()
        median = float(deltas.median()) if len(deltas) else 0.0
        if median > 0:
            # Allow the normal FX weekend (Fri 21:00 -> Sun 21:00 = 48h) plus a
            # margin for holidays before flagging a gap as suspicious.
            weekend_seconds = 3 * 86_400.0
            threshold = max(median * max_gap_multiplier, weekend_seconds)
            gaps = int((deltas > threshold).sum())
            if gaps:
                warnings.append(
                    f"{symbol}: {gaps} gap(s) larger than {threshold / 3600:.0f}h "
                    f"(possible missing history)"
                )

    span_days = (df.index[-1] - df.index[0]).total_seconds() / 86400
    if span_days < 1:
        warnings.append(f"{symbol}: only {span_days:.2f} days of history — too short to be meaningful")

    return warnings


class FrameFeed(DataFeed):
    """A feed backed by in-memory frames (used by tests and the CLI demo path)."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self._frames = frames

    def load(self) -> dict[str, pd.DataFrame]:
        return {sym: normalize_ohlc(df, sym) for sym, df in self._frames.items()}


def build_timeline(frames: Iterable[pd.DataFrame]) -> pd.DatetimeIndex:
    """Union of all timestamps across symbols, ascending."""
    parts = [df.index for df in frames]
    if not parts:
        return pd.DatetimeIndex([])
    union = parts[0]
    for part in parts[1:]:
        union = union.union(part)
    return union.sort_values()


def forward_fill_bars(df: pd.DataFrame, timeline: pd.DatetimeIndex) -> pd.DataFrame:
    """Reindex a symbol's bars onto a shared timeline.

    Missing bars are forward-filled for OHLC and zero-filled for volume, which
    is the standard way to handle a symbol that did not print during a session
    another symbol did trade. Bars that exist only in the future are dropped.
    """
    aligned = df.reindex(timeline)
    aligned[["open", "high", "low", "close"]] = aligned[["open", "high", "low", "close"]].ffill()
    aligned["volume"] = aligned["volume"].fillna(0.0)
    return aligned.dropna(subset=["close"])


def resample_bars(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate M1 bars up to a higher timeframe (left-labelled, closed bars)."""
    rule = {
        "M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
        "H1": "1h", "H4": "4h", "D1": "1D",
    }.get(timeframe.upper())
    if rule is None:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")
    agg = df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return agg.dropna(subset=["open", "high", "low", "close"])


def infer_pip_size(price: float) -> float:
    """Rough pip size guess for FX when the symbol spec is unknown."""
    if price > 1000:      # indices / metals quoted in hundreds
        return 0.1
    if price > 10:        # JPY crosses
        return 0.01
    return 0.0001


def synthetic_close_series(n: int, seed: int = 0, start_price: float = 1.1) -> pd.Series:
    """Convenience helper for tests: a simple random walk."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.0004, n)
    return pd.Series(start_price + np.cumsum(steps))
