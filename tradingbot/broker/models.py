"""Broker-layer data models (pydantic, shared by all broker adapters)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Tick(BaseModel):
    instrument: str
    bid: float
    ask: float
    ts: datetime

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class Candle(BaseModel):
    instrument: str
    timeframe_s: int
    ts_open: datetime
    open: float
    high: float
    low: float
    close: float
    volume_ticks: int = 0


class InstrumentConditions(BaseModel):
    instrument: str
    point_digits: int
    contract_size: float  # units of base per 1.0 volume (100000 for FX, 100 for XAUUSD)
    currency: str = "USD"
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    spread: float = 0.0  # typical spread (ask - bid), used by risk gates / paper fills


class AccountInfo(BaseModel):
    account_id: str
    currency: str
    balance: float
    equity: float
    free_margin: float
    margin_level: float | None  # percent, None when no used margin
    leverage: int
    open_positions: int
    unrealized_pnl: float
    ts: datetime


class Position(BaseModel):
    id: str
    instrument: str
    side: Side
    volume: float
    open_price: float
    sl: float | None = None
    tp: float | None = None
    open_ts: datetime
    unrealized_pnl: float = 0.0


class OrderRequest(BaseModel):
    instrument: str
    side: Side
    volume: float
    price: float | None = None  # None => market execution
    sl: float | None = None
    tp: float | None = None
    client_request_id: str = Field(min_length=5, max_length=128)
    comment: str | None = None

    def payload_fingerprint(self) -> str:
        """Stable hash of the economic payload (excludes client_request_id)."""
        canonical = {
            "instrument": self.instrument,
            "side": self.side.value,
            "volume": self.volume,
            "price": self.price,
            "sl": self.sl,
            "tp": self.tp,
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class OperationState(StrEnum):
    ACCEPTED = "accepted"
    FILLED = "filled"
    CLOSED = "closed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OperationStatus(BaseModel):
    operation_id: str
    client_request_id: str
    state: OperationState
    price: float | None = None
    volume: float | None = None
    position_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    ts: datetime


class Deal(BaseModel):
    id: str
    operation_id: str | None
    position_id: str
    instrument: str
    side: Side
    volume: float
    price: float
    pnl: float | None = None  # realized pnl on close deals
    commission: float = 0.0
    kind: Literal["open", "close"] = "open"
    ts: datetime
    reason: str | None = None  # e.g. SL, TP, MANUAL, STRATEGY, SHUTDOWN
