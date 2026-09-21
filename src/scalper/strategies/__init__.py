"""Built-in strategies and the registry used to look them up by name."""

from __future__ import annotations

from .base import (
    SIGNAL_FLAT,
    SIGNAL_LONG,
    SIGNAL_SHORT,
    CompositeStrategy,
    PreparedSignals,
    Strategy,
    available_strategies,
    get_strategy,
    register,
)

__all__ = [
    "SIGNAL_FLAT",
    "SIGNAL_LONG",
    "SIGNAL_SHORT",
    "CompositeStrategy",
    "PreparedSignals",
    "Strategy",
    "available_strategies",
    "get_strategy",
    "register",
    "EmaRsiMomentum",
    "BollingerReversion",
    "VwapPullback",
]

# Importing the concrete strategies registers them.
from .bollinger_reversion import BollingerReversion  # noqa: E402
from .ema_rsi_momentum import EmaRsiMomentum  # noqa: E402
from .vwap_pullback import VwapPullback  # noqa: E402
