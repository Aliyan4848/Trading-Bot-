from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from tradingbot.api.auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/market/{instrument}")
async def get_market(
    request: Request,
    instrument: str,
    timeframe: int = Query(default=5, ge=1),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Latest tick + recent aggregated bars for the instrument.

    ``timeframe`` is in seconds (5 / 10 / 60 are produced by the engine).
    """
    engine = request.app.state.engine
    market = engine.market.markets.get(instrument.upper())
    if market is None:
        raise HTTPException(status_code=404, detail=f"instrument {instrument!r} not tracked")
    bars = market.recent_bars(timeframe, limit) if timeframe in market.bars else []
    tick = market.last_tick
    return {
        "instrument": instrument.upper(),
        "last_tick": tick.model_dump(mode="json") if tick else None,
        "timeframe_s": timeframe,
        "bars": [b.model_dump(mode="json") for b in bars],
    }
