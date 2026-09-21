"""Broker factory. Unknown broker kinds fail fast at startup."""

from __future__ import annotations

from tradingbot.broker.interfaces import BrokerClient, BrokerError
from tradingbot.broker.paper import PaperBroker
from tradingbot.core.config import BrokerKind, Settings


def create_broker(settings: Settings) -> BrokerClient:
    if settings.broker is BrokerKind.PAPER:
        return PaperBroker(settings)
    if settings.broker is BrokerKind.EXNESS:
        # Implemented in Phase 3 (official Exness Public Trader API client).
        raise BrokerError(
            "BROKER_NOT_IMPLEMENTED",
            "Exness API adapter lands in Phase 3. Until then use BROKER=paper, "
            "or configure Phase 10 after the adapter is complete.",
        )
    if settings.broker is BrokerKind.MT5:
        raise BrokerError(
            "BROKER_NOT_IMPLEMENTED",
            "MT5 adapter (Windows host) lands in Phase 10 fallback path. "
            "Until then use BROKER=paper.",
        )
    raise BrokerError("BROKER_UNKNOWN", f"unknown broker: {settings.broker}")
