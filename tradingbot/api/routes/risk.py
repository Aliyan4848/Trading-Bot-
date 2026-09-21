"""Risk-control routes.

The kill switch and pause are the highest-priority controls: they are
persisted to the DB (survive restarts) and broadcast to all dashboard
clients immediately. Phase 5 wires the deterministic risk service; the
state it reads is already here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from tradingbot.api.auth import require_auth
from tradingbot.core.events import Event
from tradingbot.core.logging import get_logger
from tradingbot.db.base import session_scope
from tradingbot.db.models import AppSettings, RiskEvent

log = get_logger("tradingbot.api.risk")


class KillSwitchPayload(BaseModel):
    enabled: bool
    reason: str = "manual"


class PausePayload(BaseModel):
    paused: bool
    reason: str = "manual"


router = APIRouter(dependencies=[Depends(require_auth)])


async def _load_or_create(session_factory) -> AppSettings:
    async with session_scope(session_factory) as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == AppSettings.row_id()))
        ).scalar_one_or_none()
        if row is None:
            row = AppSettings(id=AppSettings.row_id())
            session.add(row)
        return row


@router.get("/risk")
async def get_risk(request: Request) -> dict:
    settings = request.app.state.settings
    row = await _load_or_create(request.app.state.sessions)
    return {
        "trading_mode": settings.trading_mode.value,
        "kill_switch": row.kill_switch,
        "kill_reason": row.kill_reason,
        "trading_paused": row.trading_paused,
        "pause_reason": row.pause_reason,
        "risk_config": row.risk_config,
        "updated_ts": row.updated_ts.isoformat(timespec="milliseconds"),
    }


@router.post("/risk/kill-switch")
async def set_kill_switch(payload: KillSwitchPayload, request: Request) -> dict:
    """GLOBAL KILL SWITCH: stops all new trade execution. Persists across restarts."""
    session_factory = request.app.state.sessions
    async with session_scope(session_factory) as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == AppSettings.row_id()))
        ).scalar_one_or_none()
        if row is None:
            row = AppSettings(id=AppSettings.row_id())
            session.add(row)
        row.kill_switch = payload.enabled
        row.kill_reason = payload.reason if payload.enabled else None
        from tradingbot.core.timeutils import utcnow

        row.updated_ts = utcnow()
        session.add(RiskEvent(
            rule="kill_switch",
            severity="CRITICAL" if payload.enabled else "INFO",
            blocked=payload.enabled,
            detail={"reason": payload.reason},
        ))

    await request.app.state.bus.publish(Event(type="kill_switch", data={
        "enabled": payload.enabled, "reason": payload.reason,
    }))
    log.warning("KILL SWITCH " + ("ENGAGED" if payload.enabled else "RELEASED"),
                reason=payload.reason)
    return {"kill_switch": payload.enabled, "reason": payload.reason}


@router.post("/risk/pause")
async def set_pause(payload: PausePayload, request: Request) -> dict:
    """Trading pause: like the kill switch but lower severity (manual resume expected)."""
    async with session_scope(request.app.state.sessions) as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == AppSettings.row_id()))
        ).scalar_one_or_none()
        if row is None:
            row = AppSettings(id=AppSettings.row_id())
            session.add(row)
        row.trading_paused = payload.paused
        row.pause_reason = payload.reason if payload.paused else None
        from tradingbot.core.timeutils import utcnow

        row.updated_ts = utcnow()
        session.add(RiskEvent(
            rule="trading_pause",
            severity="WARN" if payload.paused else "INFO",
            blocked=payload.paused,
            detail={"reason": payload.reason},
        ))

    await request.app.state.bus.publish(Event(type="trading_paused", data={
        "paused": payload.paused, "reason": payload.reason,
    }))
    return {"trading_paused": payload.paused, "reason": payload.reason}
