"""Strategy interface + a few shared helpers.

Contract for every strategy:

1. ``prepare(df)`` returns a DataFrame indexed exactly like ``df`` containing at
   minimum a ``signal`` column, where
     `` 1``  = go long,
     ``-1`` = go short,
     `` 0``  = do nothing,
   plus optional ``stop_pips``, ``tp_pips`` and ``reason`` columns.
2. A signal on row ``t`` means: "based on everything known when bar ``t``
   closed, enter". The engine decides *when* to fill (next bar's open by
   default), which is what keeps the backtest honest — indicators must never
   read rows beyond ``t``.

Because ``prepare`` is vectorised, a 150k-bar backtest spends its time in the
engine loop, not in indicator maths.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..models import Direction

SIGNAL_LONG = 1
SIGNAL_SHORT = -1
SIGNAL_FLAT = 0


@dataclass(slots=True)
class PreparedSignals:
    """Signals plus the feature frame they came from (for dashboards/debugging)."""

    signals: pd.DataFrame
    features: pd.DataFrame

    def __len__(self) -> int:
        return len(self.signals)


class Strategy(ABC):
    """Base class for all strategies."""

    name: str = "base"
    #: Extra bars of history the indicators need beyond the signal row.
    min_bars: int = 100

    def __init__(self, symbol: str = "", **params: Any) -> None:
        self.symbol = symbol.upper()
        self.params: dict[str, Any] = dict(self.default_params())
        unknown = set(params) - set(self.params)
        if unknown:
            raise ValueError(
                f"{self.name}: unknown parameter(s) {sorted(unknown)}. "
                f"Valid: {sorted(self.params)}"
            )
        self.params.update(params)
        self.validate_params()

    # -- overridables ---------------------------------------------------------
    @classmethod
    def default_params(cls) -> dict[str, Any]:
        """Return the parameter schema (name -> default)."""
        return {}

    def validate_params(self) -> None:
        """Raise ValueError on nonsense parameters. Override as needed."""
        for key, value in self.params.items():
            if value is None:
                raise ValueError(f"{self.name}: parameter {key!r} must not be None")

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> PreparedSignals:
        """Compute indicators and signals for the whole frame."""

    # -- shared helpers -------------------------------------------------------
    def _empty(self, df: pd.DataFrame) -> PreparedSignals:
        signals = pd.DataFrame(
            {
                "signal": np.zeros(len(df), dtype="int8"),
                "stop_pips": np.full(len(df), np.nan),
                "tp_pips": np.full(len(df), np.nan),
                "reason": np.array([""] * len(df), dtype=object),
            },
            index=df.index,
        )
        return PreparedSignals(signals=signals, features=pd.DataFrame(index=df.index))

    def _finalize(
        self,
        df: pd.DataFrame,
        signal: pd.Series,
        stop_pips: pd.Series,
        tp_pips: pd.Series,
        reason: pd.Series,
        features: pd.DataFrame | None = None,
    ) -> PreparedSignals:
        signals = pd.DataFrame(
            {
                "signal": signal.fillna(0).astype("int8"),
                "stop_pips": stop_pips.astype("float64"),
                "tp_pips": tp_pips.astype("float64"),
                "reason": reason.fillna("").astype(object),
            },
            index=df.index,
        )
        # NaN indicators must not create phantom signals.
        valid = signals["stop_pips"].notna() & (signals["stop_pips"] > 0)
        signals.loc[~valid, "signal"] = 0
        return PreparedSignals(
            signals=signals,
            features=features if features is not None else pd.DataFrame(index=df.index),
        )

    @staticmethod
    def direction_of(signal: int) -> Direction | None:
        if signal > 0:
            return Direction.LONG
        if signal < 0:
            return Direction.SHORT
        return None

    @staticmethod
    def pip_size(df: pd.DataFrame) -> float:
        return float(df.attrs.get("pip_size", 0.0001))

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        shown = ", ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.__class__.__name__}({shown})"


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------
_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator that adds a strategy to the global registry."""
    key = cls.name.lower()
    if key in _REGISTRY and _REGISTRY[key] is not cls:
        raise ValueError(f"Strategy name {key!r} is already registered")
    _REGISTRY[key] = cls
    return cls


def available_strategies() -> dict[str, type[Strategy]]:
    _load_builtins()
    return dict(_REGISTRY)


def get_strategy(name: str, symbol: str = "", **params: Any) -> Strategy:
    """Instantiate a registered strategy by name."""
    _load_builtins()
    key = str(name).strip().lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown strategy {name!r}. Available: {sorted(_REGISTRY)}\n"
            f"Set `strategy.name` in your config to one of those."
        )
    return _REGISTRY[key](symbol=symbol, **params)


_BUILTINS_LOADED = False


def _load_builtins() -> None:
    """Import the built-in strategies so their decorators run."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    from . import bollinger_reversion, ema_rsi_momentum, vwap_pullback  # noqa: F401

    _BUILTINS_LOADED = True


class CompositeStrategy(Strategy):
    """Combines several strategies by majority vote.

    Useful when you want "EMA momentum *and* VWAP pullback agree" without
    writing another strategy. Ties (1 vs 1) produce no signal; the tightest
    stop among the agreeing legs is used, which keeps risk per trade constant.
    """

    name = "composite"

    def __init__(self, symbol: str = "", strategies: list[Strategy] | None = None, **params: Any) -> None:
        self.legs: list[Strategy] = strategies or []
        super().__init__(symbol=symbol, **params)
        if not self.legs:
            raise ValueError("CompositeStrategy needs at least one leg")
        # Stop distances must not change when the legs are swapped in order.
        self.legs.sort(key=lambda s: s.name)

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"min_votes": 2}

    @property
    def min_bars(self) -> int:  # type: ignore[override]
        return max((leg.min_bars for leg in self.legs), default=100)

    def prepare(self, df: pd.DataFrame) -> PreparedSignals:
        votes = np.zeros(len(df), dtype="int32")
        stops = np.full(len(df), np.inf)
        tps = np.full(len(df), np.nan)
        reasons = np.array([""] * len(df), dtype=object)

        for leg in self.legs:
            prepared = leg.prepare(df)
            sig = prepared.signals["signal"].to_numpy(dtype="int32")
            votes += sig
            stop = prepared.signals["stop_pips"].to_numpy(dtype="float64")
            tp = prepared.signals["tp_pips"].to_numpy(dtype="float64")
            taking = (sig != 0) & np.isfinite(stop)
            stops = np.where(taking & (stop < stops), stop, stops)
            tps = np.where(taking & np.isnan(tps), tp, tps)
            for i in np.flatnonzero(taking):
                reasons[i] = f"{reasons[i]}+{leg.name}" if reasons[i] else leg.name

        needed = int(self.params["min_votes"])
        signal = np.where(np.abs(votes) >= max(needed, 1), np.sign(votes), 0)
        # A tie (equal long and short votes) must not trade.
        signal = np.where(np.abs(votes) == 0, 0, signal)
        stops = np.where(signal != 0, stops, np.nan)
        return self._finalize(
            df,
            pd.Series(signal, index=df.index),
            pd.Series(stops, index=df.index),
            pd.Series(tps, index=df.index),
            pd.Series(reasons, index=df.index),
        )
