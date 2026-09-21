"""Trading worker entrypoint (the persistent process on the worker host).

Runs the FastAPI service (dashboard API + WebSocket hub) with the trading
engine as its background loop. See docs/PHASE-1-RESEARCH-AND-ARCHITECTURE.md
for the single-process rationale and the process-split escape hatch.

Usage:
    python -m tradingbot.worker.main
or:
    uvicorn tradingbot.api.app:app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import uvicorn

from tradingbot.core.config import get_settings
from tradingbot.core.logging import configure_logging, get_logger

log = get_logger("tradingbot.worker")


def main() -> None:
    settings = get_settings()  # fails fast on unsafe config (live lock, etc.)
    configure_logging(settings.log_level)
    log.info(
        "trading worker starting",
        mode=settings.trading_mode.value,
        broker=settings.broker.value,
        instruments=settings.instruments,
    )
    uvicorn.run(
        "tradingbot.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
