"""Application configuration.

All sensitive values come from environment variables (or a local .env file).
Nothing in this module is secret; the values are loaded at startup and are the
single source of truth for the whole system.

Safety locks enforced here (fail fast, refuse to start):
  * ``trading_mode`` only accepts ``simulation`` or ``demo`` — there is no
    ``live`` value in this codebase.
  * ``allow_live_trading`` must be ``False``; ``True`` raises at startup.
  * the Exness broker requires ``exn_account_is_demo == True``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    """No LIVE value exists on purpose."""

    SIMULATION = "simulation"
    DEMO = "demo"


class BrokerKind(StrEnum):
    PAPER = "paper"
    EXNESS = "exness"
    MT5 = "mt5"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ----- app -----------------------------------------------------------
    app_name: str = "AI Trading Bot"
    app_version: str = "0.2.0"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # ----- trading safety locks ------------------------------------------
    trading_mode: TradingMode = TradingMode.SIMULATION
    allow_live_trading: bool = False
    broker: BrokerKind = BrokerKind.PAPER

    # ----- market / engine -------------------------------------------------
    instruments: list[str] = ["EURUSD", "GBPUSD", "XAUUSD"]
    bar_timeframes_s: list[int] = [5, 10, 60]
    tick_interval_ms: int = 250
    account_snapshot_interval_s: float = 30.0
    heartbeat_interval_s: float = 5.0
    max_bars_kept: int = 500

    # ----- paper broker (synthetic market; development only) ---------------
    paper_start_balance: float = 10_000.0
    paper_leverage: int = 200
    paper_slippage_bps: float = 0.0
    paper_ack_delay_s: float = 0.05
    paper_random_seed: int | None = 42

    # ----- api server --------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: str | None = None
    cors_origins: list[str] = ["*"]

    # ----- database ----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/dev.db"

    # ----- exness (Phase 3 / Phase 10) --------------------------------------
    exn_api_key: str | None = None
    exn_private_key: str | None = None
    exn_account_id: str | None = None
    exn_api_base_url: str | None = None
    exn_account_is_demo: bool = False
    exn_rest_timeout_s: float = 10.0
    exn_ws_reconnect_max_s: float = 30.0
    exn_op_timeout_s: float = 10.0  # ACK -> final-state wait before REST polling

    # ----- mt5 fallback (Windows host only) ---------------------------------
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None

    # ----- llm analysis (optional, advisory only) ---------------------------
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_s: float = 10.0

    @model_validator(mode="after")
    def _enforce_safety_locks(self) -> Settings:
        if self.allow_live_trading:
            raise ValueError(
                "ALLOW_LIVE_TRADING=true is rejected: live trading requires a "
                "separate safety review and is not supported by this codebase."
            )
        if self.broker is BrokerKind.EXNESS:
            if not self.exn_account_is_demo:
                raise ValueError(
                    "BROKER=exness requires EXN_ACCOUNT_IS_DEMO=true (demo-only restriction)."
                )
            missing = [
                name
                for name, val in (
                    ("EXN_API_KEY", self.exn_api_key),
                    ("EXN_PRIVATE_KEY", self.exn_private_key),
                    ("EXN_ACCOUNT_ID", self.exn_account_id),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    "BROKER=exness requires: " + ", ".join(missing)
                )
        for tf in self.bar_timeframes_s:
            if tf <= 0:
                raise ValueError("bar timeframes must be positive seconds")
        return self

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def db_sync_url(self) -> str:
        """Synchronous URL for Alembic metadata operations."""
        url = self.database_url
        url = url.replace("+aiosqlite", "").replace("+asyncpg", "")
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://") :]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
