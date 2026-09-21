"""Safety-lock tests: the system must refuse unsafe configurations at startup."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tradingbot.core.config import Settings


def _make(**overrides) -> Settings:
    kwargs = dict(_env_file=None)
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_defaults_are_safe() -> None:
    s = _make()
    assert s.trading_mode.value in ("simulation", "demo")
    assert s.allow_live_trading is False
    assert s.broker.value == "paper"


def test_live_mode_value_does_not_exist() -> None:
    with pytest.raises(ValidationError):
        _make(trading_mode="live")


def test_allow_live_trading_rejected() -> None:
    with pytest.raises(ValidationError, match="live trading"):
        _make(allow_live_trading=True)


def test_exness_requires_demo_flag() -> None:
    with pytest.raises(ValidationError, match="demo-only"):
        _make(broker="exness", exn_account_is_demo=False)
    s = _make(broker="exness", exn_account_is_demo=True)
    assert s.broker.value == "exness"


def test_negative_timeframe_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(bar_timeframes_s=[-5])


def test_db_sync_url_conversion() -> None:
    s = _make(database_url="postgresql+asyncpg://u:p@h:5432/db")
    assert s.db_sync_url == "postgresql+psycopg://u:p@h:5432/db"
    s2 = _make(database_url="sqlite+aiosqlite:////tmp/x.db")
    assert s2.db_sync_url == "sqlite:////tmp/x.db"
