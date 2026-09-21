"""FastAPI application factory.

One process runs the dashboard API, the WebSocket hub, and the trading
engine loop (asyncio). This keeps v1 deployment to a single container while
the engine stays off the request path (non-blocking async). Splitting the
engine into its own process is a Phase 12 consideration if profiling shows
contention.
"""

from __future__ import annotations

import contextlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tradingbot import __version__
from tradingbot.api.routes import router as api_router
from tradingbot.api.routes.websocket import router as ws_router
from tradingbot.api.ws_hub import ConnectionManager
from tradingbot.broker.interfaces import BrokerError
from tradingbot.broker.registry import create_broker
from tradingbot.core.config import Settings, get_settings
from tradingbot.core.events import EventBus
from tradingbot.core.logging import configure_logging, get_logger
from tradingbot.db.base import create_engine, create_session_factory, init_db
from tradingbot.db.models import AppSettings
from tradingbot.engine.engine import TradingEngine

log = get_logger("tradingbot.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.bus = EventBus()

    engine_ = create_engine(settings.database_url)
    app.state.db_engine = engine_
    app.state.sessions = create_session_factory(engine_)

    broker = create_broker(settings)
    app.state.broker = broker
    app.state.engine = TradingEngine(settings, app.state.bus, broker, app.state.sessions)
    app.state.ws_manager = ConnectionManager(app.state.bus)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)
    app.include_router(ws_router)

    @app.on_event("startup")
    async def _startup() -> None:
        await init_db(engine_)
        # ensure the app-settings row exists (kill switch state survives restarts)
        from tradingbot.db.base import session_scope

        async with session_scope(app.state.sessions) as session:
            from sqlalchemy import select

            row = (
                await session.execute(select(AppSettings).where(AppSettings.id == AppSettings.row_id()))
            ).scalar_one_or_none()
            if row is None:
                session.add(AppSettings(id=AppSettings.row_id()))
        if settings.api_token is None:
            log.warning("API_TOKEN not set — dashboard auth DISABLED (local development only)")
        try:
            await app.state.engine.start()
        except BrokerError as exc:
            log.error("broker failed to start", code=exc.code, error=str(exc))
            raise
        if not settings.llm_enabled:
            log.info("LLM analysis disabled (no LLM_* env vars) — deterministic-only mode")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        with contextlib.suppress(Exception):
            await app.state.engine.stop()
        await engine_.dispose()
        await app.state.bus.close()

    return app
