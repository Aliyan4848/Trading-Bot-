"""Time helpers. Everything in the system is UTC."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def floor_ms(dt: datetime) -> datetime:
    """Truncate to whole seconds (millisecond component kept as 0)."""
    return dt.replace(microsecond=0)
