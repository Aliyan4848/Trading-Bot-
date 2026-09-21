from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from tradingbot.api.auth import require_auth_ws

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Real-time event stream (ticks, bars, account, signals, risk events, heartbeat)."""
    if not await require_auth_ws(ws):
        return
    manager = ws.app.state.ws_manager
    await manager.connect(ws)
    try:
        while True:
            # inbound messages are not expected in v1; keep the socket alive
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception:
        await manager.disconnect(ws)
