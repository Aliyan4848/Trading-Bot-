from __future__ import annotations

import pytest

from tradingbot.core.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Fast, deterministic settings for tests (paper broker, file sqlite)."""
    return Settings(
        _env_file=None,
        environment="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        tick_interval_ms=40,
        account_snapshot_interval_s=0.1,
        heartbeat_interval_s=0.2,
        paper_random_seed=7,
        api_token="test-token",
        log_level="DEBUG",
        instruments=["EURUSD", "GBPUSD", "XAUUSD"],
    )
