from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from tradingbot.api.auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/account")
async def get_account(request: Request) -> dict:
    engine = request.app.state.engine
    if engine.last_account is None:
        raise HTTPException(status_code=503, detail="account info not available yet")
    return engine.last_account.model_dump(mode="json")
