"""The trading engine: turns signals into positions, and positions into trades.

This is the one place where the trading rules live, and it is shared verbatim by
the backtester, paper trading and live trading. If a backtest and a live session
disagree, the bug is in the data or the broker — not in two divergent code paths.

Order of operations on every bar. This ordering is what keeps the simulation
honest; getting it wrong is how backtests accidentally look excellent:

1. **Service existing positions.** Stops and targets are evaluated against the
   bar that just arrived, *before* any new entry, so a position can never be
   opened and closed against the same bar's range using its future.
2. **Update equity and risk state** (daily reset, drawdown kill switch).
3. **Consider a new entry** using the *previous* bar's signal, filled at this
   bar's open. A signal is only ever acted on after the bar that produced it
   has closed.
4. **Trail / break-even**, derived from the best price seen so far, applied from
   the next bar onward (we cannot know whether the bar's high preceded its low).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any

import numpy as np
import pandas as pd

from .brokers.base import Broker, BrokerError
from .config import TIMEFRAME_MINUTES, AppConfig, InstrumentSpec
from .models import (
    AccountSnapshot,
    Bar,
    Direction,
    ExitReason,
    Fill,
    OrderRequest,
    Position,
    Signal,
    Trade,
)
from .risk import RiskManager
from .strategies.base import Strategy

log = logging.getLogger("scalper.engine")


# -----------------------------------------------------------------------------
# Sessions
# -----------------------------------------------------------------------------
def _parse_hhmm(value: str) -> dtime:
    hour, _, minute = str(value).partition(":")
    return dtime(int(hour), int(minute or 0))


class SessionFilter:
    """Decides when new entries are allowed, and when to go flat."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg.session
        self._timeframe = timedelta(minutes=TIMEFRAME_MINUTES.get(str(cfg.data.timeframe).upper(), 1))
        self.skip_friday_after = (
            _parse_hhmm(self.cfg.skip_friday_after) if self.cfg.skip_friday_after else None
        )
        self._tz = None
        if self.cfg.timezone and str(self.cfg.timezone).upper() != "UTC":
            import zoneinfo

            self._tz = zoneinfo.ZoneInfo(str(self.cfg.timezone))

    def _local(self, when: datetime) -> datetime:
        return when.astimezone(self._tz) if self._tz is not None else when

    def is_open(self, when: datetime) -> bool:
        """Whether a *new position* may be opened at `when`."""
        if not self.cfg.enabled:
            return True
        local = self._local(when)
        weekday = local.weekday()

        if self.cfg.skip_weekend:
            # FX runs Sunday ~21:00 UTC to Friday ~21:00 UTC.
            if weekday == 5:
                return False
            if weekday == 6 and local.hour < 21:
                return False
            if self.skip_friday_after and weekday == 4 and local.time() >= self.skip_friday_after:
                return False

        if not self.cfg.windows:
            return True
        clock = local.time()
        return any(window.contains(clock) for window in self.cfg.windows)

    def should_flatten(self, when: datetime) -> bool:
        """True on the last bar of a session, so we do not hold overnight.

        Expressed as "the next bar would be outside the session", which works for
        any timeframe and also catches the Friday close automatically.
        """
        if not self.cfg.enabled or not getattr(self.cfg, "flat_at_close", True):
            return False
        return not self.is_open(when + self._timeframe)

    def describe(self) -> str:
        if not self.cfg.enabled:
            return "sessions disabled (24h)"
        windows = ", ".join(f"{w.name} {w.start}-{w.end}" for w in self.cfg.windows)
        return f"{self.cfg.timezone}: {windows}"


# -----------------------------------------------------------------------------
# Engine
# -----------------------------------------------------------------------------
@dataclass
class EngineStats:
    bars_processed: int = 0
    signals_seen: int = 0
    entries_attempted: int = 0
    entries_filled: int = 0
    entries_blocked: int = 0
    block_reasons: dict[str, int] = field(default_factory=dict)
    exits_by_reason: dict[str, int] = field(default_factory=dict)
    broker_errors: int = 0

    def note_block(self, reason: str) -> None:
        """Bucket block reasons so 100k bars do not produce 100k log lines."""
        key = reason.split(" (")[0].strip()
        self.block_reasons[key] = self.block_reasons.get(key, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "bars_processed": self.bars_processed,
            "signals_seen": self.signals_seen,
            "entries_attempted": self.entries_attempted,
            "entries_filled": self.entries_filled,
            "entries_blocked": self.entries_blocked,
            "block_reasons": dict(sorted(self.block_reasons.items(), key=lambda kv: -kv[1])),
            "exits_by_reason": dict(sorted(self.exits_by_reason.items(), key=lambda kv: -kv[1])),
            "broker_errors": self.broker_errors,
        }


class Engine:
    """Symbol-agnostic trading loop shared by backtest, paper and live modes."""

    def __init__(
        self,
        cfg: AppConfig,
        broker: Broker,
        strategy: Strategy,
        risk: RiskManager,
        *,
        entry_on_next_open: bool | None = None,
        record_equity: bool = True,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.sessions = SessionFilter(cfg)
        self.entry_on_next_open = (
            bool(cfg.backtest.entry_on_next_open) if entry_on_next_open is None else entry_on_next_open
        )
        self.record_equity = record_equity

        self.trades: list[Trade] = []
        self.equity_curve: list[AccountSnapshot] = []
        self.stats = EngineStats()

        self._signals: dict[str, pd.DataFrame] = {}
        self._features: dict[str, pd.DataFrame] = {}
        self._arrays: dict[str, dict[str, np.ndarray]] = {}
        self._min_bars: dict[str, int] = {}
        self._index_cache: dict[str, dict[datetime, int]] = {}
        self._last_bar_time: dict[str, datetime] = {}
        self._entry_bar_index: dict[str, int] = {}
        self._live_positions_seen: dict[str, Position] = {}
        self._last_day: date | None = None
        self._flattened_symbols: set[str] = set()

    # -- setup ----------------------------------------------------------------
    def prepare_symbol(self, symbol: str, df: pd.DataFrame, spec: InstrumentSpec) -> pd.DataFrame:
        """Precompute signals for one symbol (vectorised, once per run)."""
        if symbol in self._signals:
            return self._signals[symbol]

        frame = df.copy()
        frame.attrs.update(df.attrs)
        frame.attrs["pip_size"] = spec.pip_size
        prepared = self.strategy.prepare(frame)

        signals = prepared.signals
        self._signals[symbol] = signals
        self._features[symbol] = prepared.features
        # numpy views make the hot loop ~10x faster than .iloc row access.
        self._arrays[symbol] = {
            "signal": signals["signal"].to_numpy(dtype="int8"),
            "stop_pips": signals["stop_pips"].to_numpy(dtype="float64"),
            "tp_pips": signals["tp_pips"].to_numpy(dtype="float64"),
            "reason": signals["reason"].to_numpy(dtype=object),
        }
        self._min_bars[symbol] = max(int(self.strategy.min_bars), int(self.cfg.backtest.warmup_bars))
        if hasattr(self.broker, "register_spec"):
            self.broker.register_spec(spec)  # type: ignore[attr-defined]

        n_long = int((signals["signal"] > 0).sum())
        n_short = int((signals["signal"] < 0).sum())
        log.info(
            "%s: %d bars, %d raw signals (%d long / %d short), warmup %d bars",
            symbol,
            len(signals),
            n_long + n_short,
            n_long,
            n_short,
            self._min_bars[symbol],
        )
        return signals

    def refresh_symbol(self, symbol: str, df: pd.DataFrame, spec: InstrumentSpec) -> pd.DataFrame:
        """Recompute signals for a symbol whose rolling window just grew.

        Used by live polling: each newly closed bar extends the window, and the
        indicators for the *most recent* bars are recomputed so the engine is
        always acting on signals derived from every bar seen so far.
        """
        self._signals.pop(symbol, None)
        self._features.pop(symbol, None)
        self._arrays.pop(symbol, None)
        self._index_cache.pop(symbol, None)
        # Keep the warmup floor from the first preparation.
        floor = self._min_bars.get(symbol)
        signals = self.prepare_symbol(symbol, df, spec)
        if floor is not None:
            self._min_bars[symbol] = floor
            # The window slides, so indices shift; never let a stale floor block
            # or unblock entries incorrectly.
            self._min_bars[symbol] = min(floor, len(df) // 2)
        return signals

    def signals(self, symbol: str) -> pd.DataFrame:
        return self._signals[symbol]

    def features(self, symbol: str) -> pd.DataFrame:
        return self._features.get(symbol, pd.DataFrame())

    @property
    def _broker_simulates_exits(self) -> bool:
        """The paper broker simulates SL/TP; a live venue does it server-side."""
        return bool(getattr(self.broker, "simulates_exits", True))

    # -- main step ------------------------------------------------------------
    def on_bar(self, symbol: str, bar: Bar, bar_index: int) -> list[Trade]:
        """Process one closed bar: exits, state, entries, trailing, session."""
        spec = self.cfg.instrument(symbol)
        self.stats.bars_processed += 1
        closed: list[Trade] = []

        # 1. Positions already open: stops/targets against this bar's range.
        closed.extend(self._service_positions(symbol, bar, spec, bar_index))

        # 2. Account and risk state.
        self._update_risk_state(bar.time)

        # 3. Entry from the previous bar's signal (bar_index - 1 by default).
        if self._may_consider_entry(symbol, bar):
            fill = self._try_entry(symbol, bar, bar_index, spec)
            if fill is not None:
                self._entry_bar_index[symbol] = bar_index
                # The new position lives through the rest of this bar's range.
                closed.extend(self._service_positions(symbol, bar, spec, bar_index))

        # 4. Trail / break-even for whatever is still open.
        self._update_extremes(symbol, bar)
        self._apply_trailing_stops(symbol)

        # 5. Session close: go flat so nothing is held overnight.
        if self.sessions.should_flatten(bar.time):
            closed.extend(self._flatten_symbol(symbol, bar.time, ExitReason.SESSION_END))

        if self.record_equity:
            self._record_equity(bar.time)

        self._last_bar_time[symbol] = bar.time
        return closed

    # -- exits ----------------------------------------------------------------
    def _service_positions(
        self, symbol: str, bar: Bar, spec: InstrumentSpec, bar_index: int
    ) -> list[Trade]:
        if not self._broker_simulates_exits:
            return self._reconcile_live(symbol, bar_index)
        try:
            closed = self.broker.on_bar(bar, spec)  # type: ignore[attr-defined]
        except BrokerError as exc:
            self.stats.broker_errors += 1
            log.error("%s: broker error while processing bar: %s", symbol, exc)
            return []
        for trade in closed:
            self._on_trade_closed(trade, bar_index)
        return closed

    def _reconcile_live(self, symbol: str, bar_index: int | None = None) -> list[Trade]:
        """Detect positions the venue closed for us (server-side SL/TP)."""
        try:
            position = self.broker.position_for(symbol)
        except BrokerError:
            return []
        if position is not None:
            self._live_positions_seen[symbol] = position
            return []

        previous = self._live_positions_seen.pop(symbol, None)
        if previous is None:
            return []
        trade = self._trade_from_history(previous)
        if trade is not None:
            self._on_trade_closed(trade, bar_index)
            return [trade]
        log.warning(
            "%s: position %s vanished without a matching deal in broker history — "
            "check the terminal's account history manually",
            symbol,
            previous.ticket,
        )
        return []

    def _trade_from_history(self, position: Position) -> Trade | None:
        getter = getattr(self.broker, "trade_for_position", None)
        if getter is None:
            return None
        try:
            return getter(position)
        except Exception as exc:  # noqa: BLE001 - never let reconciliation kill the loop
            log.error("Could not reconcile closed position %s: %s", position.ticket, exc)
            return None

    def _on_trade_closed(self, trade: Trade, bar_index: int | None = None) -> None:
        entry_index = self._entry_bar_index.pop(trade.symbol, None)
        if bar_index is not None and entry_index is not None and bar_index >= entry_index:
            trade.bars_held = bar_index - entry_index
        self.trades.append(trade)
        self.risk.on_trade_closed(trade.net_pnl, trade.exit_time)
        key = str(trade.exit_reason)
        self.stats.exits_by_reason[key] = self.stats.exits_by_reason.get(key, 0) + 1
        log.info(
            "CLOSE %s %s %.2f lots @ %.5f -> %.5f | %+.2f %s (%s, %.2fR)",
            trade.symbol,
            trade.direction.value,
            trade.lots,
            trade.entry_price,
            trade.exit_price,
            trade.net_pnl,
            self.cfg.account.currency,
            trade.exit_reason.value,
            trade.r_multiple,
        )

    # -- state ----------------------------------------------------------------
    def _update_risk_state(self, when: datetime) -> None:
        if self._last_day != when.date():
            self._last_day = when.date()
            self.risk.reset_day(when.date(), self.broker.balance())
            self._flattened_symbols.clear()
        self.risk.on_equity(self.broker.equity())
        if self.risk.state.halted:
            for symbol in list(self._last_bar_time) + [s for s in self._signals if s not in self._last_bar_time]:
                self._flatten_symbol(symbol, when, ExitReason.KILL_SWITCH)

    def _record_equity(self, when: datetime) -> None:
        balance = self.broker.balance()
        equity = self.broker.equity()
        self.equity_curve.append(
            AccountSnapshot(
                time=when,
                balance=balance,
                equity=equity,
                open_positions=len(self.broker.positions()),
                unrealized=equity - balance,
            )
        )

    # -- entries --------------------------------------------------------------
    def _may_consider_entry(self, symbol: str, bar: Bar) -> bool:
        if self.risk.state.halted or symbol in self._flattened_symbols:
            return False
        if not self.sessions.is_open(bar.time):
            return False
        if self._signals.get(symbol) is None:
            return False
        return True

    def _row_index(self, symbol: str, when: datetime) -> int | None:
        """Index of `when` within the symbol's signal rows."""
        cache = self._index_cache.get(symbol)
        if cache is None:
            cache = self._index_cache[symbol] = {
                ts: i for i, ts in enumerate(self._signals[symbol].index)
            }
        return cache.get(when)

    def _signal_at(self, symbol: str, bar_index: int) -> Signal | None:
        """The signal to act on when processing the bar at `bar_index`."""
        row_index = bar_index - 1 if self.entry_on_next_open else bar_index
        arrays = self._arrays[symbol]
        if row_index < 0 or row_index >= len(arrays["signal"]):
            return None
        raw = int(arrays["signal"][row_index])
        if raw == 0:
            return None
        stop_pips = float(arrays["stop_pips"][row_index])
        if not (stop_pips > 0) or not np.isfinite(stop_pips):
            return None
        tp = float(arrays["tp_pips"][row_index])
        return Signal(
            direction=Direction.LONG if raw > 0 else Direction.SHORT,
            stop_pips=stop_pips,
            take_profit_pips=tp if np.isfinite(tp) and tp > 0 else None,
            reason=str(arrays["reason"][row_index]),
            strategy=self.strategy.name,
            symbol=symbol,
            time=self._signals[symbol].index[row_index],
        )

    def _try_entry(
        self, symbol: str, bar: Bar, bar_index: int, spec: InstrumentSpec
    ) -> Fill | None:
        signal = self._signal_at(symbol, bar_index)
        if signal is None:
            return None
        self.stats.signals_seen += 1
        self.stats.entries_attempted += 1

        # --- gates -----------------------------------------------------------
        try:
            spread = self.broker.spread_pips(symbol, spec)
        except BrokerError:
            spread = spec.spread_pips
        decision = self.risk.check(
            now=bar.time,
            equity=self.broker.equity(),
            open_positions=len(self.broker.positions()),
            spread_pips=spread,
            symbol_open=self.broker.position_for(symbol) is not None,
        )
        if not decision.allowed:
            self.stats.entries_blocked += 1
            self.stats.note_block(decision.reason)
            log.debug("%s: entry blocked — %s", symbol, decision.reason)
            return None

        # --- sizing ----------------------------------------------------------
        equity = self.broker.equity()
        sized = self.risk.size(equity=equity, spec=spec, stop_pips=signal.stop_pips)
        if not sized.allowed:
            self.stats.entries_blocked += 1
            self.stats.note_block(sized.reason)
            log.debug("%s: sizing refused — %s", symbol, sized.reason)
            return None

        # --- levels ----------------------------------------------------------
        # Absolute price levels derived from the signal bar, exactly as a live
        # bot would compute them from its last completed candle.
        reference = bar.open if self.entry_on_next_open else bar.close
        pip = spec.pip_size
        sign = signal.direction.sign
        stop_price = reference - sign * signal.stop_pips * pip
        tp_price = reference + sign * signal.take_profit_pips * pip if signal.take_profit_pips else None
        if stop_price <= 0 or (tp_price is not None and tp_price <= 0):
            log.debug("%s: computed stop/target is not a valid price, skipping", symbol)
            return None

        request = OrderRequest(
            symbol=symbol,
            direction=signal.direction,
            lots=sized.lots,
            stop_price=round(stop_price, spec.digits),
            take_profit_price=round(tp_price, spec.digits) if tp_price else None,
            time=bar.time,
            reference_price=reference,
            comment=str(signal.reason)[:30],
            strategy=self.strategy.name,
            risk_amount=self.risk.risk_amount(equity, sized.lots, signal.stop_pips, spec),
        )

        try:
            fill = self.broker.open_position(request, spec)
        except BrokerError as exc:
            self.stats.broker_errors += 1
            self.stats.entries_blocked += 1
            self.stats.note_block(f"broker rejected order: {exc}")
            log.warning("%s: order rejected — %s", symbol, exc)
            return None

        self.risk.on_trade_opened(bar.time)
        self.stats.entries_filled += 1
        log.info(
            "OPEN  %s %s %.2f lots @ %.5f (SL %.5f, TP %s) risking %.2f %s | %s",
            symbol,
            signal.direction.value,
            sized.lots,
            fill.price,
            request.stop_price,
            f"{request.take_profit_price:.5f}" if request.take_profit_price else "—",
            request.risk_amount,
            self.cfg.account.currency,
            signal.reason,
        )
        return fill

    # -- position maintenance -------------------------------------------------
    def _update_extremes(self, symbol: str, bar: Bar) -> None:
        """Feed this bar's range into MFE/MAE tracking for trailing stops."""
        position = self.broker.position_for(symbol)
        if position is not None:
            position.update_extremes(bar.high, bar.low)

    def _apply_trailing_stops(self, symbol: str) -> None:
        cfg = self.cfg.risk
        if not cfg.trailing_stop and cfg.break_even_at_r is None:
            return
        position = self.broker.position_for(symbol)
        if position is None:
            return
        risk_price = position.initial_risk_price
        if risk_price <= 0:
            return

        best_r = position.r_multiple(position.mfe_price)
        candidates: list[float] = []
        if cfg.trailing_stop and best_r >= cfg.trailing_start_r:
            trail = cfg.trailing_distance_r * risk_price
            candidates.append(
                position.mfe_price - trail
                if position.direction is Direction.LONG
                else position.mfe_price + trail
            )
        if cfg.break_even_at_r is not None and best_r >= cfg.break_even_at_r:
            candidates.append(position.entry_price)
        if not candidates:
            return

        new_stop = max(candidates) if position.direction is Direction.LONG else min(candidates)
        if position.direction is Direction.LONG and new_stop <= position.stop_price:
            return
        if position.direction is Direction.SHORT and new_stop >= position.stop_price:
            return
        if new_stop <= 0:
            return

        spec = self.cfg.instrument(symbol)
        if self.broker.modify_position(position, stop_price=round(new_stop, spec.digits)):
            log.debug(
                "%s: stop moved to %.5f (best %.5f, %.2fR)",
                symbol,
                new_stop,
                position.mfe_price,
                best_r,
            )

    # -- flatten --------------------------------------------------------------
    def _flatten_symbol(self, symbol: str, when: datetime, reason: ExitReason) -> list[Trade]:
        position = self.broker.position_for(symbol)
        if position is None:
            return []
        try:
            trade = self.broker.close_position(position, reason=reason)
        except BrokerError as exc:
            log.error("%s: could not flatten position — %s", symbol, exc)
            return []
        self._on_trade_closed(trade, self._row_index(symbol, when))
        self._flattened_symbols.add(symbol)
        return [trade]

    def flatten_all(self, reason: ExitReason = ExitReason.MANUAL) -> list[Trade]:
        """Close every open position (shutdown, kill switch, end of run)."""
        closed: list[Trade] = []
        for position in list(self.broker.positions()):
            when = self._last_bar_time.get(position.symbol, position.entry_time)
            closed.extend(self._flatten_symbol(position.symbol, when, reason))
        return closed

    def finish(self, when: datetime | None = None) -> list[Trade]:
        """Flatten and flush at the end of a run."""
        closed = self.flatten_all(ExitReason.END_OF_DATA)
        if self.record_equity and closed:
            self._record_equity(when or closed[-1].exit_time)
        return closed

    def summary(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.name,
            "strategy_params": dict(self.strategy.params),
            "session_filter": self.sessions.describe(),
            "entry_timing": "next bar open" if self.entry_on_next_open else "signal bar close",
            "risk_config": {
                "risk_per_trade_pct": self.cfg.risk.risk_per_trade_pct,
                "max_daily_loss_pct": self.cfg.risk.max_daily_loss_pct,
                "max_drawdown_pct": self.cfg.risk.max_drawdown_pct,
                "trailing_stop": self.cfg.risk.trailing_stop,
                "break_even_at_r": self.cfg.risk.break_even_at_r,
            },
            "risk_state": self.risk.summary(),
            "engine": self.stats.to_dict(),
            "trades": len(self.trades),
        }
