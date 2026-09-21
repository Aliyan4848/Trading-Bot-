"""Session-VWAP pullback continuation.

VWAP is the intraday reference price: institutions benchmark fills to it, so it
acts as a magnet and then as support/resistance depending on the trend.

  * Trend context: price above the daily VWAP and EMA stack pointing up -> only
    take longs. Below -> only take shorts. No counter-trend version of this.
  * Trigger: price pulls back to within `touch_tolerance_pips` of VWAP and then
    closes back in the trend direction (a rejection wick).
  * Stop: `atr_stop_mult * ATR` from entry, placed past the VWAP.
  * Target: `reward_risk * stop`, with an optional VWAP-deviation cap so we do
    not aim beyond the statistically probable daily range.

Uses `rolling_vwap` which resets at each session open — that is the version that
matters intraday. NOTE: FX spot has no true volume, so the fallback in
`indicators.vwap` equal-weights bars; on futures/CFD feeds with real volume this
strategy behaves notably better.
"""

from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from .base import PreparedSignals, Strategy, register


@register
class VwapPullback(Strategy):
    """Session-VWAP pullback continuation, in the direction of the EMA stack only."""

    name = "vwap_pullback"

    @classmethod
    def default_params(cls) -> dict:
        return {
            "vwap_reset": "daily",       # daily | rolling
            "vwap_period": 60,           # used when vwap_reset == "rolling"
            "atr_period": 14,
            "atr_stop_mult": 1.3,
            "reward_risk": 1.6,
            "touch_tolerance_pips": 2.0,
            "ema_period": 50,
            "require_stack": True,
            "max_distance_pips": 25.0,   # do not chase price far from VWAP
            "allow_shorts": True,
        }

    def validate_params(self) -> None:
        p = self.params
        if p["vwap_reset"] not in ("daily", "rolling"):
            raise ValueError("vwap_reset must be 'daily' or 'rolling'")
        if p["atr_stop_mult"] <= 0 or p["reward_risk"] <= 0:
            raise ValueError("atr_stop_mult and reward_risk must be > 0")
        if p["touch_tolerance_pips"] < 0:
            raise ValueError("touch_tolerance_pips must be >= 0")

    @property
    def min_bars(self) -> int:  # type: ignore[override]
        return int(max(self.params["ema_period"], self.params["atr_period"] * 2, 60) + 50)

    def prepare(self, df: pd.DataFrame) -> PreparedSignals:
        if len(df) < self.min_bars:
            return self._empty(df)

        p = self.params
        pip = self.pip_size(df)

        if p["vwap_reset"] == "daily":
            vwap = ind.rolling_vwap(df["high"], df["low"], df["close"], df["volume"], 1, reset_daily=True)
        else:
            vwap = ind.rolling_vwap(
                df["high"], df["low"], df["close"], df["volume"], int(p["vwap_period"]), reset_daily=False
            )

        ema_fast = ind.ema(df["close"], 9)
        ema_slow = ind.ema(df["close"], int(p["ema_period"]))
        atr = ind.atr(df["high"], df["low"], df["close"], int(p["atr_period"]))

        # --- trend context ---------------------------------------------------
        stack_up = (df["close"] > vwap) & (ema_fast > ema_slow)
        stack_down = (df["close"] < vwap) & (ema_fast < ema_slow)
        if not p["require_stack"]:
            stack_up = df["close"] > vwap
            stack_down = df["close"] < vwap
        if not p["allow_shorts"]:
            stack_down = pd.Series(False, index=df.index)

        distance_pips = (df["close"] - vwap).abs() / pip
        near_vwap = distance_pips <= float(p["max_distance_pips"])

        # --- rejection trigger ----------------------------------------------
        tolerance = float(p["touch_tolerance_pips"]) * pip
        touched_from_above = df["low"] <= (vwap + tolerance)
        touched_from_below = df["high"] >= (vwap - tolerance)

        # Bullish rejection: dips to VWAP, closes above it, closes green.
        long_trigger = (
            touched_from_above
            & (df["close"] > vwap)
            & (df["close"] > df["open"])
        )
        short_trigger = (
            touched_from_below
            & (df["close"] < vwap)
            & (df["close"] < df["open"])
        )

        long_ok = stack_up & near_vwap & long_trigger
        short_ok = stack_down & near_vwap & short_trigger

        signal = pd.Series(0, index=df.index, dtype="int8")
        signal[long_ok] = 1
        signal[short_ok] = -1

        stop_pips = atr / pip * float(p["atr_stop_mult"])
        tp_pips = stop_pips * float(p["reward_risk"])

        reason = pd.Series("", index=df.index, dtype=object)
        reason[long_ok] = "vwap_reclaim+pullback"
        reason[short_ok] = "vwap_reject+pullback"

        features = pd.DataFrame(
            {
                "vwap": vwap,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "atr": atr,
                "distance_pips": distance_pips,
            },
            index=df.index,
        )
        return self._finalize(df, signal, stop_pips, tp_pips, reason, features)
