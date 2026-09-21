"""Broker abstraction.

Every broker adapter (paper, Exness API, MT5) implements ``BrokerClient``.
The engine, strategy, risk and execution layers only ever talk to this
interface — swapping brokers is a configuration decision.

Contract notes (mirrors the official Exness API semantics so adapters are
interchangeable):
  * mutating calls are **async**: they return an ACK-like
    :class:`OperationStatus` (state=``accepted``) and the final state arrives
    via ``operation_status`` polling — the engine always verifies the final
    state before recording success.
  * mutating calls are **idempotent** on ``(client_request_id, payload)``:
    a duplicate request returns the original operation result and is never
    re-executed.
  * ``subscribe_ticks`` is a push stream (WS for Exness, polling loop for
    MT5, synthetic generator for paper).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from tradingbot.broker.models import (
    AccountInfo,
    Deal,
    InstrumentConditions,
    OperationStatus,
    OrderRequest,
    Position,
    Tick,
)


class BrokerError(RuntimeError):
    """Broker-level failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class BrokerClient(ABC):
    name: str = "abstract"

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection. Must be idempotent-ish (one call at start)."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources; stop all tasks."""

    @abstractmethod
    async def account_info(self) -> AccountInfo: ...

    @abstractmethod
    async def instrument_conditions(self, instrument: str) -> InstrumentConditions: ...

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    def subscribe_ticks(self, instruments: list[str]) -> AsyncIterator[Tick]: ...

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OperationStatus:
        """Submit an order. Returns ACK (accepted) or a synchronous rejection."""

    @abstractmethod
    async def close_position(self, position_id: str, client_request_id: str,
                             reason: str = "MANUAL") -> OperationStatus: ...

    @abstractmethod
    async def operation_status(self, operation_id: str) -> OperationStatus: ...

    @abstractmethod
    async def history_deals(self, since: datetime | None = None) -> list[Deal]: ...
