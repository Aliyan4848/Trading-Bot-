"""EMA crossover + RSI confirmation, ATR-sized stops.

The classic intraday scalping setup, tightened up:

  * **Direction** — fast EMA crosses slow EMA.
  * **Trend filter** — price must be on the correct side of a slow trend EMA,
    so we are not buying a 9/21 pop inside a downtrend.
  * **Momentum filter** — RSI must confirm (>= `rsi_long_min` for longs,
    <= `rsi_short_max` for shorts). Asymmetric thresholds stop the bot from
    taking longs into a falling knife.
  * **Volatility filter** — ATR must sit between `min_atr_pips` (dead market,
     the spread eats the edge) and `max_atr_pips` (news spike, slippage insane).

Stops are `atr_stop_mult * ATR`, targets are `reward_risk * stop`, so every
trade risks the same volatility-adjusted amount.
"""

from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from .base import PreparedSignals, Strategy, register


@register
class EmaRsiMomentum(Strategy):
    """Trend continuation: EMA stack + RSI pullback, ATR stop, fixed R target."""

    name = "ema_rsi_momentum"

    @classmethod
    def default_params(cls) -> dict:
        return {
            "fast_ema": 9,
            "slow_ema": 21,
            "trend_ema": 200,
            "rsi_period": 14,
            "rsi_long_min": 52.0,
            "rsi_short_max": 48.0,
            "atr_period": 14,
            "atr_stop_mult": 1.5,
            "reward_risk": 1.8,
            "min_atr_pips": 1.2,
            "max_atr_pips": 45.0,
            "one_signal_per_cross": True,
            "allow_shorts": True,
        }

    def validate_params(self) -> None:
        p = self.params
        if p["fast_ema"] >= p["slow_ema"]:
            raise ValueError("fast_ema must be smaller than slow_ema")
        if p["slow_ema"] >= p["trend_ema"]:
            raise ValueError("slow_ema must be smaller than trend_ema")
        if not 0 < p["rsi_period"]:
            raise ValueError("rsi_period must be > 0")
        if p["atr_stop_mult"] <= 0 or p["reward_risk"] <= 0:
            raise ValueError("atr_stop_mult and reward_risk must be > 0")
        if p["min_atr_pips"] >= p["max_atr_pips"]:
            raise ValueError("min_atr_pips must be below max_atr_pips")

    @property
    def min_bars(self) -> int:  # type: ignore[override]
        return int(max(self.params["trend_ema"], self.params["slow_ema"], self.params["rsi_period"]) + 50)

    def prepare(self, df: pd.DataFrame) -> PreparedSignals:
        if len(df) < self.min_bars:
            return self._empty(df)

        p = self.params
        pip = self.pip_size(df)

        fast = ind.ema(df["close"], int(p["fast_ema"]))
        slow = ind.ema(df["close"], int(p["slow_ema"]))
        trend = ind.ema(df["close"], int(p["trend_ema"]))
        rsi = ind.rsi(df["close"], int(p["rsi_period"]))
        atr = ind.atr(df["high"], df["low"], df["close"], int(p["atr_period"]))

        atr_pips = atr / pip
        up_cross = ind.crossed_above(fast, slow)
        down_cross = ind.crossed_below(fast, slow)

        if p["one_signal_per_cross"]:
            long_trigger = up_cross
            short_trigger = down_cross
        else:
            # Stay with the trend: signal whenever the fast EMA leads the slow one.
            long_trigger = fast > slow
            short_trigger = fast < slow

        trend_up = df["close"] > trend
        trend_down = df["close"] < trend
        vol_ok = atr_pips.between(float(p["min_atr_pips"]), float(p["max_atr_pips"]))

        long_ok = long_trigger & trend_up & (rsi >= float(p["rsi_long_min"])) & vol_ok
        short_ok = short_trigger & trend_down & (rsi <= float(p["rsi_short_max"])) & vol_ok
        if not p["allow_shorts"]:
            short_ok = pd.Series(False, index=df.index)

        signal = pd.Series(0, index=df.index, dtype="int8")
        signal[long_ok] = 1
        signal[short_ok] = -1

        stop_pips = atr_pips * float(p["atr_stop_mult"])
        tp_pips = stop_pips * float(p["reward_risk"])

        reason = pd.Series("", index=df.index, dtype=object)
        reason[long_ok] = "ema_up_cross+rsi_confirm"
        reason[short_ok] = "ema_down_cross+rsi_confirm"

        features = pd.DataFrame(
            {
                "fast_ema": fast,
                "slow_ema": slow,
                "trend_ema": trend,
                "rsi": rsi,
                "atr": atr,
                "atr_pips": atr_pips,
            },
            index=df.index,
        )
        return self._finalize(df, signal, stop_pips, tp_pips, reason, features)
