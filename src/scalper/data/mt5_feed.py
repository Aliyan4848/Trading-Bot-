"""MetaTrader 5 history feed.

Fetches bars straight from a running MT5 terminal via the official
``MetaTrader5`` Python package. That package is **Windows-only** and requires a
logged-in terminal on the same machine, so this module imports it lazily and
fails with an actionable message instead of a bare ImportError.

The returned frames look exactly like the other feeds (UTC tz-aware), which
means anything you backtest on MT5 history can be paper traded and live traded
on the same bars without a translation layer.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pandas as pd

from .base import DataError, DataFeed, validate_dataframe

if TYPE_CHECKING:  # pragma: no cover
    pass

# MT5 timeframe constants are resolved at runtime (they only exist on Windows).
TIMEFRAME_NAMES = ("M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30", "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1")

_COLUMNS = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")


def import_mt5() -> Any:
    """Import the MetaTrader5 package or raise a helpful error."""
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the host OS
        raise DataError(
            "The MetaTrader5 package is not installed or not available on this platform.\n"
            "It is WINDOWS ONLY and needs the MT5 terminal installed and running.\n"
            "  Windows:  pip install -r requirements-mt5.txt\n"
            "  On Linux/macOS: use `data.source: csv` (export history from MT5) or run\n"
            "  the bot on a Windows VPS / Wine.\n"
            f"Original error: {exc}"
        ) from exc
    return mt5


def timeframe_constant(mt5: Any, timeframe: str) -> Any:
    """Map 'M15' -> mt5.TIMEFRAME_M15."""
    key = str(timeframe).strip().upper()
    if not hasattr(mt5, f"TIMEFRAME_{key}"):
        raise DataError(f"MT5 has no timeframe {timeframe!r}. Known: {TIMEFRAME_NAMES}")
    return getattr(mt5, f"TIMEFRAME_{key}")


def rates_to_frame(rates: Any, symbol: str = "") -> pd.DataFrame:
    """Convert the numpy structured array from MT5 into canonical OHLC."""
    if rates is None or len(rates) == 0:
        raise DataError(f"MT5 returned no bars for {symbol or 'requested symbol'}")
    df = pd.DataFrame(rates)
    names = list(df.columns)
    if "tick_volume" not in names and "real_volume" in names:
        df["tick_volume"] = df["real_volume"]
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    keep = [c for c in ("open", "high", "low", "close", "tick_volume") if c in df.columns]
    df = df[keep].rename(columns={"tick_volume": "volume"})
    return df


class Mt5Feed(DataFeed):
    """Pulls `bars` most recent bars per symbol from the terminal."""

    name = "mt5"

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "M1",
        bars: int = 50_000,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        path: str | None = None,
        portable: bool = False,
        retries: int = 3,
        retry_sleep_sec: float = 1.0,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.timeframe = timeframe.upper()
        self.bars = int(bars)
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self.portable = portable
        self.retries = retries
        self.retry_sleep_sec = retry_sleep_sec
        self._mt5: Any = None

    # -- connection -----------------------------------------------------------
    def connect(self) -> Any:
        """Initialise the terminal connection (idempotent)."""
        if self._mt5 is not None:
            return self._mt5
        mt5 = import_mt5()

        kwargs: dict[str, Any] = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login:
            kwargs["login"] = int(self.login)
            kwargs["password"] = self.password
            kwargs["server"] = self.server
        kwargs["portable"] = self.portable

        last_error = ""
        for attempt in range(1, max(1, self.retries) + 1):
            ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
            if ok:
                info = mt5.terminal_info()
                if info is None:
                    last_error = f"terminal_info() returned None: {mt5.last_error()}"
                else:
                    self._mt5 = mt5
                    return mt5
            else:
                last_error = f"{mt5.last_error()}"
            if attempt < self.retries:
                _time.sleep(self.retry_sleep_sec)

        raise DataError(
            "Could not connect to the MetaTrader 5 terminal "
            f"after {self.retries} attempt(s).\n"
            "Checklist:\n"
            "  1. Is the MT5 terminal installed, running, and logged in?\n"
            "  2. Algorithms trading enabled (Tools > Options > Expert Advisors)?\n"
            "  3. Are MT5_LOGIN / MT5_PASSWORD / MT5_SERVER correct in your .env?\n"
            "  4. If MT5 runs outside the default folder, set MT5_PATH to terminal64.exe.\n"
            f"Last terminal error: {last_error}"
        )

    def shutdown(self) -> None:
        if self._mt5 is not None:
            try:
                self._mt5.shutdown()
            finally:
                self._mt5 = None

    def __enter__(self) -> Mt5Feed:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # -- data -----------------------------------------------------------------
    def load(self) -> dict[str, pd.DataFrame]:
        mt5 = self.connect()
        tf = timeframe_constant(mt5, self.timeframe)
        frames: dict[str, pd.DataFrame] = {}

        for symbol in self.symbols:
            if not mt5.symbol_select(symbol, True):
                raise DataError(
                    f"MT5 does not offer symbol {symbol!r} (symbol_select failed). "
                    f"Check the exact name in Market Watch — brokers add suffixes "
                    f"like EURUSD.a or EURUSDm."
                )
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, self.bars)
            frame = rates_to_frame(rates, symbol)
            for warning in validate_dataframe(frame, symbol):
                frame.attrs.setdefault("warnings", []).append(warning)
            frames[symbol] = frame
        return frames

    def load_range(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Fetch an explicit window — useful for backfilling history."""
        mt5 = self.connect()
        tf = timeframe_constant(mt5, self.timeframe)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        mt5.symbol_select(symbol, True)
        rates = mt5.copy_rates_range(symbol, tf, start, end)
        return rates_to_frame(rates, symbol)

    def latest_bar_time(self, symbol: str) -> datetime | None:
        mt5 = self.connect()
        tf = timeframe_constant(mt5, self.timeframe)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, 1)
        if rates is None or len(rates) == 0:
            return None
        return pd.to_datetime(rates[0]["time"], unit="s", utc=True).to_pydatetime()

    def account_balance(self) -> float:
        mt5 = self.connect()
        info = mt5.account_info()
        if info is None:
            raise DataError(f"MT5 account_info() failed: {mt5.last_error()}")
        return float(info.balance)

    def symbol_spread_pips(self, symbol: str, pip_size: float) -> float | None:
        mt5 = self.connect()
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None or pip_size <= 0:
            return None
        return float(tick.ask - tick.bid) / pip_size

    def describe(self) -> str:
        return f"mt5({self.timeframe}, {self.bars} bars, {len(self.symbols)} symbols)"


def recent_window(hours: int = 24) -> tuple[datetime, datetime]:
    """Handy default range for ad-hoc history pulls."""
    end = datetime.now(timezone.utc)
    return end - timedelta(hours=hours), end
