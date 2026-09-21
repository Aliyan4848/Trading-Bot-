"""Async SQLAlchemy engine/session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# NOTE: models import here would create a circular import (models needs Base).
# Importers (api app, alembic env, tests) import ``tradingbot.db.models`` which
# registers the tables on Base before ``init_db`` is ever called.


class Base(DeclarativeBase):
    pass


def ensure_sqlite_dir(url: str) -> None:
    """SQLite cannot create missing parent directories — do it here."""
    from pathlib import Path

    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return
    path = parsed.database
    if not path or path == ":memory:":
        return
    parent = Path(path).expanduser().parent
    if str(parent) not in (".", ""):
        parent.mkdir(parents=True, exist_ok=True)


def create_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    ensure_sqlite_dir(url)
    kwargs: dict = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    return create_async_engine(url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Commit on success, rollback on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(engine: AsyncEngine) -> None:
    """Create all tables (dev path). Production uses `alembic upgrade head`."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
