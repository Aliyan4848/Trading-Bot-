"""MetaTrader 5 execution broker.

Wraps the terminal's trade API. Three things it does that a naive wrapper
usually gets wrong:

  * **Contract specs come from the terminal, not the config.** `symbol_info`
    gives the real pip size, tick value and lot limits, so position sizing
    matches the broker's arithmetic instead of a hard-coded guess. Config values
    are only a fallback when the terminal cannot be queried.
  * **Real-vs-demo detection.** The broker refuses to route live orders on a
    real-money account unless explicitly allowed (`allow_live=True` *and*
    `SCALPER_ALLOW_LIVE=yes`). Demo accounts pass through.
  * **Retcode handling.** MT5 returns 200/OK HTTP-style success codes even for
    rejected orders; `retcode` is what matters. Requotes and temporary failures
    are retried, hard rejections raise with the terminal's own wording.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..config import InstrumentSpec, Mt5BrokerConfig
from ..models import Bar, Direction, ExitReason, Fill, OrderRequest, Position, Trade
from .base import Broker, BrokerError

# MT5 trade server return codes we treat as success.
RETCODE_SUCCESS = {10008, 10009, 10010}  # placed, done, partially done
RETCODE_RETRYABLE = {10004, 10021, 10024, 10025, 10031}  # requote, no prices, too many requests...
RETCODE_UNSUPPORTED_FILLING = 10030  # TRADE_RETCODE_INVALID_FILL

FILLING_MODES = {"IOC": "ORDER_FILLING_IOC", "FOK": "ORDER_FILLING_FOK", "RETURN": "ORDER_FILLING_RETURN"}

DEAL_TYPES = {"buy": "DEAL_TYPE_BUY", "sell": "DEAL_TYPE_SELL"}


class MT5Broker(Broker):
    """Order routing through a running MetaTrader 5 terminal."""

    is_live = True
    #: A live venue triggers SL/TP on its own servers, so the engine reconciles
    #: the position list instead of simulating bar-by-bar exits.
    simulates_exits = False

    def __init__(
        self,
        cfg: Mt5BrokerConfig,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        path: str | None = None,
        portable: bool = False,
        allow_live: bool = False,
        magic: int | None = None,
        connected: bool = False,
    ) -> None:
        self.cfg = cfg
        self.magic = magic if magic is not None else cfg.magic
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self.portable = portable
        self.allow_live = allow_live
        self._mt5: Any = None
        self._specs: dict[str, InstrumentSpec] = {}
        self._entry_meta: dict[int, dict[str, Any]] = {}
        self._closed: list[Trade] = []
        self._is_demo: bool | None = None
        self.total_commission = 0.0
        if connected:  # test hook: lets unit tests inject a fake terminal
            self._mt5 = None

    # -- lifecycle ------------------------------------------------------------
    def connect(self) -> None:
        if self._mt5 is not None and self._is_demo is not None:
            return
        from ..data.mt5_feed import import_mt5

        mt5 = import_mt5()
        self._mt5 = None  # don't leave a half-initialised handle behind

        kwargs: dict[str, Any] = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login:
            kwargs.update(login=int(self.login), password=self.password, server=self.server)
        kwargs["portable"] = self.portable

        last_error = ""
        for attempt in range(1, max(1, self.cfg.order_retries) + 1):
            ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
            if ok:
                info = mt5.account_info()
                if info is not None:
                    self._mt5 = mt5
                    self._is_demo = self._detect_demo(mt5, info)
                    if self._is_demo is False and not self.allow_live:
                        self.shutdown()
                        raise BrokerError(
                            "Refusing to trade: this MT5 account is a REAL-money account.\n"
                            "  - Point the terminal at a demo account, or\n"
                            "  - set SCALPER_ALLOW_LIVE=yes in .env AND pass --live on the CLI.\n"
                            "Paper trading (`broker.mode: paper`) is the safe default."
                        )
                    return
                last_error = f"account_info() returned None: {mt5.last_error()}"
            else:
                last_error = f"{mt5.last_error()}"
            if attempt < self.cfg.order_retries:
                time.sleep(self.cfg.retry_sleep_sec)

        raise BrokerError(
            f"Could not connect to MetaTrader 5 after {self.cfg.order_retries} attempt(s): {last_error}\n"
            "Checklist: terminal installed and running, logged in, Algo Trading enabled, "
            "MT5_LOGIN / MT5_PASSWORD / MT5_SERVER correct, and MT5_PATH set if the terminal "
            "is not in the default location."
        )

    def _detect_demo(self, mt5: Any, info: Any) -> bool:
        try:
            mode = getattr(info, "trade_mode", None)
            demo = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
            contest = getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1)
            return mode in (demo, contest)
        except Exception:  # noqa: BLE001 - unknown mode must never imply "demo"
            return False

    def disconnect(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        mt5, self._mt5 = self._mt5, None
        self._is_demo = None
        if mt5 is not None:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                pass

    @property
    def is_demo(self) -> bool:
        return bool(self._is_demo)

    # -- symbols --------------------------------------------------------------
    def ensure_symbol(self, symbol: str) -> None:
        mt5 = self._require()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise BrokerError(
                f"MT5 does not know symbol {symbol!r}. Use the exact Market Watch name — "
                f"brokers often add suffixes (EURUSD.a, EURUSDm, EURUSD-ECN)."
            )
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise BrokerError(f"Could not add {symbol} to Market Watch")

    def spec_for(self, symbol: str, fallback: InstrumentSpec | None = None) -> InstrumentSpec:
        """Build an InstrumentSpec from the terminal's real contract details."""
        if symbol in self._specs:
            return self._specs[symbol]
        mt5 = self._require()
        info = mt5.symbol_info(symbol)
        if info is None:
            if fallback is not None:
                return fallback
            raise BrokerError(f"Cannot read contract specs for {symbol!r}")

        digits = int(getattr(info, "digits", 5))
        point = float(getattr(info, "point", 10 ** -digits))
        tick_size = float(getattr(info, "trade_tick_size", point) or point)
        tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)

        # A "pip" is 10 points on 3/5-digit quotes, 1 point elsewhere.
        pip_size = point * 10 if digits in (3, 5) else point
        if tick_size > 0 and tick_value > 0:
            # Value of a 1-pip move on 1.00 lot, in account currency.
            pip_value_per_lot = tick_value * (pip_size / tick_size)
        elif fallback is not None:
            pip_value_per_lot = fallback.pip_value_per_lot
        else:
            pip_value_per_lot = 10.0

        tick = mt5.symbol_info_tick(symbol)
        spread_pips = fallback.spread_pips if fallback else 1.0
        if tick is not None and pip_size > 0:
            spread_pips = float(tick.ask - tick.bid) / pip_size

        spec = InstrumentSpec(
            symbol=symbol,
            pip_size=pip_size,
            contract_size=float(getattr(info, "trade_contract_size", 100_000.0) or 100_000.0),
            pip_value_per_lot=pip_value_per_lot,
            spread_pips=spread_pips,
            digits=digits,
        )
        self._specs[symbol] = spec
        return spec

    # -- market data ----------------------------------------------------------
    def _require(self) -> Any:
        if self._mt5 is None:
            self.connect()
        return self._mt5

    def price(self, symbol: str) -> tuple[float, float]:
        mt5 = self._require()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerError(f"No tick data for {symbol} (is the market open?)")
        return float(tick.bid), float(tick.ask)

    def spread_pips(self, symbol: str, spec: InstrumentSpec) -> float:
        bid, ask = self.price(symbol)
        return (ask - bid) / spec.pip_size if spec.pip_size else 0.0

    # -- account --------------------------------------------------------------
    def info(self) -> Any:
        mt5 = self._require()
        account = mt5.account_info()
        if account is None:
            raise BrokerError(f"account_info() failed: {mt5.last_error()}")
        return account

    def balance(self) -> float:
        return float(self.info().balance)

    def equity(self) -> float:
        return float(self.info().equity)

    def currency(self) -> str:
        return str(getattr(self.info(), "currency", "USD"))

    def leverage(self) -> int:
        return int(getattr(self.info(), "leverage", 30) or 30)

    # -- positions ------------------------------------------------------------
    def positions(self) -> list[Position]:
        mt5 = self._require()
        raw = mt5.positions_get()
        if raw is None:
            return []
        out: list[Position] = []
        for p in raw:
            if getattr(p, "magic", 0) != self.magic:
                continue  # not ours: never touch manual trades
            symbol = str(p.symbol)
            spec = self.spec_for(symbol)
            direction = Direction.LONG if p.type == getattr(mt5, "POSITION_TYPE_BUY", 0) else Direction.SHORT
            meta = self._entry_meta.get(int(p.ticket), {})
            position = Position(
                symbol=symbol,
                direction=direction,
                lots=float(p.volume),
                entry_price=float(p.price_open),
                entry_time=_to_dt(getattr(p, "time", None)),
                stop_price=float(p.sl) if getattr(p, "sl", 0.0) else 0.0,
                take_profit_price=float(p.tp) if getattr(p, "tp", 0.0) else None,
                pip_size=spec.pip_size,
                pip_value_per_lot=spec.pip_value_per_lot,
                strategy=str(meta.get("strategy", "")),
                risk_amount=float(meta.get("risk_amount", 0.0)),
                comment=str(getattr(p, "comment", "")),
                ticket=int(p.ticket),
                order_id=str(meta.get("order_id", "")),
                # The server-side SL may have been trailed; R must use the
                # stop we originally placed.
                initial_stop_price=float(meta.get("initial_stop", 0.0) or 0.0),
            )
            out.append(position)
        return out

    def position_for(self, symbol: str) -> Position | None:
        for position in self.positions():
            if position.symbol == symbol:
                return position
        return None

    # -- trading --------------------------------------------------------------
    def _filling_mode(self, symbol: str) -> int:
        """The configured filling mode for a symbol.

        Symbols differ in which modes they accept, and the bitmask in
        ``symbol_info.filling_mode`` uses different numbering from the
        ``ORDER_FILLING_*`` order values, so guessing from the mask is
        error-prone. Instead we send the configured mode and let
        ``_send_with_retry`` fall back on retcode 10030 (unsupported filling
        mode) — which is what the terminal tells us authoritatively.
        """
        mt5 = self._require()
        wanted = FILLING_MODES.get(str(self.cfg.filling_mode).upper(), "ORDER_FILLING_IOC")
        return int(getattr(mt5, wanted, getattr(mt5, "ORDER_FILLING_RETURN", 2)))

    def open_position(self, request: OrderRequest, spec: InstrumentSpec) -> Fill:
        mt5 = self._require()
        self.ensure_symbol(request.symbol)
        spec = self.spec_for(request.symbol, fallback=spec)

        min_lot = self._symbol_min_lot(request.symbol)
        if request.lots < min_lot:
            raise BrokerError(
                f"{request.symbol}: volume {request.lots} is below the broker minimum {min_lot}"
            )

        order_type = (
            getattr(mt5, "ORDER_TYPE_BUY", 0)
            if request.direction is Direction.LONG
            else getattr(mt5, "ORDER_TYPE_SELL", 1)
        )
        tick = mt5.symbol_info_tick(request.symbol)
        if tick is None:
            raise BrokerError(f"{request.symbol}: no prices available")
        price = float(tick.ask if request.direction is Direction.LONG else tick.bid)

        payload: dict[str, Any] = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": request.symbol,
            "volume": float(request.lots),
            "type": order_type,
            "price": price,
            "sl": float(request.stop_price),
            "tp": float(request.take_profit_price) if request.take_profit_price else 0.0,
            "deviation": int(self.cfg.deviation_points),
            "magic": self.magic,
            "comment": (request.comment or "fx-scalper")[:31],
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": self._filling_mode(request.symbol),
        }
        if request.limit_price is not None:
            # Pending entry: keep the same stop/target distances.
            payload["action"] = getattr(mt5, "TRADE_ACTION_PENDING", 5)
            payload["price"] = float(request.limit_price)

        result = self._send_with_retry(payload, request.symbol)
        order_ticket = int(getattr(result, "order", 0) or 0)
        deal_ticket = int(getattr(result, "deal", 0) or 0)
        fill_price = float(getattr(result, "price", 0.0) or price)
        if fill_price <= 0:
            fill_price = price

        # `result.order` is the ORDER ticket, but SLTP and close calls need the
        # POSITION ticket, so resolve it from the terminal. Falling back to the
        # order ticket is safe: MT5 uses the opening order's ticket as the
        # position id for market orders.
        ticket = order_ticket
        if request.limit_price is None:
            ticket = self._resolve_position_ticket(request.symbol, order_ticket)
            self._entry_meta[ticket] = {
                "strategy": request.strategy,
                "risk_amount": request.risk_amount,
                "order_id": request.id,
                "spec": spec,
                "initial_stop": request.stop_price,
            }

        return Fill(
            order_id=request.id,
            symbol=request.symbol,
            direction=request.direction,
            lots=request.lots,
            price=fill_price,
            time=datetime.now(timezone.utc),
            stop_price=request.stop_price,
            take_profit_price=request.take_profit_price,
            commission=0.0,  # MT5 reports commission on the deal, not the order
            slippage_pips=abs(fill_price - price) / spec.pip_size if spec.pip_size else 0.0,
            comment=str(getattr(result, "comment", "")) or f"deal {deal_ticket}",
            # The POSITION ticket: this is what later modify/close calls need.
            ticket=ticket,
        )

    def modify_position(
        self,
        position: Position,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> bool:
        mt5 = self._require()
        if position.ticket is None:
            raise BrokerError("Position has no MT5 ticket; cannot modify")
        payload = {
            "action": getattr(mt5, "TRADE_ACTION_SLTP", 6),
            "symbol": position.symbol,
            "position": int(position.ticket),
            "sl": float(stop_price if stop_price is not None else position.stop_price),
            "tp": float(
                take_profit_price
                if take_profit_price is not None
                else (position.take_profit_price or 0.0)
            ),
            "magic": self.magic,
        }
        try:
            result = self._send(payload, position.symbol)
        except BrokerError:
            return False
        return int(getattr(result, "retcode", -1)) in RETCODE_SUCCESS

    def close_position(
        self, position: Position, price: float | None = None, reason: ExitReason = ExitReason.MANUAL
    ) -> Trade:
        mt5 = self._require()
        if position.ticket is None:
            raise BrokerError("Position has no MT5 ticket; cannot close")

        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            raise BrokerError(f"{position.symbol}: no prices available to close")
        closing_long = position.direction is Direction.SHORT
        close_type = (
            getattr(mt5, "ORDER_TYPE_BUY", 0) if closing_long else getattr(mt5, "ORDER_TYPE_SELL", 1)
        )
        fill_reference = float(tick.ask if closing_long else tick.bid)

        payload = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": position.symbol,
            "volume": float(position.lots),
            "type": close_type,
            "position": int(position.ticket),
            "price": fill_reference if price is None else float(price),
            "deviation": int(self.cfg.deviation_points),
            "magic": self.magic,
            "comment": f"close:{reason.value}"[:31],
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": self._filling_mode(position.symbol),
        }
        result = self._send_with_retry(payload, position.symbol)
        exit_price = float(getattr(result, "price", 0.0) or fill_reference)

        meta = self._entry_meta.pop(int(position.ticket), {})
        gross = position.unrealized_pnl(exit_price)
        trade = Trade(
            symbol=position.symbol,
            direction=position.direction,
            lots=position.lots,
            entry_price=position.entry_price,
            entry_time=position.entry_time,
            exit_price=exit_price,
            exit_time=datetime.now(timezone.utc),
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price,
            gross_pnl=gross,
            commission=0.0,
            net_pnl=gross,
            pips=position.pips(exit_price),
            r_multiple=position.r_multiple(exit_price),
            exit_reason=reason,
            strategy=position.strategy or str(meta.get("strategy", "")),
            comment=str(getattr(result, "comment", "")),
            ticket=position.ticket,
        )
        self._closed.append(trade)
        return trade

    def closed_trades(self) -> list[Trade]:
        """Trades this process closed itself.

        MT5 keeps the authoritative history (deals). Use `history_deals()` if you
        need trades that the terminal closed for you — for example a server-side
        stop-loss that filled between two of the bot's polling intervals.
        """
        return list(self._closed)

    def last_closed_bar(self, symbol: str, timeframe: str = "M1") -> Bar | None:
        """The most recently *closed* bar (position 1, never position 0).

        Position 0 is the still-forming candle; trading off it would mean acting
        on a price that has not settled yet.
        """
        mt5 = self._require()
        from ..data.mt5_feed import rates_to_frame, timeframe_constant

        tf = timeframe_constant(mt5, timeframe)
        rates = mt5.copy_rates_from_pos(symbol, tf, 1, 1)
        if rates is None or len(rates) == 0:
            return None
        frame = rates_to_frame(rates, symbol)
        row = frame.iloc[-1]
        return Bar(
            time=frame.index[-1].to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0)),
            symbol=symbol,
        )

    def history_frame(self, symbol: str, timeframe: str = "M1", bars: int = 50_000) -> Any:
        """History used to prime indicators before live bars arrive."""
        mt5 = self._require()
        from ..data.mt5_feed import rates_to_frame, timeframe_constant

        tf = timeframe_constant(mt5, timeframe)
        self.ensure_symbol(symbol)
        rates = mt5.copy_rates_from_pos(symbol, tf, 1, bars)
        return rates_to_frame(rates, symbol)

    def history_deals(self, since: datetime, symbol: str | None = None) -> list[Any]:
        """Raw MT5 deal history (for reconciling stops the server hit)."""
        mt5 = self._require()
        kwargs: dict[str, Any] = {"date_from": since}
        if symbol:
            kwargs["group"] = f"*{symbol}*"
        return list(mt5.history_deals_get(**kwargs) or [])

    # -- helpers --------------------------------------------------------------
    def _resolve_position_ticket(self, symbol: str, fallback: int) -> int:
        """Look up the real position ticket for a symbol we just traded."""
        mt5 = self._require()
        try:
            raw = mt5.positions_get(symbol=symbol) or []
        except Exception:  # noqa: BLE001 - fall back rather than fail a filled order
            return fallback
        for p in raw:
            if getattr(p, "magic", 0) == self.magic:
                return int(getattr(p, "ticket", fallback))
        return fallback

    def _symbol_min_lot(self, symbol: str) -> float:
        mt5 = self._require()
        info = mt5.symbol_info(symbol)
        return float(getattr(info, "volume_min", 0.01) or 0.01)

    def _send_with_retry(self, payload: dict[str, Any], symbol: str) -> Any:
        last: Any = None
        for _attempt in range(1, max(1, self.cfg.order_retries) + 1):
            result = self._send(payload, symbol)
            retcode = int(getattr(result, "retcode", -1))
            if retcode in RETCODE_SUCCESS:
                return result
            last = result
            if retcode == RETCODE_UNSUPPORTED_FILLING:
                # The terminal tells us exactly what it wants; switch and retry.
                payload["type_filling"] = _fallback_filling(self._require())
                continue
            if retcode not in RETCODE_RETRYABLE:
                break
            time.sleep(self.cfg.retry_sleep_sec)
            # Refresh the price before retrying a requote.
            try:
                bid, ask = self.price(symbol)
                payload["price"] = ask if payload.get("type") == 0 else bid
            except BrokerError:
                pass
        raise BrokerError(
            f"Order rejected for {symbol}: retcode={getattr(last, 'retcode', 'n/a')} "
            f"comment={getattr(last, 'comment', '')!r}"
        )

    def _send(self, payload: dict[str, Any], symbol: str) -> Any:
        mt5 = self._require()
        result = mt5.order_send(payload)
        if result is None:
            raise BrokerError(f"order_send returned None for {symbol}: {mt5.last_error()}")
        return result

    # -- reporting ------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        account = self.info()
        return {
            "login": int(getattr(account, "login", 0)),
            "server": str(getattr(account, "server", "")),
            "currency": self.currency(),
            "leverage": self.leverage(),
            "balance": self.balance(),
            "equity": self.equity(),
            "margin_free": float(getattr(account, "margin_free", 0.0)),
            "is_demo": self.is_demo,
            "magic": self.magic,
            "open_positions": len(self.positions()),
        }


def _to_dt(value: Any) -> datetime:
    """MT5 gives epoch seconds; convert to tz-aware UTC."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


def build_mt5_broker(cfg: Any, allow_live: bool = False) -> MT5Broker:
    """Construct an MT5 broker from config + environment credentials."""
    import os

    login = os.environ.get("MT5_LOGIN")
    return MT5Broker(
        cfg.broker.mt5,
        login=int(login) if login else None,
        password=os.environ.get("MT5_PASSWORD"),
        server=os.environ.get("MT5_SERVER"),
        path=os.environ.get("MT5_PATH"),
        allow_live=allow_live,
    )


def _fallback_filling(mt5: Any) -> int:
    """Least-restrictive filling mode; almost every symbol accepts it."""
    return int(getattr(mt5, "ORDER_FILLING_RETURN", 2))


__all__ = ["MT5Broker", "build_mt5_broker", "RETCODE_SUCCESS"]
