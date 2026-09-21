"""Fill mechanics — the numbers a backtest is made of.

Every expected value here is computed by hand from the bars in the test. If one
of these breaks, real money would have been mis-modelled.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scalper.brokers.base import BrokerError
from scalper.brokers.paper import PaperBroker
from scalper.config import InstrumentSpec, PaperBrokerConfig
from scalper.models import Bar, Direction, ExitReason, OrderRequest

T0 = datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc)


def make_broker(
    *,
    balance: float = 10_000.0,
    spread_pips: float = 1.0,
    slippage_pips: float = 0.0,
    commission_per_lot: float = 0.0,
    intrabar_priority: str = "stop",
    leverage: int = 30,
) -> PaperBroker:
    spec = InstrumentSpec("EURUSD", 0.0001, 100_000, 10.0, spread_pips, 5)
    broker = PaperBroker(
        PaperBrokerConfig(
            slippage_pips=slippage_pips,
            commission_per_lot=commission_per_lot,
            stop_out_level_pct=50.0,
        ),
        initial_balance=balance,
        leverage=leverage,
        intrabar_priority=intrabar_priority,
    )
    broker.register_spec(spec)
    broker.connect()
    return broker


def bar(o: float, h: float, low: float, c: float, symbol: str = "EURUSD", minute: int = 0) -> Bar:
    return Bar(
        time=T0.replace(minute=minute),
        open=o,
        high=h,
        low=low,
        close=c,
        symbol=symbol,
    )


def request(direction: Direction, lots: float, stop: float, tp: float | None = None) -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        direction=direction,
        lots=lots,
        stop_price=stop,
        take_profit_price=tp,
        time=T0,
        strategy="test",
    )


# -----------------------------------------------------------------------------
# Spread convention
# -----------------------------------------------------------------------------
def test_long_entry_pays_the_spread_and_exits_at_bid():
    broker = make_broker(spread_pips=1.0)
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))

    fill = broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), broker.spec("EURUSD"))

    # Buy at ask = bid + 1 pip.
    assert fill.price == pytest.approx(1.1001)
    # Marked at the bid, the spread cost is already showing: -1 pip * 10 USD.
    assert broker.equity() == pytest.approx(9_990.0)
    # Selling immediately at the bid loses exactly the spread.
    trade = broker.close_position(broker.position_for("EURUSD"))
    assert trade.gross_pnl == pytest.approx(-10.0)  # -1 pip * 10 USD/pip * 1 lot


def test_short_entry_sells_the_bid_and_exits_at_ask():
    broker = make_broker(spread_pips=1.0)
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))

    fill = broker.open_position(request(Direction.SHORT, 1.0, stop=1.1050), broker.spec("EURUSD"))
    assert fill.price == pytest.approx(1.1000)  # sold at the bid

    trade = broker.close_position(broker.position_for("EURUSD"))
    # Bought back at the ask: -1 pip.
    assert trade.exit_price == pytest.approx(1.1001)
    assert trade.gross_pnl == pytest.approx(-10.0)


def test_round_trip_pays_exactly_one_spread_either_side():
    for direction in (Direction.LONG, Direction.SHORT):
        broker = make_broker(spread_pips=1.0)
        broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
        broker.open_position(request(direction, 0.5, stop=1.0950), broker.spec("EURUSD"))
        trade = broker.close_position(broker.position_for("EURUSD"))
        # 0.5 lots * 1 pip * 10 USD/pip = 5 USD, regardless of direction.
        assert trade.gross_pnl == pytest.approx(-5.0), direction


# -----------------------------------------------------------------------------
# Stop and target triggers
# -----------------------------------------------------------------------------
def test_long_stop_loss_fills_at_the_stop_price():
    broker = make_broker(spread_pips=1.0)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), spec)
    assert broker.position_for("EURUSD").entry_price == pytest.approx(1.1001)

    # Bar dips to the stop exactly.
    trades = broker.on_bar(bar(1.1000, 1.1005, 1.0950, 1.0960, minute=1), spec)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price == pytest.approx(1.0950)
    # (1.0950 - 1.1001) = -5.1 pips * 10 USD/pip * 1 lot = -51 USD
    assert trade.net_pnl == pytest.approx(-510.0)
    # Hitting the stop is exactly -1R by construction: the stop sits 51 pips
    # from the fill, so R measures the trade, not the spread.
    assert trade.r_multiple == pytest.approx(-1.0)


def test_long_stop_not_hit_when_low_stays_above():
    broker = make_broker(spread_pips=1.0)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), spec)

    trades = broker.on_bar(bar(1.1000, 1.1030, 1.0951, 1.1020, minute=1), spec)
    assert trades == []
    assert broker.position_for("EURUSD") is not None


def test_long_take_profit_fills_at_the_target():
    broker = make_broker(spread_pips=1.0)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950, tp=1.1050), spec)

    trades = broker.on_bar(bar(1.1000, 1.1050, 1.1000, 1.1040, minute=1), spec)
    assert len(trades) == 1
    assert trades[0].exit_reason is ExitReason.TAKE_PROFIT
    assert trades[0].exit_price == pytest.approx(1.1050)
    # (1.1050 - 1.1001) = 4.9 pips * 10 USD/pip * 1 lot
    assert trades[0].net_pnl == pytest.approx(490.0)


def test_gap_through_stop_fills_at_the_open_not_the_stop():
    """A price that never traded cannot fill you."""
    broker = make_broker(spread_pips=1.0)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), spec)

    # Bar opens 30 pips below the stop (a weekend gap).
    trades = broker.on_bar(bar(1.0920, 1.0925, 1.0910, 1.0915, minute=1), spec)

    assert len(trades) == 1
    assert trades[0].exit_price == pytest.approx(1.0920)
    # Loss is the gap, not the stop: (1.0920 - 1.1001) = -81 pips.
    assert trades[0].net_pnl == pytest.approx(-810.0)
    assert trades[0].r_multiple == pytest.approx(-1.588, abs=0.01)  # far worse than 1R


def test_short_stop_triggers_on_the_ask_not_the_bid():
    """A short is stopped by the ask, so the bid only needs to reach stop-spread."""
    broker = make_broker(spread_pips=1.0)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.SHORT, 1.0, stop=1.1050), spec)

    # Bid rises to 1.1049: the ask (1.1050) has reached the stop.
    trades = broker.on_bar(bar(1.1000, 1.1049, 1.1000, 1.1045, minute=1), spec)
    assert len(trades) == 1
    assert trades[0].exit_reason is ExitReason.STOP_LOSS
    assert trades[0].exit_price == pytest.approx(1.1050)

    # One pip lower and the ask never reached the stop.
    broker2 = make_broker(spread_pips=1.0)
    broker2.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker2.open_position(request(Direction.SHORT, 1.0, stop=1.1050), spec)
    assert broker2.on_bar(bar(1.1000, 1.1048, 1.1000, 1.1045, minute=1), spec) == []


def test_intrabar_priority_stop_by_default_is_the_pessimistic_one():
    """Both stop and target inside one bar: assume the stop came first."""
    broker = make_broker(spread_pips=1.0, intrabar_priority="stop")
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950, tp=1.1050), spec)

    trades = broker.on_bar(bar(1.1000, 1.1055, 1.0945, 1.1050, minute=1), spec)
    assert trades[0].exit_reason is ExitReason.STOP_LOSS


def test_intrabar_priority_target_when_configured():
    broker = make_broker(spread_pips=1.0, intrabar_priority="target")
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950, tp=1.1050), spec)

    trades = broker.on_bar(bar(1.1000, 1.1055, 1.0945, 1.1050, minute=1), spec)
    assert trades[0].exit_reason is ExitReason.TAKE_PROFIT


def test_slippage_makes_stop_exits_worse():
    broker = make_broker(spread_pips=1.0, slippage_pips=0.5)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    fill = broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), spec)
    assert fill.price == pytest.approx(1.1001 + 0.00005)  # entry also slipped

    trades = broker.on_bar(bar(1.1000, 1.1005, 1.0940, 1.0950, minute=1), spec)
    assert trades[0].exit_price == pytest.approx(1.0950 - 0.00005)


# -----------------------------------------------------------------------------
# Commission / account
# -----------------------------------------------------------------------------
def test_commission_is_charged_round_turn_and_split():
    broker = make_broker(spread_pips=1.0, commission_per_lot=7.0)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    fill = broker.open_position(request(Direction.LONG, 2.0, stop=1.0950), spec)

    # Half up front, half on exit.
    assert fill.commission == pytest.approx(7.0)
    assert broker.balance() == pytest.approx(10_000.0 - 7.0)

    trades = broker.on_bar(bar(1.1000, 1.1000, 1.0950, 1.0950, minute=1), spec)
    trade = trades[0]
    assert trade.commission == pytest.approx(14.0)  # 2 lots * 7 USD round turn
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - 14.0)
    assert broker.balance() == pytest.approx(10_000.0 + trade.net_pnl)


def test_one_position_per_symbol_is_enforced():
    broker = make_broker()
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), spec)
    with pytest.raises(BrokerError, match="already open"):
        broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), spec)


def test_stop_can_only_move_in_the_safe_direction():
    broker = make_broker()
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), spec)
    position = broker.position_for("EURUSD")

    # Widening risk must be refused; tightening is allowed.
    assert broker.modify_position(position, stop_price=1.0900) is False
    assert position.stop_price == pytest.approx(1.0950)
    assert broker.modify_position(position, stop_price=1.1000) is True
    assert position.stop_price == pytest.approx(1.1000)


def test_stop_out_liquidates_when_margin_is_breached():
    """1 lot at 30:1 needs ~3,667 USD margin; 50% stop-out needs equity < 1,833."""
    broker = make_broker(balance=4_000.0, spread_pips=0.0, leverage=30)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0000), spec)

    trades = broker.on_bar(bar(1.1000, 1.1000, 1.0600, 1.0600, minute=1), spec)
    assert len(trades) == 1
    assert trades[0].exit_reason is ExitReason.KILL_SWITCH
    assert broker.stop_out_events


def test_equity_tracks_unrealized_pnl():
    broker = make_broker(spread_pips=0.0)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 1.0, stop=1.0950), spec)

    broker.set_bar(bar(1.1000, 1.1020, 1.1000, 1.1020, minute=1))
    # +20 pips * 10 USD/pip = +200
    assert broker.equity() == pytest.approx(10_200.0)
    assert broker.unrealized_pnl() == pytest.approx(200.0)
    assert broker.balance() == pytest.approx(10_000.0)

    trade = broker.close_position(broker.position_for("EURUSD"))
    assert broker.balance() == pytest.approx(10_200.0)
    assert trade.net_pnl == pytest.approx(200.0)


def test_position_value_math_uses_pip_value_not_contract_size():
    broker = make_broker(spread_pips=0.0)
    spec = broker.spec("EURUSD")
    broker.set_bar(bar(1.1000, 1.1000, 1.1000, 1.1000))
    broker.open_position(request(Direction.LONG, 0.25, stop=1.0950), spec)
    broker.set_bar(bar(1.1000, 1.1040, 1.1000, 1.1040, minute=1))

    # 40 pips * 10 USD/pip * 0.25 lots = 100 USD
    assert broker.unrealized_pnl() == pytest.approx(100.0)
