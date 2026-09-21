"""Database schema v1.

Money-critical design rules:
  * every order transition is an append-only row in ``order_journal``;
  * ``signals`` records both accepted and *rejected* decisions (with reasons);
  * ``risk_events`` records every risk violation / kill-switch action;
  * ``account_snapshots`` gives a time series of the account state.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tradingbot.core.timeutils import utcnow
from tradingbot.db.base import Base


class SignalDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class SignalStatus(StrEnum):
    GENERATED = "GENERATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


class OrderState(StrEnum):
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    ORDER_REJECTED = "ORDER_REJECTED"
    EXECUTION_ERROR = "EXECUTION_ERROR"


class BaseTable(Base):
    """Common column defaults."""

    __abstract__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Signal(BaseTable):
    __tablename__ = "signals"

    instrument: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(8), default=SignalDirection.HOLD.value)
    confidence: Mapped[float | None]
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)  # entry/exit condition detail
    entry_price: Mapped[float | None]
    sl: Mapped[float | None]
    tp: Mapped[float | None]
    risk_reward: Mapped[float | None]
    market_price: Mapped[float]  # price at decision time
    ai_recommendation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=SignalStatus.GENERATED.value, index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("order_journal.id"), nullable=True)

    __table_args__ = (Index("ix_signals_instrument_ts", "instrument", "ts"),)


class OrderJournal(BaseTable):
    """Append-only order lifecycle journal (state machine, one row per transition is optional;
    v1 stores the current state per order and appends transitions to system_events via the bus)."""

    __tablename__ = "order_journal"

    order_uid: Mapped[str] = mapped_column(String(40), unique=True, index=True)  # internal unique id
    client_request_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)  # idempotency key
    operation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # broker op id
    broker: Mapped[str] = mapped_column(String(16), default="paper")
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float]
    state: Mapped[str] = mapped_column(String(24), default=OrderState.SIGNAL_GENERATED.value, index=True)
    price_requested: Mapped[float | None]
    price_filled: Mapped[float | None]
    sl: Mapped[float | None]
    tp: Mapped[float | None]
    slippage: Mapped[float | None]  # filled - requested, in price units
    spread_at_entry: Mapped[float | None]
    error_code: Mapped[str | None]
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_id: Mapped[str | None]
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None]
    exit_reason: Mapped[str | None]  # SL | TP | MANUAL | STRATEGY | SHUTDOWN
    gross_pnl: Mapped[float | None]
    net_pnl: Mapped[float | None]
    costs: Mapped[float | None]


class Trade(BaseTable):
    """A completed round trip (entry -> exit), denormalized for reporting."""

    __tablename__ = "trades"

    order_uid: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float]
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_price: Mapped[float]
    exit_price: Mapped[float | None]
    gross_pnl: Mapped[float | None]
    costs: Mapped[float | None]
    net_pnl: Mapped[float | None]
    duration_s: Mapped[float | None]
    exit_reason: Mapped[str | None]
    execution_status: Mapped[str] = mapped_column(String(24), default="FILLED")


class RiskEvent(BaseTable):
    __tablename__ = "risk_events"

    rule: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="WARN")  # INFO | WARN | CRITICAL
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    order_uid: Mapped[str | None]


class AccountSnapshot(BaseTable):
    __tablename__ = "account_snapshots"

    account_id: Mapped[str] = mapped_column(String(40), index=True)
    currency: Mapped[str] = mapped_column(String(8))
    balance: Mapped[float]
    equity: Mapped[float]
    free_margin: Mapped[float]
    margin_level: Mapped[float | None]
    leverage: Mapped[int]
    open_positions: Mapped[int]
    unrealized_pnl: Mapped[float]

    __table_args__ = (Index("ix_snapshots_account_ts", "account_id", "ts"),)


class Candle(BaseTable):
    __tablename__ = "candles"

    instrument: Mapped[str] = mapped_column(String(16), index=True)
    timeframe_s: Mapped[int] = mapped_column(Integer, index=True)
    ts_open: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float]
    high: Mapped[float]
    low: Mapped[float]
    close: Mapped[float]
    volume_ticks: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("instrument", "timeframe_s", "ts_open", name="uq_candle"),
        Index("ix_candles_lookup", "instrument", "timeframe_s", "ts_open"),
    )


class SystemLog(BaseTable):
    __tablename__ = "system_logs"

    level: Mapped[str] = mapped_column(String(8), index=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    message: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class AppSettings(Base):
    """Single-row runtime settings (kill switch, pause, risk params JSON)."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    trading_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_config: Mapped[dict] = mapped_column(JSON, default=dict)  # Phase 5 risk parameters
    updated_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @staticmethod
    def row_id() -> int:
        return 1
