"""Dashboard API authentication.

v1: single shared bearer token (``API_TOKEN``). When unset (local dev)
authentication is disabled and a warning is logged. Phase 7 adds per-user
accounts on top of this dependency — routes stay unchanged.

Settings are read from ``app.state`` (never the global ``get_settings``
cache) so tests and multi-instance deployments behave correctly.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket

from tradingbot.core.config import Settings
from tradingbot.core.logging import get_logger

log = get_logger("tradingbot.api.auth")


def _check(token: str | None, settings: Settings) -> None:
    if not settings.api_token:
        return  # dev mode, auth disabled
    if not token or token != settings.api_token:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def require_auth(request: Request) -> None:
    settings = request.app.state.settings
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.lower().startswith("bearer ") else None
    _check(token, settings)


async def require_auth_ws(ws: WebSocket) -> bool:
    """Returns True when authorized. On failure the socket is closed with 4401."""
    settings = ws.app.state.settings
    token = ws.query_params.get("token")
    if not settings.api_token:
        return True
    if token != settings.api_token:
        await ws.accept()
        await ws.close(code=4401)
        return False
    return True
