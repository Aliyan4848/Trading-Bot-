"""Liveness probe — intentionally unauthenticated (used by the host/uptime checks)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from tradingbot import __version__
from tradingbot.core.timeutils import utcnow

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    engine = request.app.state.engine
    account = engine.last_account
    return {
        "status": "ok",
        "version": __version__,
        "mode": request.app.state.settings.trading_mode.value,
        "broker": engine.broker.name,
        "engine_running": engine.running,
        "instruments": request.app.state.settings.instruments,
        "account_connected": account is not None,
        "server_time": utcnow().isoformat(timespec="milliseconds"),
    }
