"""MT5 broker tests against a fake terminal.

The real `MetaTrader5` package is Windows-only, so these tests stub it. That is
not a compromise: the risky parts of the MT5 integration are *our* decisions —
quote side, filling mode, retcode handling, position-ticket resolution, and the
real-money refusal — and every one of those is a pure function of what the
terminal returns. Simulating the terminal is how we test them on any OS.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pytest

from scalper.brokers.base import BrokerError
from scalper.brokers.mt5 import MT5Broker
from scalper.config import InstrumentSpec, Mt5BrokerConfig
from scalper.models import Direction, ExitReason, OrderRequest

# -----------------------------------------------------------------------------
# Fake terminal
# -----------------------------------------------------------------------------
ACCOUNT_DEMO = 0
ACCOUNT_REAL = 2


class FakeSymbol:
    def __init__(self, name: str, digits: int = 5, point: float = 1e-5) -> None:
        self.name = name
        self.digits = digits
        self.point = point
        self.trade_tick_size = point
        self.trade_tick_value = 1.0        # 1 USD per point on 1 lot
        self.trade_contract_size = 100_000.0
        self.volume_min = 0.01
        self.volume_step = 0.01
        self.visible = True
        self.filling_mode = 1


class FakeTick:
    def __init__(self, bid: float, ask: float) -> None:
        self.bid = bid
        self.ask = ask


class FakePosition:
    def __init__(self, ticket: int, symbol: str, lots: float, price: float, magic: int, ptype: int = 0) -> None:
        self.ticket = ticket
        self.symbol = symbol
        self.volume = lots
        self.price_open = price
        self.sl = 0.0
        self.tp = 0.0
        self.magic = magic
        self.type = ptype
        self.comment = "fx-scalper"
        self.time = 1704067200


class FakeAccount:
    def __init__(self, trade_mode: int = ACCOUNT_DEMO, balance: float = 5_000.0) -> None:
        self.login = 999
        self.server = "Fake-Demo"
        self.currency = "USD"
        self.leverage = 100
        self.balance = balance
        self.equity = balance
        self.margin_free = balance / 2
        self.trade_mode = trade_mode


class FakeResult:
    def __init__(self, retcode: int = 10009, price: float = 1.10010, order: int = 555, deal: int = 666,
                 comment: str = "Done") -> None:
        self.retcode = retcode
        self.price = price
        self.order = order
        self.deal = deal
        self.comment = comment


class FakeMT5(types.ModuleType):
    """Minimal stand-in for the MetaTrader5 module."""

    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ACCOUNT_TRADE_MODE_DEMO = ACCOUNT_DEMO
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = ACCOUNT_REAL
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, trade_mode: int = ACCOUNT_DEMO) -> None:
        super().__init__("MetaTrader5")
        self.initialized = False
        self.account = FakeAccount(trade_mode)
        self.symbols = {"EURUSD": FakeSymbol("EURUSD")}
        self.tick = FakeTick(1.10000, 1.10010)
        self.sent: list[dict[str, Any]] = []
        self.positions: list[FakePosition] = []
        self.responses: list[FakeResult] = []
        self.sl_tp_calls: list[dict[str, Any]] = []
        self.rates_calls: list[tuple] = []
        self.shutdowns = 0

    # -- lifecycle ---------------------------------------------------------
    def initialize(self, **kwargs: Any) -> bool:
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.shutdowns += 1
        self.initialized = False

    def last_error(self) -> tuple[int, str]:
        return (0, "no error")

    def terminal_info(self) -> Any:
        return types.SimpleNamespace(connected=True)

    def account_info(self) -> Any:
        return self.account

    # -- symbols -----------------------------------------------------------
    def symbol_info(self, symbol: str) -> Any:
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        return self.tick if symbol in self.symbols else None

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return symbol in self.symbols

    # -- trading -----------------------------------------------------------
    def order_send(self, payload: dict[str, Any]) -> Any:
        self.sent.append(dict(payload))
        if self.responses:
            return self.responses.pop(0)
        return FakeResult(price=payload.get("price", 1.10010))

    def positions_get(self, symbol: str | None = None, **kwargs: Any) -> Any:
        if symbol is None:
            return list(self.positions)
        return [p for p in self.positions if p.symbol == symbol]

    def history_deals_get(self, **kwargs: Any) -> Any:
        return []

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int) -> Any:
        self.rates_calls.append((symbol, timeframe, start, count))
        base = 1704067200
        rows = []
        for i in range(count):
            t = base + (start + i) * 60
            rows.append((t, 1.1000, 1.1010, 1.0990, 1.1005, 42, 2, 0))
        return np.array(
            rows,
            dtype=[
                ("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
                ("close", "<f8"), ("tick_volume", "<i8"), ("spread", "<i4"), ("real_volume", "<i8"),
            ],
        )


@pytest.fixture
def fake_mt5(monkeypatch) -> FakeMT5:
    module = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", module)
    return module


@pytest.fixture
def broker(fake_mt5: FakeMT5) -> MT5Broker:
    return MT5Broker(
        Mt5BrokerConfig(magic=777, deviation_points=20, filling_mode="IOC", order_retries=3, retry_sleep_sec=0.0)
    )


def order(direction: Direction = Direction.LONG, lots: float = 0.10) -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        direction=direction,
        lots=lots,
        stop_price=1.09850 if direction is Direction.LONG else 1.10150,
        take_profit_price=1.10200 if direction is Direction.LONG else 1.09800,
        time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        strategy="test",
        comment="test entry",
    )


SPEC = InstrumentSpec("EURUSD", 0.0001, 100_000, 10.0, 1.0, 5)


# -----------------------------------------------------------------------------
# Connection and safety
# -----------------------------------------------------------------------------
def test_connect_reads_the_account(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    assert fake_mt5.initialized
    assert broker.is_demo is True
    assert broker.balance() == pytest.approx(5_000.0)
    assert broker.currency() == "USD"


def test_real_account_is_refused_without_explicit_optin(monkeypatch):
    module = FakeMT5(trade_mode=ACCOUNT_REAL)
    monkeypatch.setitem(sys.modules, "MetaTrader5", module)
    broker = MT5Broker(Mt5BrokerConfig(), allow_live=False)

    with pytest.raises(BrokerError) as exc:
        broker.connect()
    message = str(exc.value)
    assert "REAL-money" in message
    assert "SCALPER_ALLOW_LIVE" in message
    # It must also have hung up rather than left a live connection dangling.
    assert module.shutdowns == 1


def test_real_account_is_allowed_with_the_optin(monkeypatch):
    module = FakeMT5(trade_mode=ACCOUNT_REAL)
    monkeypatch.setitem(sys.modules, "MetaTrader5", module)
    broker = MT5Broker(Mt5BrokerConfig(), allow_live=True)
    broker.connect()
    assert broker.is_demo is False


def test_missing_package_gives_an_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)
    with pytest.raises(Exception) as exc:
        MT5Broker(Mt5BrokerConfig()).connect()
    message = str(exc.value)
    assert "WINDOWS ONLY" in message or "MetaTrader5" in message


def test_unknown_symbol_is_reported_clearly(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    with pytest.raises(BrokerError, match="EURUSDX"):
        broker.ensure_symbol("EURUSDX")


# -----------------------------------------------------------------------------
# Contract specs from the terminal
# -----------------------------------------------------------------------------
def test_spec_is_derived_from_the_terminal_not_the_config(broker: MT5Broker):
    broker.connect()
    spec = broker.spec_for("EURUSD")
    # 5 digits -> 1 point = 1e-5, so a pip is 10 points = 1e-4.
    assert spec.pip_size == pytest.approx(0.0001)
    # tick_value 1.0 USD per point * 10 points per pip = 10 USD per pip per lot.
    assert spec.pip_value_per_lot == pytest.approx(10.0)
    assert spec.contract_size == pytest.approx(100_000.0)
    assert spec.digits == 5
    # Spread comes from the live tick: 1.10010 - 1.10000 = 1 pip.
    assert spec.spread_pips == pytest.approx(1.0)


def test_jpy_style_3_digit_symbols_use_a_10_point_pip(broker: MT5Broker, fake_mt5: FakeMT5):
    fake_mt5.symbols["USDJPY"] = FakeSymbol("USDJPY", digits=3, point=1e-3)
    broker.connect()
    spec = broker.spec_for("USDJPY")
    assert spec.pip_size == pytest.approx(0.01)


def test_gold_style_2_digit_symbols_use_a_1_point_pip(broker: MT5Broker, fake_mt5: FakeMT5):
    fake_mt5.symbols["XAUUSD"] = FakeSymbol("XAUUSD", digits=2, point=0.01)
    broker.connect()
    spec = broker.spec_for("XAUUSD")
    assert spec.pip_size == pytest.approx(0.01)


# -----------------------------------------------------------------------------
# Opening
# -----------------------------------------------------------------------------
def test_long_order_sends_the_ask_and_carries_sl_tp(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    fill = broker.open_position(order(Direction.LONG), SPEC)

    payload = fake_mt5.sent[-1]
    assert payload["type"] == fake_mt5.ORDER_TYPE_BUY
    assert payload["price"] == pytest.approx(1.10010)      # bought the ask
    assert payload["sl"] == pytest.approx(1.09850)
    assert payload["tp"] == pytest.approx(1.10200)
    assert payload["magic"] == 777
    assert payload["deviation"] == 20
    assert fill.price == pytest.approx(1.10010)
    assert fill.direction is Direction.LONG


def test_short_order_sends_the_bid(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    broker.open_position(order(Direction.SHORT), SPEC)

    payload = fake_mt5.sent[-1]
    assert payload["type"] == fake_mt5.ORDER_TYPE_SELL
    assert payload["price"] == pytest.approx(1.10000)      # sold the bid
    assert payload["sl"] == pytest.approx(1.10150)
    assert payload["tp"] == pytest.approx(1.09800)


def test_volume_below_the_broker_minimum_is_refused(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    with pytest.raises(BrokerError, match="minimum"):
        broker.open_position(order(lots=0.001), SPEC)
    assert not fake_mt5.sent  # nothing was ever sent


def test_unsupported_filling_mode_falls_back_and_retries(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    # First attempt: terminal rejects the filling mode. Second: accepted.
    fake_mt5.responses = [FakeResult(retcode=10030, comment="Unsupported filling mode")]
    fill = broker.open_position(order(), SPEC)

    assert len(fake_mt5.sent) == 2
    assert fake_mt5.sent[0]["type_filling"] == fake_mt5.ORDER_FILLING_IOC
    assert fake_mt5.sent[1]["type_filling"] == fake_mt5.ORDER_FILLING_RETURN
    assert fill.price > 0


def test_transient_requote_is_retried(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    fake_mt5.responses = [FakeResult(retcode=10004, comment="Requote"), FakeResult()]
    broker.open_position(order(), SPEC)
    assert len(fake_mt5.sent) == 2


def test_hard_rejection_raises_with_the_terminal_wording(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    fake_mt5.responses = [FakeResult(retcode=10019, comment="No money")]
    with pytest.raises(BrokerError) as exc:
        broker.open_position(order(), SPEC)
    assert "No money" in str(exc.value)
    assert len(fake_mt5.sent) == 1  # a hard rejection is not retried


def test_position_ticket_is_resolved_from_the_terminal(broker: MT5Broker, fake_mt5: FakeMT5):
    """result.order is an order ticket; SLTP/close need the position ticket."""
    broker.connect()
    fake_mt5.responses = [FakeResult(order=555)]
    fake_mt5.positions = [FakePosition(ticket=888, symbol="EURUSD", lots=0.1, price=1.1001, magic=777)]

    fill = broker.open_position(order(), SPEC)
    assert fill.ticket == 888

    position = broker.positions()[0]
    assert position.ticket == 888
    assert position.direction is Direction.LONG


def test_pending_order_is_sent_as_a_pending_action(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    pending = order()
    pending.limit_price = 1.09900
    broker.open_position(pending, SPEC)
    payload = fake_mt5.sent[-1]
    assert payload["action"] == fake_mt5.TRADE_ACTION_PENDING
    assert payload["price"] == pytest.approx(1.09900)


# -----------------------------------------------------------------------------
# Positions, modify, close
# -----------------------------------------------------------------------------
def test_positions_from_other_experts_are_ignored(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    fake_mt5.positions = [
        FakePosition(ticket=1, symbol="EURUSD", lots=0.1, price=1.1, magic=777),
        FakePosition(ticket=2, symbol="EURUSD", lots=0.1, price=1.1, magic=12345),   # someone else's
    ]
    positions = broker.positions()
    assert [p.ticket for p in positions] == [1]


def test_modify_sends_sltp_and_reports_failure(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    fake_mt5.positions = [FakePosition(ticket=888, symbol="EURUSD", lots=0.1, price=1.1001, magic=777)]
    position = broker.positions()[0]

    assert broker.modify_position(position, stop_price=1.09950) is True
    assert fake_mt5.sent[-1]["action"] == fake_mt5.TRADE_ACTION_SLTP
    assert fake_mt5.sent[-1]["sl"] == pytest.approx(1.09950)

    fake_mt5.responses = [FakeResult(retcode=10013, comment="Invalid request")]
    assert broker.modify_position(position, stop_price=1.09960) is False


def test_close_sends_the_opposite_deal_with_the_position_ticket(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    fake_mt5.positions = [FakePosition(ticket=888, symbol="EURUSD", lots=0.1, price=1.1001, magic=777)]
    position = broker.positions()[0]

    fake_mt5.responses = [FakeResult(price=1.10100)]
    trade = broker.close_position(position, reason=ExitReason.TAKE_PROFIT)

    payload = fake_mt5.sent[-1]
    assert payload["type"] == fake_mt5.ORDER_TYPE_SELL     # closing a long means selling
    assert payload["position"] == 888
    assert payload["volume"] == pytest.approx(0.1)
    # 1.10100 - 1.10010 = 9 pips * 10 USD/pip * 0.1 lots = 9 USD
    assert trade.net_pnl == pytest.approx(9.0)
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert broker.closed_trades() == [trade]


def test_closing_a_short_buys_at_the_ask(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    fake_mt5.positions = [
        FakePosition(ticket=889, symbol="EURUSD", lots=0.2, price=1.10000, magic=777, ptype=1)
    ]
    position = broker.positions()[0]
    assert position.direction is Direction.SHORT

    fake_mt5.responses = [FakeResult(price=1.09900)]
    broker.close_position(position)
    payload = fake_mt5.sent[-1]
    assert payload["type"] == fake_mt5.ORDER_TYPE_BUY
    assert payload["price"] == pytest.approx(1.10010)  # buying back means paying the ask


# -----------------------------------------------------------------------------
# Bars
# -----------------------------------------------------------------------------
def test_last_closed_bar_asks_for_position_one_not_zero(broker: MT5Broker, fake_mt5: FakeMT5):
    """Position 0 is the still-forming candle; trading on it is trading blind."""
    broker.connect()
    bar = broker.last_closed_bar("EURUSD", "M1")

    assert bar is not None
    assert fake_mt5.rates_calls[-1][2] == 1   # start index
    assert fake_mt5.rates_calls[-1][3] == 1   # one bar
    assert bar.time.tzinfo is not None
    assert bar.high >= bar.low


def test_history_frame_returns_a_usable_ohlc_frame(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    frame = broker.history_frame("EURUSD", "M1", 50)
    assert len(frame) == 50
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.tz is not None


def test_summary_describes_the_connection(broker: MT5Broker, fake_mt5: FakeMT5):
    broker.connect()
    summary = broker.summary()
    assert summary["is_demo"] is True
    assert summary["magic"] == 777
    assert summary["balance"] == pytest.approx(5_000.0)
