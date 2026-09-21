from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from tradingbot.api.auth import require_auth
from tradingbot.db.base import session_scope
from tradingbot.db.models import Signal

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/signals")
async def get_signals(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    instrument: str | None = None,
) -> list[dict]:
    """Recent strategy signals (accepted AND rejected — auditability)."""
    async with session_scope(request.app.state.sessions) as session:
        stmt = select(Signal).order_by(Signal.ts.desc()).limit(limit)
        if instrument:
            stmt = stmt.where(Signal.instrument == instrument.upper())
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": s.id,
                "ts": s.ts.isoformat(timespec="milliseconds"),
                "instrument": s.instrument,
                "strategy": s.strategy,
                "direction": s.direction,
                "confidence": s.confidence,
                "entry_reason": s.entry_reason,
                "reject_reason": s.reject_reason,
                "conditions": s.conditions,
                "market_price": s.market_price,
                "sl": s.sl,
                "tp": s.tp,
                "risk_reward": s.risk_reward,
                "status": s.status,
            }
            for s in rows
        ]
