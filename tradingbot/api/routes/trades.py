from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from tradingbot.api.auth import require_auth
from tradingbot.db.base import session_scope
from tradingbot.db.models import Trade

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/trades")
async def get_trades(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    instrument: str | None = None,
) -> list[dict]:
    """Completed round trips (backtest/paper/demo results are labelled in ``execution_status``)."""
    async with session_scope(request.app.state.sessions) as session:
        stmt = select(Trade).order_by(Trade.entry_ts.desc()).limit(limit)
        if instrument:
            stmt = stmt.where(Trade.instrument == instrument.upper())
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "order_uid": t.order_uid,
                "instrument": t.instrument,
                "side": t.side,
                "volume": t.volume,
                "strategy": t.strategy,
                "entry_ts": t.entry_ts.isoformat(timespec="milliseconds"),
                "exit_ts": t.exit_ts.isoformat(timespec="milliseconds") if t.exit_ts else None,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "gross_pnl": t.gross_pnl,
                "costs": t.costs,
                "net_pnl": t.net_pnl,
                "duration_s": t.duration_s,
                "exit_reason": t.exit_reason,
                "execution_status": t.execution_status,
            }
            for t in rows
        ]
