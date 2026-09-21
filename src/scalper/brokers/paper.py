"""Paper broker: realistic simulated fills on real bars.

Fidelity details that matter for scalping (a strategy can flip from profitable
to unprofitable on these alone):

  * **Quote convention.** OHLC bars are treated as **BID** prices, which is what
    MT5 and most FX exports give you. So:
      - long entry fills at ``bid + spread`` (the ask) and exits at the bid
      - short entry fills at the bid and exits at ``bid + spread`` (the ask)
      - a short's stop/target levels are *ask* levels, so they trigger half a
        spread later than a naive bid-only check would suggest
    Every round trip therefore pays exactly one spread, whichever side it is.
  * **Slippage** on market orders (entries and stop exits), configured
    separately from the spread.
  * **Gaps through the stop.** If a bar opens beyond the stop, the fill is at
    the open — nobody gets filled at a price that never traded.
  * **Intrabar ambiguity.** When a bar's range contains both the stop and the
    target we cannot know which came first, so `intrabar_priority` decides and
    defaults to the pessimistic "stop first".
  * **Commission** per lot, round-turn, split across entry and exit.
  * **Stop-out.** Equity below `stop_out_level_pct` of used margin liquidates
    everything, like a real margin call.
"""

from __future__ import annotations

from datetime import datetime

from ..config import InstrumentSpec, PaperBrokerConfig
from ..models import Bar, Direction, ExitReason, Fill, OrderRequest, Position, Trade
from .base import Broker, BrokerError


class PaperBroker(Broker):
    """In-memory simulated venue.

    Drives two callers with the same `on_bar` step:
      * the backtester, feeding historical bars one at a time
      * live paper trading, feeding bars as they close
    That shared path is what makes a backtest a meaningful rehearsal.
    """

    is_live = False
    simulates_exits = True

    def __init__(
        self,
        cfg: PaperBrokerConfig,
        initial_balance: float = 10_000.0,
        leverage: int = 30,
        currency: str = "USD",
        intrabar_priority: str = "stop",
        spread_overrides: dict[str, float] | None = None,
    ) -> None:
        self.cfg = cfg
        self._balance = float(initial_balance)
        self.initial_balance = float(initial_balance)
        self.leverage = max(int(leverage), 1)
        self.currency = currency
        self.intrabar_priority = intrabar_priority

        self._specs: dict[str, InstrumentSpec] = {}
        self.spreads: dict[str, float] = dict(spread_overrides or {})
        self._positions: dict[str, Position] = {}
        self._last_bars: dict[str, Bar] = {}
        self._entry_commission: dict[str, float] = {}
        self._closed_trades: list[Trade] = []
        self.stop_out_events: list[datetime] = []
        self._connected = False
        self._ticket_seq = 1000

    # -- lifecycle ------------------------------------------------------------
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def register_spec(self, spec: InstrumentSpec) -> None:
        """Tell the broker the contract specs for the symbols it will trade."""
        self._specs[spec.symbol] = spec

    def register_specs(self, specs: list[InstrumentSpec]) -> None:
        for spec in specs:
            self.register_spec(spec)

    def spec(self, symbol: str) -> InstrumentSpec:
        spec = self._specs.get(symbol)
        if spec is None:
            raise BrokerError(
                f"PaperBroker has no contract spec for {symbol}. "
                f"Add it to `instruments:` and pass it to the broker."
            )
        return spec

    # -- market data ----------------------------------------------------------
    def set_bar(self, bar: Bar) -> None:
        self._last_bars[bar.symbol] = bar

    def _bar(self, symbol: str) -> Bar:
        bar = self._last_bars.get(symbol)
        if bar is None:
            raise BrokerError(f"PaperBroker has no market data for {symbol} yet")
        return bar

    def spread_price(self, spec: InstrumentSpec) -> float:
        """Current spread in price terms (config override beats the spec)."""
        return float(self.spreads.get(spec.symbol, spec.spread_pips)) * spec.pip_size

    def price(self, symbol: str) -> tuple[float, float]:
        """(bid, ask) at the latest bar's close."""
        spec = self.spec(symbol)
        bid = self._bar(symbol).close
        return bid, bid + self.spread_price(spec)

    # -- account --------------------------------------------------------------
    def balance(self) -> float:
        return self._balance

    def _mark_price(self, position: Position) -> float:
        """Price the position would exit at right now (long -> bid, short -> ask)."""
        bar = self._bar(position.symbol)
        if position.direction is Direction.LONG:
            return bar.close
        return bar.close + self.spread_price(self.spec(position.symbol))

    def unrealized_pnl(self) -> float:
        return sum(
            pos.unrealized_pnl(self._mark_price(pos))
            for pos in self._positions.values()
            if pos.symbol in self._last_bars
        )

    def equity(self) -> float:
        return self._balance + self.unrealized_pnl()

    def used_margin(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            spec = self._specs.get(pos.symbol)
            contract = spec.contract_size if spec else 100_000.0
            price = self._last_bars[pos.symbol].close if pos.symbol in self._last_bars else pos.entry_price
            total += pos.lots * contract * price / self.leverage
        return total

    def free_margin(self) -> float:
        return self.equity() - self.used_margin()

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def position_for(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    def closed_trades(self) -> list[Trade]:
        return list(self._closed_trades)

    def last_closed_bar(self, symbol: str, timeframe: str = "M1") -> Bar | None:
        """Latest bar the simulation has been given (kept for API parity)."""
        return self._last_bars.get(symbol)

    @property
    def realized_pnl(self) -> float:
        return self._balance - self.initial_balance

    def _next_ticket(self) -> int:
        self._ticket_seq += 1
        return self._ticket_seq

    # -- order entry ----------------------------------------------------------
    def open_position(self, request: OrderRequest, spec: InstrumentSpec) -> Fill:
        if not self._connected:
            raise BrokerError("PaperBroker is not connected")
        if request.symbol in self._positions:
            raise BrokerError(f"{request.symbol}: a position is already open (one per symbol)")
        if request.lots <= 0:
            raise BrokerError(f"{request.symbol}: order volume must be > 0")

        bar = self._bar(request.symbol)
        spread = self.spread_price(spec)
        slip = float(self.cfg.slippage_pips) * spec.pip_size
        if request.reference_price is not None:
            reference = request.reference_price
        elif request.limit_price is not None:
            reference = request.limit_price
        else:
            # No bar context supplied (live paper trading): use the last price.
            reference = bar.close

        if request.direction is Direction.LONG:
            price = reference + spread + slip      # buy the ask, slipped against us
        else:
            price = reference - slip               # sell the bid, slipped against us
        if price <= 0:
            raise BrokerError(f"{request.symbol}: computed entry price {price} is not tradeable")

        half_commission = request.lots * float(self.cfg.commission_per_lot) / 2.0
        self._balance -= half_commission

        ticket = self._next_ticket()
        position = Position(
            symbol=request.symbol,
            direction=request.direction,
            lots=request.lots,
            entry_price=price,
            entry_time=request.time,
            stop_price=request.stop_price,
            take_profit_price=request.take_profit_price,
            pip_size=spec.pip_size,
            pip_value_per_lot=spec.pip_value_per_lot,
            strategy=request.strategy,
            risk_amount=request.risk_amount,
            comment=request.comment,
            ticket=ticket,
            order_id=request.id,
        )
        self._positions[request.symbol] = position
        self._entry_commission[request.symbol] = half_commission

        return Fill(
            order_id=request.id,
            symbol=request.symbol,
            direction=request.direction,
            lots=request.lots,
            price=price,
            time=request.time,
            stop_price=request.stop_price,
            take_profit_price=request.take_profit_price,
            commission=half_commission,
            slippage_pips=slip / spec.pip_size if spec.pip_size else 0.0,
            comment="paper entry",
            ticket=ticket,
        )

    def modify_position(
        self,
        position: Position,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> bool:
        """Move SL/TP. A stop can only ever move in the *safe* direction."""
        live = self._positions.get(position.symbol)
        if live is None:
            return False
        changed = False
        if stop_price is not None:
            if live.direction is Direction.LONG and stop_price > live.stop_price:
                live.stop_price = position.stop_price = stop_price
                changed = True
            elif live.direction is Direction.SHORT and stop_price < live.stop_price:
                live.stop_price = position.stop_price = stop_price
                changed = True
        if take_profit_price is not None and take_profit_price != live.take_profit_price:
            live.take_profit_price = position.take_profit_price = take_profit_price
            changed = True
        return changed

    # -- bar processing -------------------------------------------------------
    def on_bar(self, bar: Bar, spec: InstrumentSpec) -> list[Trade]:
        """Advance the simulation one bar. Returns trades closed on this bar."""
        self.set_bar(bar)
        position = self._positions.get(bar.symbol)
        closed = self._check_exits(position, bar, spec) if position is not None else []
        # A margin stop-out can close other symbols too, so its trades must be
        # reported to the engine -- not just recorded internally.
        closed.extend(self._maybe_stop_out(spec))
        return closed

    def _check_exits(self, position: Position, bar: Bar, spec: InstrumentSpec) -> list[Trade]:
        stop_hit, stop_fill = self._stop_trigger(position, bar, spec)
        tp_hit, tp_fill = self._target_trigger(position, bar, spec)

        if stop_hit and tp_hit:
            if self.intrabar_priority == "target":
                return [self._close(position, tp_fill, bar.time, ExitReason.TAKE_PROFIT, spec)]
            return [self._close(position, stop_fill, bar.time, ExitReason.STOP_LOSS, spec)]
        if stop_hit:
            return [self._close(position, stop_fill, bar.time, ExitReason.STOP_LOSS, spec)]
        if tp_hit:
            return [self._close(position, tp_fill, bar.time, ExitReason.TAKE_PROFIT, spec)]
        return []

    def _stop_trigger(self, position: Position, bar: Bar, spec: InstrumentSpec) -> tuple[bool, float]:
        """Whether the stop fired on this bar, and the fill price (exit side)."""
        stop = position.stop_price
        slip = float(self.cfg.slippage_pips) * position.pip_size

        if position.direction is Direction.LONG:
            # Long exits at the bid: a gap-down open fills at the open.
            if bar.open <= stop:
                return True, bar.open
            if bar.low <= stop:
                return True, stop - slip
            return False, stop

        # Short exits at the ask, so the trigger happens a spread earlier in bid
        # terms: ask = bid + spread >= stop  <=>  bid >= stop - spread.
        spread = self.spread_price(spec)
        bid_trigger = stop - spread
        if bar.open >= bid_trigger:
            return True, max(bar.open + spread, stop)
        if bar.high >= bid_trigger:
            return True, stop + slip
        return False, stop

    def _target_trigger(self, position: Position, bar: Bar, spec: InstrumentSpec) -> tuple[bool, float]:
        tp = position.take_profit_price
        if tp is None:
            return False, 0.0

        if position.direction is Direction.LONG:
            # Long exits at the bid.
            if bar.open >= tp:
                return True, bar.open
            if bar.high >= tp:
                return True, tp
            return False, tp

        # Short exits at the ask: ask <= tp  <=>  bid <= tp - spread.
        spread = self.spread_price(spec)
        bid_trigger = tp - spread
        if bar.open <= bid_trigger:
            return True, min(bar.open + spread, tp)
        if bar.low <= bid_trigger:
            return True, tp
        return False, tp

    def _close(
        self,
        position: Position,
        exit_price: float,
        when: datetime,
        reason: ExitReason,
        spec: InstrumentSpec,
        bars_held: int = 0,
    ) -> Trade:
        gross = position.unrealized_pnl(exit_price)
        exit_half_commission = position.lots * float(self.cfg.commission_per_lot) / 2.0
        entry_half_commission = self._entry_commission.pop(position.symbol, 0.0)
        commission = entry_half_commission + exit_half_commission

        self._balance += gross - exit_half_commission

        trade = Trade(
            symbol=position.symbol,
            direction=position.direction,
            lots=position.lots,
            entry_price=position.entry_price,
            entry_time=position.entry_time,
            exit_price=exit_price,
            exit_time=when,
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price,
            gross_pnl=gross,
            commission=commission,
            net_pnl=gross - commission,
            pips=position.pips(exit_price),
            r_multiple=position.r_multiple(exit_price),
            exit_reason=reason,
            strategy=position.strategy,
            comment=position.comment,
            ticket=position.ticket,
            bars_held=bars_held,
        )
        self._positions.pop(position.symbol, None)
        self._closed_trades.append(trade)
        return trade

    def close_position(
        self, position: Position, price: float | None = None, reason: ExitReason = ExitReason.MANUAL
    ) -> Trade:
        """Market-close a position (session exit, kill switch, shutdown)."""
        live = self._positions.get(position.symbol)
        if live is None:
            raise BrokerError(f"No open position for {position.symbol}")
        spec = self.spec(position.symbol)

        if price is None:
            bar = self._bar(position.symbol)
            slip = float(self.cfg.slippage_pips) * spec.pip_size
            if live.direction is Direction.LONG:
                price = bar.close - slip                     # sell the bid
            else:
                price = bar.close + self.spread_price(spec) + slip  # buy the ask
        return self._close(live, price, self._bar(position.symbol).time, reason, spec)

    # -- margin call ----------------------------------------------------------
    def _maybe_stop_out(self, spec: InstrumentSpec) -> list[Trade]:
        """Liquidate everything if equity falls below the stop-out level."""
        closed: list[Trade] = []
        level = float(self.cfg.stop_out_level_pct) / 100.0
        if level <= 0 or not self._positions:
            return closed
        margin = self.used_margin()
        if margin <= 0 or self.equity() >= margin * level:
            return closed

        self.stop_out_events.append(self._last_bars[next(iter(self._positions))].time)
        for position in list(self._positions.values()):
            try:
                closed.append(self.close_position(position, reason=ExitReason.KILL_SWITCH))
            except BrokerError:
                continue
        return closed

    # -- reporting ------------------------------------------------------------
    def stats(self) -> dict[str, float]:
        return {
            "initial_balance": self.initial_balance,
            "balance": round(self._balance, 2),
            "equity": round(self.equity(), 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "open_positions": len(self._positions),
            "used_margin": round(self.used_margin(), 2),
            "free_margin": round(self.free_margin(), 2),
            "closed_trades": len(self._closed_trades),
            "stop_outs": len(self.stop_out_events),
        }
