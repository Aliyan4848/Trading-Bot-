"""Broker interface.

Two implementations exist:

  * `PaperBroker`  — simulated fills against real (or synthetic) bars. Safe
    default, used by the backtester and by `python -m scalper paper`.
  * `MT5Broker`    — real order routing through a MetaTrader 5 terminal.
    Requires an explicit opt-in (see `config.is_live_allowed`).

The engine only ever talks to this interface, so a strategy cannot tell which
venue it is running on. That is what makes "backtest -> paper -> live" a config
change rather than a rewrite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import InstrumentSpec
from ..models import ExitReason, Fill, OrderRequest, Position, Trade


class BrokerError(RuntimeError):
    """Any broker-side failure (rejected order, no connection, bad symbol)."""


class Broker(ABC):
    """Everything the engine is allowed to ask of a venue."""

    #: True when orders reach a real account. The engine refuses to start live
    #: unless this is explicitly requested.
    is_live: bool = False

    #: True when this broker simulates stop/target hits itself (paper). A live
    #: venue holds SL/TP server-side instead, so the engine reconciles instead
    #: of simulating.
    simulates_exits: bool = True

    @abstractmethod
    def connect(self) -> None:
        """Establish the connection / validate credentials."""

    @abstractmethod
    def disconnect(self) -> None:
        """Clean up. Must be safe to call when not connected."""

    @abstractmethod
    def price(self, symbol: str) -> tuple[float, float]:
        """Current (bid, ask)."""

    @abstractmethod
    def open_position(self, request: OrderRequest, spec: InstrumentSpec) -> Fill:
        """Send an entry order. Raises `BrokerError` when rejected."""

    @abstractmethod
    def close_position(
        self, position: Position, price: float | None = None, reason: ExitReason = ExitReason.MANUAL
    ) -> Trade:
        """Close an open position and return the resulting `Trade`."""

    @abstractmethod
    def modify_position(
        self,
        position: Position,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> bool:
        """Change SL/TP. Returns True if the modification took effect."""

    @abstractmethod
    def positions(self) -> list[Position]:
        """Currently open positions."""

    @abstractmethod
    def balance(self) -> float:
        """Account balance (closed P&L only)."""

    @abstractmethod
    def equity(self) -> float:
        """Balance plus floating P&L."""

    def spread_pips(self, symbol: str, spec: InstrumentSpec) -> float:
        bid, ask = self.price(symbol)
        return (ask - bid) / spec.pip_size if spec.pip_size else 0.0

    def __enter__(self) -> Broker:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()
