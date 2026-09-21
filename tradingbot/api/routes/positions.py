from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from tradingbot.api.auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/positions")
async def get_positions(request: Request) -> list[dict]:
    engine = request.app.state.engine
    positions = await engine.broker.positions()
    return [p.model_dump(mode="json") for p in positions]
