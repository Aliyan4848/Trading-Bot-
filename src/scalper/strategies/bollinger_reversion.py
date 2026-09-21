"""Bollinger Band mean reversion with a regime filter.

Logic: in a *ranging* market, a close outside the band is noise that snaps back
to the mean. In a *trending* market the same close is the start of a leg and
reverting is how accounts die — so ADX is used to gate the strategy off when
the market is trending.

  * Entry: close pierces the lower band (long) or upper band (short).
  * Filter 1: ADX below `max_adx` — i.e. not in a strong trend.
  * Filter 2: band width above `min_bandwidth_pct` — a squeeze is neither
    range-bound nor tradeable, it is a coil waiting to break.
  * Target: the middle band (the mean). Stop: `atr_stop_mult * ATR` beyond the
    band, which is where the "it was not noise" thesis is invalidated.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from .base import PreparedSignals, Strategy, register


@register
class BollingerReversion(Strategy):
    """Band-pierce mean reversion, gated off when ADX says the market is trending."""

    name = "bollinger_reversion"

    @classmethod
    def default_params(cls) -> dict:
        return {
            "bb_period": 20,
            "bb_std": 2.0,
            "atr_period": 14,
            "atr_stop_mult": 1.2,
            "min_reward_risk": 0.8,
            "max_adx": 25.0,
            "adx_period": 14,
            "min_bandwidth_pct": 0.05,
            "max_bandwidth_pct": 2.5,
            "target": "middle",       # middle | opposite_band
            "allow_shorts": True,
            "require_reversal_candle": True,
        }

    def validate_params(self) -> None:
        p = self.params
        if p["bb_period"] < 3:
            raise ValueError("bb_period must be >= 3")
        if p["bb_std"] <= 0:
            raise ValueError("bb_std must be > 0")
        if p["target"] not in ("middle", "opposite_band"):
            raise ValueError("target must be 'middle' or 'opposite_band'")
        if p["min_bandwidth_pct"] >= p["max_bandwidth_pct"]:
            raise ValueError("min_bandwidth_pct must be below max_bandwidth_pct")

    @property
    def min_bars(self) -> int:  # type: ignore[override]
        return int(max(self.params["bb_period"], self.params["adx_period"] * 2) + 50)

    def prepare(self, df: pd.DataFrame) -> PreparedSignals:
        if len(df) < self.min_bars:
            return self._empty(df)

        p = self.params
        pip = self.pip_size(df)

        middle, upper, lower, bandwidth = ind.bollinger_bands(
            df["close"], int(p["bb_period"]), float(p["bb_std"])
        )
        atr = ind.atr(df["high"], df["low"], df["close"], int(p["atr_period"]))
        adx, _, _ = ind.adx(df["high"], df["low"], df["close"], int(p["adx_period"]))

        ranging = adx < float(p["max_adx"])
        width_ok = bandwidth.between(float(p["min_bandwidth_pct"]), float(p["max_bandwidth_pct"]))

        below = df["close"] < lower
        above = df["close"] > upper

        if p["require_reversal_candle"]:
            # Wait for the first candle that stops making new lows/highs, which
            # avoids catching a knife still in free fall.
            below = below & (df["close"] > df["open"]) & (df["low"] >= df["low"].shift(1))
            above = above & (df["close"] < df["open"]) & (df["high"] <= df["high"].shift(1))

        long_ok = below & ranging & width_ok
        short_ok = above & ranging & width_ok
        if not p["allow_shorts"]:
            short_ok = pd.Series(False, index=df.index)

        signal = pd.Series(0, index=df.index, dtype="int8")
        signal[long_ok] = 1
        signal[short_ok] = -1

        # --- stops and targets ------------------------------------------------
        stop_price_long = df["low"] - atr * float(p["atr_stop_mult"])
        stop_price_short = df["high"] + atr * float(p["atr_stop_mult"])
        stop_pips_long = (df["close"] - stop_price_long) / pip
        stop_pips_short = (stop_price_short - df["close"]) / pip

        if p["target"] == "middle":
            target_long = middle
            target_short = middle
        else:
            target_long = upper
            target_short = lower
        tp_pips_long = (target_long - df["close"]) / pip
        tp_pips_short = (df["close"] - target_short) / pip

        # Both branches are published on every bar (not just signal bars) so the
        # values are always inspectable and a signal can never be dropped for a
        # missing stop. `signal` alone decides what trades.
        stop_series = pd.Series(
            np.where(signal < 0, stop_pips_short, stop_pips_long), index=df.index, dtype="float64"
        )
        tp_series = pd.Series(
            np.where(signal < 0, tp_pips_short, tp_pips_long), index=df.index, dtype="float64"
        )

        # A mean-reversion trade whose target is closer than the stop rarely pays
        # for the spread; drop those.
        rr = tp_series / stop_series.replace(0.0, np.nan)
        signal = signal.where(rr >= float(p["min_reward_risk"]), 0)

        reason = pd.Series("", index=df.index, dtype=object)
        reason[(signal > 0)] = "bb_lower_pierce+range"
        reason[(signal < 0)] = "bb_upper_pierce+range"

        features = pd.DataFrame(
            {
                "bb_middle": middle,
                "bb_upper": upper,
                "bb_lower": lower,
                "bandwidth": bandwidth,
                "atr": atr,
                "adx": adx,
            },
            index=df.index,
        )
        return self._finalize(df, signal.astype("int8"), stop_series, tp_series, reason, features)
