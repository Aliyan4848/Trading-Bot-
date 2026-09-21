"""Synthetic FX bar generator.

Why not just use a random walk? Because a pure GBM has no trend regimes and no
session behaviour, so every momentum strategy looks either great or terrible by
accident. This generator adds:

  * regime switching (trending up / trending down / chopping) so momentum logic
    is actually exercised,
  * intraday volatility seasonality (London + NY sessions are more active),
  * weekend gaps and a flat Sunday open, like real FX,
  * plausible intrabar OHLC from an underlying minute path.

It is **not** a market simulator — it exists so the pipeline can be exercised
end-to-end without a broker connection. Never treat synthetic results as
evidence a strategy works.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

BASE_PRICES = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2650,
    "USDJPY": 149.50,
    "AUDUSD": 0.6580,
    "USDCAD": 1.3550,
    "USDCHF": 0.8850,
    "NZDUSD": 0.6050,
    "XAUUSD": 2050.0,
}


def _intraday_vol_profile(hours: np.ndarray) -> np.ndarray:
    """Relative activity by UTC hour: quiet Asia, busy London, busiest NY overlap."""
    profile = np.array(
        [
            0.45, 0.40, 0.38, 0.38, 0.42, 0.50,  # 00-05 Asia/off-hours
            0.65, 0.90, 1.20, 1.35, 1.30, 1.25,  # 06-11 London
            1.45, 1.60, 1.55, 1.40, 1.15, 0.95,  # 12-17 London/NY overlap
            0.80, 0.65, 0.55, 0.50, 0.48, 0.46,  # 18-23 NY wind-down
        ]
    )
    return profile[np.clip(hours, 0, 23)]


def _minute_timeline(start: str, bars: int) -> pd.DatetimeIndex:
    """Continuous M1 timeline including weekends (caller filters if desired)."""
    start_ts = pd.Timestamp(start, tz="UTC")
    return pd.date_range(start=start_ts, periods=bars, freq="1min", tz="UTC")


def generate_series(
    symbol: str = "EURUSD",
    bars: int = 40_000,
    start: str = "2024-01-01",
    seed: int = 7,
    annual_vol: float = 0.08,
    trend_strength: float = 0.5,
    regime_flip_bars: int = 3_000,
    weekend_gaps: bool = True,
    base_price: float | None = None,
) -> pd.DataFrame:
    """Generate an OHLC frame for one symbol.

    Returns a frame indexed by UTC timestamps with columns open/high/low/close/volume.
    """
    if bars < 10:
        raise ValueError("bars must be >= 10")

    rng = np.random.default_rng(seed)
    index = _minute_timeline(start, bars)

    start_price = base_price if base_price is not None else BASE_PRICES.get(symbol.upper(), 1.1000)

    # --- per-bar volatility --------------------------------------------------
    # 374400 M1 bars/year of FX trading time.
    bars_per_year = 374_400.0
    sigma_bar = annual_vol / np.sqrt(bars_per_year)
    profile = _intraday_vol_profile(index.hour.to_numpy())

    # --- regime drift --------------------------------------------------------
    n_regimes = max(1, int(np.ceil(bars / regime_flip_bars)))
    # +1 trend up, -1 trend down, 0 chop
    states = rng.choice([1, -1, 0, 0], size=n_regimes, p=[0.3, 0.3, 0.2, 0.2])
    regime = np.repeat(states, regime_flip_bars)[:bars]
    # `trend_strength` is a trending regime's annualised drift as a fraction of
    # annual volatility (0.5 -> a Sharpe-0.5 trend while the regime lasts), spread
    # evenly over the year's bars. Multiplying this per-bar drift by the regime
    # length instead compounds into a 25x price move over 138 days -- which is
    # how EURUSD ends up quoted at 27.
    drift = regime * trend_strength * annual_vol / bars_per_year

    # --- returns -------------------------------------------------------------
    shocks = rng.normal(0.0, 1.0, bars) * sigma_bar * profile
    # Mild volatility clustering: a fat-tailed ARCH-ish multiplier.
    vol_mult = 1.0 + 0.6 * np.abs(rng.normal(0.0, 1.0, bars)) ** 0.5
    log_ret = drift + shocks * np.clip(vol_mult, 0.5, 3.0)

    # A handful of news spikes so stop-loss logic gets tested properly.
    n_spikes = max(1, bars // 8_000)
    spike_idx = rng.choice(bars, size=n_spikes, replace=False)
    log_ret[spike_idx] += rng.choice([-1, 1], size=n_spikes) * sigma_bar * rng.uniform(8, 20, n_spikes)

    log_ret[0] = 0.0
    close_open = start_price * np.exp(np.cumsum(log_ret))

    # --- intrabar OHLC -------------------------------------------------------
    # Each bar's open is the previous close; the range is drawn around the body.
    open_ = np.empty(bars)
    open_[0] = start_price
    open_[1:] = close_open[:-1]

    body_high = np.maximum(open_, close_open)
    body_low = np.minimum(open_, close_open)
    wick_scale = sigma_bar * profile * np.abs(rng.normal(1.0, 0.35, bars)) * 1.8
    high = body_high + np.abs(rng.normal(0.0, 1.0, bars)) * wick_scale
    low = body_low - np.abs(rng.normal(0.0, 1.0, bars)) * wick_scale
    low = np.minimum(low, body_low)
    high = np.maximum(high, body_high)

    # Tick volume proxy: activity-scaled, with Poisson noise.
    volume = rng.poisson(np.clip(profile * 60.0, 5.0, None)).astype("float64")

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close_open, "volume": volume},
        index=index,
    )

    if weekend_gaps:
        df = _apply_market_calendar(df, rng)

    df.index.name = "time"
    df.attrs["symbol"] = symbol.upper()
    df.attrs["synthetic"] = True
    return df


def _apply_market_calendar(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Emulate FX hours: closed from Friday 21:00 UTC to Sunday 21:00 UTC.

    Closed periods are dropped, so the resulting series has the weekend gaps a
    real M1 export would have. The re-open bar gets a small gap jump.
    """
    idx = df.index
    weekday = idx.dayofweek  # 0=Mon ... 6=Sun
    hour = idx.hour

    closed = (
        ((weekday == 4) & (hour >= 21))          # Friday evening
        | (weekday == 5)                          # Saturday
        | ((weekday == 6) & (hour < 21))          # Sunday before the open
    )
    out = df.loc[~closed].copy()
    if out.empty:
        return df

    reopens = out.index.to_series().diff().dt.total_seconds().fillna(0.0) > 3600
    if reopens.any():
        jump = rng.normal(0.0, 0.0006, int(reopens.sum()))
        positions = np.flatnonzero(reopens.to_numpy())
        open_vals = out["open"].to_numpy(copy=True)
        close_vals = out["close"].to_numpy(copy=True)
        high_vals = out["high"].to_numpy(copy=True)
        low_vals = out["low"].to_numpy(copy=True)
        for pos, delta in zip(positions, jump, strict=True):
            open_vals[pos] = close_vals[pos - 1] * (1.0 + delta) if pos > 0 else open_vals[pos] * (1.0 + delta)
            high_vals[pos] = max(high_vals[pos], open_vals[pos])
            low_vals[pos] = min(low_vals[pos], open_vals[pos])
        out["open"], out["close"], out["high"], out["low"] = open_vals, close_vals, high_vals, low_vals
    return out


class SyntheticFeed:
    """DataFeed-compatible source returning synthetic bars for each symbol."""

    name = "synthetic"

    def __init__(
        self,
        symbols: list[str],
        bars: int = 40_000,
        start: str = "2024-01-01",
        seed: int = 7,
        annual_vol: float = 0.08,
        trend_strength: float = 0.5,
        regime_flip_bars: int = 3_000,
        spread_pips: float = 0.6,
    ) -> None:
        self.symbols = symbols
        self.bars = bars
        self.start = start
        self.seed = seed
        self.annual_vol = annual_vol
        self.trend_strength = trend_strength
        self.regime_flip_bars = regime_flip_bars
        self.spread_pips = spread_pips

    def load(self) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        for offset, symbol in enumerate(self.symbols):
            frames[symbol.upper()] = generate_series(
                symbol=symbol,
                bars=self.bars,
                start=self.start,
                # Different seed per symbol -> not perfectly correlated pairs.
                seed=self.seed + offset * 101,
                annual_vol=self.annual_vol,
                trend_strength=self.trend_strength,
                regime_flip_bars=self.regime_flip_bars,
            )
        return frames

    def describe(self) -> str:
        return f"synthetic({self.bars} M1 bars from {self.start}, seed={self.seed})"
