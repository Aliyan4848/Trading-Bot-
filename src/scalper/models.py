"""Typed domain objects shared by every layer of the bot.

Nothing in here imports the broker, the data layer, or the strategy code, so it
is safe to use from the engine, the backtester, and the tests alike.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    """Side of the market an order or position is exposed to."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short. Handy for P&L arithmetic."""
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    SIGNAL_FLIP = "signal_flip"
    SESSION_END = "session_end"
    MAX_HOLD = "max_hold"
    MANUAL = "manual"
    KILL_SWITCH = "kill_switch"
    END_OF_DATA = "end_of_data"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


@dataclass(slots=True)
class Bar:
    """A single OHLC candle. Volume defaults to 0 because FX spot often has none."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    symbol: str = ""

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass(slots=True)
class Signal:
    """A strategy's *intent*. Position sizing happens later, in the risk layer."""

    direction: Direction
    stop_pips: float
    take_profit_pips: float | None = None
    reason: str = ""
    strength: float = 1.0
    strategy: str = ""
    symbol: str = ""
    time: datetime | None = None
    # Optional hard levels; when absent the engine derives them from pip distances.
    stop_price: float | None = None
    take_profit_price: float | None = None
    limit_price: float | None = None  # None -> market order

    def __post_init__(self) -> None:
        if self.stop_pips <= 0 and self.stop_price is None:
            raise ValueError("Signal needs either a positive stop_pips or an explicit stop_price")

    @property
    def reward_risk(self) -> float | None:
        if self.take_profit_pips is None or self.stop_pips <= 0:
            return None
        return self.take_profit_pips / self.stop_pips


@dataclass(slots=True)
class OrderRequest:
    """What the strategy/risk layer asks the broker to do."""

    symbol: str
    direction: Direction
    lots: float
    stop_price: float
    take_profit_price: float | None
    time: datetime
    limit_price: float | None = None
    #: The market price the entry decision was made from (the signal bar's open
    #: for next-bar fills, or its close for same-bar fills). A market order must
    #: fill from THIS price, never from a later bar's close.
    reference_price: float | None = None
    comment: str = ""
    strategy: str = ""
    risk_amount: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def is_market(self) -> bool:
        return self.limit_price is None


@dataclass(slots=True)
class Fill:
    """The broker's answer to an OrderRequest."""

    order_id: str
    symbol: str
    direction: Direction
    lots: float
    price: float
    time: datetime
    stop_price: float | None = None
    take_profit_price: float | None = None
    commission: float = 0.0
    slippage_pips: float = 0.0
    comment: str = ""
    ticket: int | None = None  # broker-side identifier (MT5 ticket)

    @property
    def is_open(self) -> bool:
        return self.price > 0


@dataclass(slots=True)
class Position:
    """An open trade, marked to market as bars arrive."""

    symbol: str
    direction: Direction
    lots: float
    entry_price: float
    entry_time: datetime
    stop_price: float
    take_profit_price: float | None
    pip_size: float
    pip_value_per_lot: float
    strategy: str = ""
    risk_amount: float = 0.0
    comment: str = ""
    ticket: int | None = None
    order_id: str = ""
    opened_bar_index: int = -1
    #: The stop as placed at entry, never updated. R must be measured against the
    #: risk that was actually taken on, otherwise a trailing stop silently
    #: inflates every R multiple (the stop becomes a profit level, so
    #: |entry - stop| shrinks and division explodes).
    initial_stop_price: float = 0.0
    # Highest/lowest price seen since entry — used by trailing stops.
    mfe_price: float = 0.0
    mae_price: float = 0.0

    def __post_init__(self) -> None:
        if self.mfe_price == 0.0:
            self.mfe_price = self.entry_price
        if self.mae_price == 0.0:
            self.mae_price = self.entry_price
        if self.initial_stop_price == 0.0:
            self.initial_stop_price = self.stop_price

    @property
    def initial_risk_price(self) -> float:
        """Price distance risked at entry (1R), fixed for the life of the trade."""
        return abs(self.entry_price - self.initial_stop_price)

    @property
    def initial_risk_money(self) -> float:
        """Account-currency risk at entry (the 1R of this trade)."""
        if self.pip_size <= 0:
            return 0.0
        return abs(self.entry_price - self.stop_price) / self.pip_size * self.pip_value_per_lot * self.lots

    def pips(self, price: float) -> float:
        """Convert a raw price into signed pips from entry."""
        if self.pip_size <= 0:
            return 0.0
        return (price - self.entry_price) * self.direction.sign / self.pip_size

    def r_multiple(self, price: float) -> float:
        risk = self.initial_risk_price
        if risk <= 0:
            return 0.0
        return (price - self.entry_price) * self.direction.sign / risk

    def unrealized_pnl(self, price: float) -> float:
        if self.pip_size <= 0:
            return 0.0
        pips = (price - self.entry_price) * self.direction.sign / self.pip_size
        return pips * self.pip_value_per_lot * self.lots

    def update_extremes(self, high: float, low: float) -> None:
        if self.direction is Direction.LONG:
            self.mfe_price = max(self.mfe_price, high)
            self.mae_price = min(self.mae_price, low)
        else:
            self.mfe_price = min(self.mfe_price, low)
            self.mae_price = max(self.mae_price, high)


@dataclass(slots=True)
class Trade:
    """A closed position, plus everything needed to audit *why* it closed."""

    symbol: str
    direction: Direction
    lots: float
    entry_price: float
    entry_time: datetime
    exit_price: float
    exit_time: datetime
    stop_price: float
    take_profit_price: float | None
    gross_pnl: float
    commission: float
    net_pnl: float
    pips: float
    r_multiple: float
    exit_reason: ExitReason
    strategy: str = ""
    comment: str = ""
    ticket: int | None = None
    bars_held: int = 0

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def duration_minutes(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 60.0


@dataclass(slots=True)
class AccountSnapshot:
    """Equity curve point. Written once per bar by the engine."""

    time: datetime
    balance: float
    equity: float
    open_positions: int = 0
    unrealized: float = 0.0

    @property
    def drawdown(self) -> float:
        return self.balance - self.equity
