"""Broker factory. Unknown broker kinds fail fast at startup."""

from __future__ import annotations

from tradingbot.broker.exness.broker import ExnessBroker
from tradingbot.broker.interfaces import BrokerClient, BrokerError
from tradingbot.broker.paper import PaperBroker
from tradingbot.core.config import BrokerKind, Settings


def create_broker(settings: Settings) -> BrokerClient:
    if settings.broker is BrokerKind.PAPER:
        return PaperBroker(settings)
    if settings.broker is BrokerKind.EXNESS:
        # Official Exness Public Trader API adapter (Phase 3). Demo account is
        # enforced at the settings level (EXN_ACCOUNT_IS_DEMO=true required).
        return ExnessBroker(settings)
    if settings.broker is BrokerKind.MT5:
        raise BrokerError(
            "BROKER_NOT_IMPLEMENTED",
            "MT5 adapter (Windows host) lands in Phase 10 fallback path. "
            "Until then use BROKER=paper.",
        )
    raise BrokerError("BROKER_UNKNOWN", f"unknown broker: {settings.broker}")
