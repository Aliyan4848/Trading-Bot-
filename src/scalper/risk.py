"""Position sizing and the safety rails around it.

Every trade goes through `RiskManager` before it reaches a broker. Its job:

1. **Size to a fixed fraction of equity.** Risk per trade is
   ``equity * risk_per_trade_pct / 100``; lots are derived from the stop
   distance and the instrument's pip value, then rounded *down* to the lot step
   so rounding never increases risk.
2. **Refuse bad trades.** Spread too wide, too many positions, too many trades
   today, symbol not tradeable, lot below the broker minimum.
3. **Stop the bleeding.** A daily loss cap and a peak-to-trough drawdown kill
   switch. Once tripped, no size calculation can override them.

The class is deliberately stateful and explicit about *why* it blocked a trade —
"no signal" and "you already lost 3% today" should never look the same in a log.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from .config import AppConfig, InstrumentSpec, RiskConfig

BLOCK_OK = "ok"


@dataclass
class RiskState:
    """Mutable intraday counters, reset on `reset_day`."""

    day: date | None = None
    day_start_balance: float = 0.0
    day_realized_pnl: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    peak_equity: float = 0.0
    last_trade_time: datetime | None = None
    halted: bool = False
    halt_reason: str = ""
    #: Reasons that already produced a log line, so we do not spam one per bar.
    seen_blocks: set[str] = field(default_factory=set)


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str = BLOCK_OK
    lots: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


class RiskManager:
    def __init__(self, cfg: RiskConfig, account_currency: str = "USD", initial_balance: float = 0.0) -> None:
        self.cfg = cfg
        self.account_currency = account_currency
        self.state = RiskState(
            day_start_balance=initial_balance,
            peak_equity=initial_balance,
        )

    # -- state transitions ----------------------------------------------------
    def reset_day(self, day: date, balance: float) -> None:
        self.state.day = day
        self.state.day_start_balance = balance
        self.state.day_realized_pnl = 0.0
        self.state.trades_today = 0
        self.state.seen_blocks.clear()

    def on_equity(self, equity: float) -> None:
        self.state.peak_equity = max(self.state.peak_equity, equity)
        limit = self.cfg.max_drawdown_pct
        if limit and self.state.peak_equity > 0:
            dd_pct = (self.state.peak_equity - equity) / self.state.peak_equity * 100.0
            if dd_pct >= limit:
                self.halt(
                    f"max drawdown kill switch: equity {equity:,.2f} is {dd_pct:.2f}% "
                    f"below peak {self.state.peak_equity:,.2f} (limit {limit:.2f}%)"
                )

    def on_trade_closed(self, net_pnl: float, when: datetime) -> None:
        self.state.day_realized_pnl += net_pnl
        self.state.consecutive_losses = self.state.consecutive_losses + 1 if net_pnl < 0 else 0
        self.state.last_trade_time = when

    def on_trade_opened(self, when: datetime) -> None:
        self.state.trades_today += 1
        self.state.last_trade_time = when

    def halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.peak_equity = 0.0

    # -- gates ----------------------------------------------------------------
    @property
    def daily_loss_hit(self) -> bool:
        base = self.state.day_start_balance
        if base <= 0 or not self.cfg.max_daily_loss_pct:
            return False
        loss_pct = -self.state.day_realized_pnl / base * 100.0
        return loss_pct >= self.cfg.max_daily_loss_pct

    @property
    def daily_loss_pct(self) -> float:
        base = self.state.day_start_balance
        if base <= 0:
            return 0.0
        return -self.state.day_realized_pnl / base * 100.0

    def check(
        self,
        *,
        now: datetime,
        equity: float,
        open_positions: int,
        spread_pips: float | None = None,
        symbol_open: bool = False,
    ) -> RiskDecision:
        """Answer "may I open a new position right now?" plus the reason if not."""
        st = self.state
        if st.halted:
            return RiskDecision(False, f"halted: {st.halt_reason}")
        if symbol_open:
            return RiskDecision(False, "position already open on this symbol")
        if open_positions >= self.cfg.max_concurrent_positions:
            return RiskDecision(
                False, f"max concurrent positions ({self.cfg.max_concurrent_positions}) reached"
            )
        if self.daily_loss_hit:
            return RiskDecision(
                False,
                f"daily loss limit hit ({self.daily_loss_pct:.2f}% >= {self.cfg.max_daily_loss_pct}%)",
            )
        if self.cfg.max_trades_per_day and st.trades_today >= self.cfg.max_trades_per_day:
            return RiskDecision(False, f"max trades/day ({self.cfg.max_trades_per_day}) reached")
        if (
            self.cfg.min_seconds_between_trades
            and st.last_trade_time is not None
            and (now - st.last_trade_time).total_seconds() < self.cfg.min_seconds_between_trades
        ):
            return RiskDecision(False, "cooldown between trades still active")
        if spread_pips is not None and self.cfg.max_spread_pips and spread_pips > self.cfg.max_spread_pips:
            return RiskDecision(False, f"spread {spread_pips:.2f} pips > max {self.cfg.max_spread_pips:.2f}")
        if equity <= 0:
            return RiskDecision(False, "equity is zero or negative")
        return RiskDecision(True)

    # -- sizing ---------------------------------------------------------------
    def size(
        self,
        *,
        equity: float,
        spec: InstrumentSpec,
        stop_pips: float,
        risk_per_trade_pct: float | None = None,
    ) -> RiskDecision:
        """Convert a stop distance into a lot size at fixed fractional risk."""
        if stop_pips <= 0:
            return RiskDecision(False, "stop distance is zero — refusing to size an unbounded trade")
        if spec.pip_value_per_lot <= 0:
            return RiskDecision(False, f"{spec.symbol}: pip_value_per_lot must be > 0")

        pct = self.cfg.risk_per_trade_pct if risk_per_trade_pct is None else risk_per_trade_pct
        risk_amount = equity * pct / 100.0
        raw_lots = risk_amount / (stop_pips * spec.pip_value_per_lot)
        lots = self.round_lots(raw_lots)

        if lots < self.cfg.min_lot:
            return RiskDecision(
                False,
                f"sized {raw_lots:.4f} lots < broker minimum {self.cfg.min_lot} — "
                f"stop is too wide for {pct}% risk on {equity:,.2f} {self.account_currency}",
            )
        return RiskDecision(True, BLOCK_OK, lots)

    def round_lots(self, lots: float) -> float:
        """Round DOWN to the lot step so rounding never increases risk."""
        step = self.cfg.lot_step or 0.01
        if lots <= 0:
            return 0.0
        steps = math.floor(lots / step + 1e-9)
        rounded = round(steps * step, 8)
        return min(rounded, self.cfg.max_lot)

    def risk_amount(self, equity: float, lots: float, stop_pips: float, spec: InstrumentSpec) -> float:
        return lots * stop_pips * spec.pip_value_per_lot

    # -- reporting ------------------------------------------------------------
    def summary(self) -> dict[str, float | int | str | bool]:
        st = self.state
        return {
            "trades_today": st.trades_today,
            "day_realized_pnl": round(st.day_realized_pnl, 2),
            "daily_loss_pct": round(self.daily_loss_pct, 3),
            "daily_loss_limit_pct": self.cfg.max_daily_loss_pct,
            "daily_loss_hit": self.daily_loss_hit,
            "peak_equity": round(st.peak_equity, 2),
            "halted": st.halted,
            "halt_reason": st.halt_reason,
            "consecutive_losses": st.consecutive_losses,
        }

    def describe(self) -> str:
        c = self.cfg
        return (
            f"risk {c.risk_per_trade_pct}%/trade, daily stop {c.max_daily_loss_pct}%, "
            f"max DD {c.max_drawdown_pct}%, max {c.max_concurrent_positions} positions, "
            f"max {c.max_trades_per_day} trades/day"
        )


def build_risk_manager(cfg: AppConfig) -> RiskManager:
    return RiskManager(
        cfg.risk,
        account_currency=cfg.account.currency,
        initial_balance=cfg.account.initial_balance,
    )
