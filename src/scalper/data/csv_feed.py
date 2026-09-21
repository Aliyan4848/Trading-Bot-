"""CSV data feed.

Understands the formats you actually get handed:

  * MT5 "Export to CSV" history (tab or comma separated, ``<DATE>``/``<TIME>``)
  * Dukascopy / HistData style ``Date,Time,Open,High,Low,Close,Volume``
  * Yahoo-style ``Datetime,Open,High,Low,Close,Volume``
  * Anything with recognisable column aliases (see ``normalize_ohlc``)

Timestamp handling: naive stamps are localised to the configured timezone and
then converted to UTC, so a ``tz: "America/New_York"`` export lands on the right
session windows.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import DataError, DataFeed, normalize_ohlc, validate_dataframe

# Separators to try, best guess first.
_SEPARATORS = (";", "\t", ",", "\\s+")


def _read_any_separator(path: Path) -> pd.DataFrame:
    """Read a CSV/TSV whose delimiter we do not know up front."""
    last_error: Exception | None = None
    for sep in _SEPARATORS:
        try:
            df = pd.read_csv(path, sep=sep, engine="python", skipinitialspace=True)
        except Exception as exc:  # noqa: BLE001 - try the next separator
            last_error = exc
            continue
        if df.shape[1] > 1:
            return df
    # Single column: fall back to a plain read so the error message is useful.
    try:
        return pd.read_csv(path)
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"Could not parse {path} with any known separator: {exc}") from last_error or exc


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if p.exists():
        return p
    # Allow `{symbol}` patterns (e.g. data/{symbol}_M1.csv) to expand per symbol.
    raise DataError(
        f"CSV data file not found: {p}. Download M1 history (MT5: Tools > History Center "
        f"> Export, or Dukascopy) and point `data.csv.path` at it."
    )


class CsvFeed(DataFeed):
    """Loads one CSV per symbol, or a single file for a single symbol."""

    name = "csv"

    def __init__(
        self,
        path: str,
        symbols: list[str],
        timestamp_column: str | None = None,
        tz: str = "UTC",
    ) -> None:
        self.path = str(path)
        self.symbols = [s.upper() for s in symbols]
        self.timestamp_column = timestamp_column
        self.tz = tz

    def _path_for(self, symbol: str) -> Path:
        """Expand a `{symbol}` placeholder, else use the static path.

        Both cases are tried (EURUSD_M1.csv and eurusd_M1.csv) because file
        naming varies by export tool and Linux filesystems are case-sensitive.
        """
        if "{symbol}" not in self.path and "{SYMBOL}" not in self.path:
            return _resolve_path(self.path)

        candidates = [
            self.path.replace("{symbol}", case).replace("{SYMBOL}", case)
            for case in (symbol, symbol.lower(), symbol.upper())
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                return Path(candidate)
        # Nothing matched: report the default spelling so the error is readable.
        return Path(candidates[0])

    def load(self) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        for symbol in self.symbols:
            frame = self._load_one(symbol)
            for warning in validate_dataframe(frame, symbol):
                # Surfaced by the caller's logger (kept as attrs so the data layer
                # stays free of logging config).
                frame.attrs.setdefault("warnings", []).append(warning)
            frames[symbol] = frame
        return frames

    def _load_one(self, symbol: str) -> pd.DataFrame:
        path = self._path_for(symbol)
        if not path.exists():
            raise DataError(
                f"CSV data file not found for {symbol}: {path}\n"
                f"Set `data.csv.path` to your history file, or use `{{symbol}}` in the "
                f"path to load one file per instrument."
            )
        raw = _read_any_separator(path)
        return normalize_ohlc(
            raw,
            symbol=symbol,
            timestamp_column=self.timestamp_column,
            tz=self.tz,
        )

    def describe(self) -> str:
        return f"csv({self.path}, tz={self.tz})"


def write_csv(df: pd.DataFrame, path: str | Path) -> Path:
    """Write bars in the canonical format (round-trip safe with ``CsvFeed``)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame = df.copy()
    frame.index.name = "time"
    frame.to_csv(out)
    return out


class MultiFileCsvFeed(CsvFeed):
    """Explicit per-symbol file mapping, e.g. ``{"EURUSD": "data/eu.csv"}``."""

    def __init__(self, paths: dict[str, str], timestamp_column: str | None = None, tz: str = "UTC") -> None:
        super().__init__(path="", symbols=list(paths), timestamp_column=timestamp_column, tz=tz)
        self.paths = {k.upper(): v for k, v in paths.items()}

    def _path_for(self, symbol: str) -> Path:
        return _resolve_path(self.paths[symbol])

    def load(self) -> dict[str, pd.DataFrame]:
        frames = super().load()
        for symbol, frame in frames.items():
            frame.attrs["path"] = self.paths[symbol]
        return frames

    def describe(self) -> str:
        return f"csv(multi-file, {len(self.paths)} symbols)"
