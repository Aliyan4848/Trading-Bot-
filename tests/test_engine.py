"""Engine behaviour: entry timing, risk gates, position management.

The timing tests are the most important in the repository. If a signal on bar
`t` can be filled at bar `t`'s own price, every backtest in the project becomes
fiction, so these assert the exact fill price on the exact bar.
"""

from __future__ import annotations

import pandas as pd
import pytest
from conftest import make_frame

from scalper.brokers.paper import PaperBroker
from scalper.config import (
    AppConfig,
    BacktestConfig,
    InstrumentSpec,
    PaperBrokerConfig,
    RiskConfig,
    SessionConfig,
    SessionWindow,
    StrategyConfig,
)
from scalper.engine import Engine
from scalper.models import Bar, ExitReason
from scalper.risk import RiskManager, build_risk_manager
from scalper.strategies.base import PreparedSignals, Strategy


# -----------------------------------------------------------------------------
# A strategy that emits signals exactly where a test tells it to.
# -----------------------------------------------------------------------------
class ScriptedStrategy(Strategy):
    name = "scripted"

    def __init__(self, signals: dict[int, int], stop_pips: float = 10.0, tp_pips: float = 20.0) -> None:
        super().__init__(symbol="EURUSD", stop_pips=stop_pips, tp_pips=tp_pips)
        self.scripted = signals

    @classmethod
    def default_params(cls) -> dict:
        return {"stop_pips": 10.0, "tp_pips": 20.0}

    @property
    def min_bars(self) -> int:  # type: ignore[override]
        return 1

    def prepare(self, df: pd.DataFrame) -> PreparedSignals:
        signal = pd.Series(0, index=df.index, dtype="int8")
        for index, value in self.scripted.items():
            if 0 <= index < len(df):
                signal.iloc[index] = value
        stop = pd.Series(float(self.params["stop_pips"]), index=df.index)
        tp = pd.Series(float(self.params["tp_pips"]), index=df.index)
        reason = pd.Series("", index=df.index, dtype=object)
        return self._finalize(df, signal, stop, tp, reason)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
SPEC = InstrumentSpec("EURUSD", 0.0001, 100_000, 10.0, spread_pips=1.0, digits=5)


def make_config(**overrides) -> AppConfig:
    """A minimal config with sessions disabled, so timing tests are pure."""
    cfg = AppConfig(
        account=__import__("scalper.config", fromlist=["AccountConfig"]).AccountConfig(
            initial_balance=10_000.0, leverage=30, currency="USD"
        ),
        instruments=[SPEC],
        risk=RiskConfig(
            risk_per_trade_pct=0.5,
            max_daily_loss_pct=3.0,
            max_drawdown_pct=50.0,
            max_concurrent_positions=1,
            max_trades_per_day=100,
            min_lot=0.01,
            max_lot=100.0,
            lot_step=0.01,
            max_spread_pips=10.0,
        ),
        session=SessionConfig(enabled=False),
        backtest=BacktestConfig(warmup_bars=1, entry_on_next_open=True),
        strategy=StrategyConfig(name="scripted"),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def make_broker(cfg: AppConfig, **kwargs) -> PaperBroker:
    broker = PaperBroker(
        PaperBrokerConfig(slippage_pips=0.0, commission_per_lot=0.0, stop_out_level_pct=50.0),
        initial_balance=cfg.account.initial_balance,
        leverage=cfg.account.leverage,
        intrabar_priority="stop",
        **kwargs,
    )
    broker.register_spec(SPEC)
    broker.connect()
    return broker


def run_engine(cfg: AppConfig, frame: pd.DataFrame, strategy: Strategy, broker: PaperBroker | None = None):
    broker = broker or make_broker(cfg)
    risk = build_risk_manager(cfg)
    engine = Engine(cfg, broker, strategy, risk)
    engine.prepare_symbol("EURUSD", frame, SPEC)
    times = frame.index.to_pydatetime()
    for i in range(len(frame)):
        row = frame.iloc[i]
        engine.on_bar(
            "EURUSD",
            Bar(time=times[i], open=row.open, high=row.high, low=row.low, close=row.close,
                volume=row.volume, symbol="EURUSD"),
            i,
        )
    return engine, broker


# -----------------------------------------------------------------------------
# Entry timing (the lookahead tests)
# -----------------------------------------------------------------------------
def test_signal_on_bar_t_fills_at_bar_t_plus_1_open():
    """The single most important property of this codebase."""
    # Bar 3 closes at 1.1000 and signals. Bar 4 opens at 1.1050 — a price that
    # bar 3's information could not have known.
    frame = make_frame(
        [1.1000, 1.1000, 1.1000, 1.1000, 1.1050],
        opens=[1.1000, 1.1000, 1.1000, 1.1000, 1.1050],
        highs=[1.1005] * 5,
        lows=[1.0995] * 5,
    )
    cfg = make_config()
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({3: 1}))

    assert len(engine.trades) <= 1
    position = broker.position_for("EURUSD") or engine.trades[0]
    # Filled at bar 4's OPEN (1.1050) plus the 1 pip spread — never at 1.1000.
    assert position.entry_price == pytest.approx(1.1051)
    assert position.entry_time == frame.index[4].to_pydatetime()


def test_signal_bar_itself_is_never_traded():
    """A signal on the final bar cannot be filled — there is no next bar."""
    frame = make_frame([1.1000] * 6)
    cfg = make_config()
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({5: 1}))
    assert broker.position_for("EURUSD") is None
    assert engine.stats.entries_filled == 0


def test_entry_on_signal_bar_close_when_configured():
    frame = make_frame([1.1000, 1.1000, 1.1000, 1.1010], opens=[1.1] * 4, highs=[1.1015] * 4, lows=[1.0995] * 4)
    cfg = make_config(backtest=BacktestConfig(warmup_bars=1, entry_on_next_open=False))
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({2: 1}))

    position = broker.position_for("EURUSD") or engine.trades[0]
    # Fill at bar 2's close (1.1000) + spread, not the next open.
    assert position.entry_price == pytest.approx(1.1001)


def test_stop_and_target_distances_are_measured_from_the_signal_open():
    frame = make_frame([1.1000] * 6)
    cfg = make_config()
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({2: 1}, stop_pips=10.0, tp_pips=20.0))

    position = broker.position_for("EURUSD") or engine.trades[0]
    # Reference is bar 3's open = 1.1000 -> stop 10 pips below, target 20 above.
    assert position.stop_price == pytest.approx(1.0990)
    assert position.take_profit_price == pytest.approx(1.1020)


def test_a_position_can_be_stopped_out_on_its_own_entry_bar():
    """Entering at the open does not exempt you from the rest of that bar."""
    frame = make_frame(
        [1.1000] * 5,
        opens=[1.1000, 1.1000, 1.1000, 1.1000, 1.1000],
        highs=[1.1005, 1.1005, 1.1005, 1.1005, 1.1005],
        lows=[1.0995, 1.0995, 1.0995, 1.0995, 1.0950],  # bar 4 collapses 50 pips
    )
    cfg = make_config()
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({3: 1}, stop_pips=10.0))

    assert len(engine.trades) == 1
    trade = engine.trades[0]
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.bars_held == 0
    assert trade.exit_time == frame.index[4].to_pydatetime()


# -----------------------------------------------------------------------------
# Sizing and risk gates
# -----------------------------------------------------------------------------
def test_position_size_respects_the_risk_budget():
    """0.5% of 10,000 = 50 USD risk; a 10 pip stop at 10 USD/pip = 1.0 lot... """
    cfg = make_config()
    frame = make_frame([1.1000] * 6)
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({2: 1}, stop_pips=10.0))

    position = broker.position_for("EURUSD") or engine.trades[0]
    # risk_amount / (stop_pips * pip_value) = 50 / (10 * 10) = 0.5 lots
    assert position.lots == pytest.approx(0.50)
    # The stop is 10 pips below the SIGNAL reference (1.1000) but the fill is at
    # the ask (1.1001), so the true risk is 11 pips = 55 USD, not 50. Sizing on
    # the signal distance and stopping from the fill is what every retail bot
    # does; the extra pip is the spread, and it is why a strategy needs an edge
    # bigger than one spread to survive.
    assert position.initial_risk_money == pytest.approx(55.0)
    assert position.initial_risk_money > 10_000 * 0.005


def test_lot_size_rounds_down_never_up():
    rm = RiskManager(RiskConfig(lot_step=0.01, min_lot=0.01, max_lot=100.0))
    assert rm.round_lots(0.719) == pytest.approx(0.71)
    assert rm.round_lots(0.001) == pytest.approx(0.0)
    assert rm.round_lots(5.0) == pytest.approx(5.0)


def test_sizing_refuses_when_the_stop_is_too_wide_for_the_budget():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=0.1, min_lot=0.01, lot_step=0.01, max_lot=100.0))
    # 0.1% of 10,000 = 10 USD risk; a 500 pip stop needs ~0.002 lots -> below min.
    decision = rm.size(equity=10_000.0, spec=SPEC, stop_pips=500.0)
    assert decision.allowed is False
    assert "broker minimum" in decision.reason


def test_daily_loss_limit_blocks_further_entries():
    cfg = make_config(
        risk=RiskConfig(
            risk_per_trade_pct=0.5,
            max_daily_loss_pct=1.0,      # tighten so a couple of losses trip it
            max_drawdown_pct=90.0,
            max_concurrent_positions=1,
            max_trades_per_day=100,
            min_lot=0.01,
            max_lot=100.0,
            lot_step=0.01,
            max_spread_pips=10.0,
        )
    )
    # Two losing trades of ~1% each on the same day, then a third signal.
    prices = [1.1000] * 40
    frame = make_frame(
        prices,
        opens=[1.1000] * 40,
        highs=[1.1005] * 40,
        lows=[1.0980] * 40,   # every entry gets stopped immediately
    )
    strategy = ScriptedStrategy({2: 1, 6: 1, 10: 1, 14: 1}, stop_pips=10.0, tp_pips=20.0)
    engine, broker = run_engine(cfg, frame, strategy)

    assert engine.stats.entries_blocked > 0
    assert any("daily loss" in reason for reason in engine.stats.block_reasons)
    assert not engine.risk.state.halted  # a daily pause, not a kill switch


def test_max_drawdown_kill_switch_halts_trading_for_good():
    cfg = make_config(
        risk=RiskConfig(
            risk_per_trade_pct=2.0,
            max_daily_loss_pct=50.0,
            max_drawdown_pct=3.0,     # trip quickly
            max_concurrent_positions=1,
            max_trades_per_day=100,
            min_lot=0.01,
            max_lot=100.0,
            lot_step=0.01,
            max_spread_pips=10.0,
        )
    )
    frame = make_frame(
        [1.1000] * 60,
        opens=[1.1000] * 60,
        highs=[1.1005] * 60,
        lows=[1.0900] * 60,
    )
    strategy = ScriptedStrategy({i: 1 for i in range(2, 50, 4)}, stop_pips=10.0, tp_pips=20.0)
    engine, broker = run_engine(cfg, frame, strategy)

    assert engine.risk.state.halted
    assert "drawdown" in engine.risk.state.halt_reason
    assert len(engine.trades) < len(strategy.scripted)  # stopped early


def test_max_trades_per_day_is_enforced():
    cfg = make_config(
        risk=RiskConfig(
            risk_per_trade_pct=0.1, max_daily_loss_pct=90.0, max_drawdown_pct=90.0,
            max_concurrent_positions=1, max_trades_per_day=2, min_lot=0.01,
            max_lot=100.0, lot_step=0.01, max_spread_pips=10.0,
        )
    )
    # A wide bar range stops every entry out on its own bar (intrabar priority
    # is "stop"), which frees the slot for the next signal.
    frame = make_frame(
        [1.1000] * 40, opens=[1.1000] * 40, highs=[1.1010] * 40, lows=[1.0990] * 40
    )
    strategy = ScriptedStrategy({2: 1, 6: 1, 10: 1}, stop_pips=5.0, tp_pips=5.0)
    engine, broker = run_engine(cfg, frame, strategy)

    assert engine.stats.entries_filled == 2
    assert engine.risk.state.trades_today == 2
    assert any("max trades/day" in reason for reason in engine.stats.block_reasons)


def test_no_pyramiding_on_one_symbol():
    frame = make_frame([1.1000] * 30, opens=[1.1000] * 30, highs=[1.1002] * 30, lows=[1.0998] * 30)
    cfg = make_config()
    strategy = ScriptedStrategy({i: 1 for i in range(2, 25)}, stop_pips=50.0, tp_pips=50.0)
    engine, broker = run_engine(cfg, frame, strategy)

    assert len(broker.positions()) == 1
    assert any("already open" in reason for reason in engine.stats.block_reasons)


def test_spread_filter_blocks_entry_when_too_wide():
    cfg = make_config(
        risk=RiskConfig(
            risk_per_trade_pct=0.5, max_daily_loss_pct=50.0, max_drawdown_pct=50.0,
            max_concurrent_positions=1, max_trades_per_day=100, min_lot=0.01,
            max_lot=100.0, lot_step=0.01, max_spread_pips=0.5,  # tighter than the 1.0 pip market
        )
    )
    frame = make_frame([1.1000] * 10)
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({2: 1}))
    assert engine.stats.entries_filled == 0
    assert any("spread" in reason for reason in engine.stats.block_reasons)


# -----------------------------------------------------------------------------
# Sessions
# -----------------------------------------------------------------------------
def test_session_window_blocks_entries_outside_hours():
    cfg = make_config(
        session=SessionConfig(
            enabled=True,
            timezone="UTC",
            windows=[SessionWindow(name="London", start="08:00", end="12:00")],
            skip_weekend=False,
            flat_at_close=False,
        )
    )
    # Signal on bar 1 (07:58); the entry bar would be 07:59, before the 08:00 open.
    index = pd.date_range("2024-01-02 07:57", periods=6, freq="1min", tz="UTC", name="time")
    frame = make_frame([1.1000] * 6)
    frame.index = index
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({1: 1}))
    assert engine.stats.entries_filled == 0
    assert broker.position_for("EURUSD") is None

    # The same signal just after the open does trade (entry bar 09:02).
    index2 = pd.date_range("2024-01-02 09:00", periods=6, freq="1min", tz="UTC", name="time")
    frame2 = make_frame([1.1000] * 6)
    frame2.index = index2
    engine2, broker2 = run_engine(cfg, frame2, ScriptedStrategy({1: 1}))
    assert engine2.stats.entries_filled == 1


def test_weekend_is_never_traded():
    cfg = make_config(session=SessionConfig(enabled=True, windows=[], skip_weekend=True))
    saturday = pd.date_range("2024-01-06 10:00", periods=6, freq="1min", tz="UTC", name="time")
    frame = make_frame([1.1000] * 6)
    frame.index = saturday
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({2: 1}))
    assert engine.stats.entries_filled == 0


def test_positions_are_flattened_at_session_close():
    cfg = make_config(
        session=SessionConfig(
            enabled=True,
            timezone="UTC",
            windows=[SessionWindow(name="London", start="08:00", end="12:00")],
            skip_weekend=False,
            flat_at_close=True,
        )
    )
    index = pd.date_range("2024-01-02 11:56", periods=6, freq="1min", tz="UTC", name="time")
    frame = make_frame([1.1000] * 6)
    frame.index = index
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({1: 1}, stop_pips=100.0, tp_pips=100.0))

    assert broker.position_for("EURUSD") is None
    assert any(t.exit_reason is ExitReason.SESSION_END for t in engine.trades)


# -----------------------------------------------------------------------------
# Position management
# -----------------------------------------------------------------------------
def test_trailing_stop_locks_in_profit():
    """1R up, then a 0.5R trail: the trade must exit in profit, not at the stop."""
    cfg = make_config()
    cfg.risk = RiskConfig(
        risk_per_trade_pct=0.5, max_daily_loss_pct=50.0, max_drawdown_pct=90.0,
        max_concurrent_positions=1, max_trades_per_day=100, min_lot=0.01,
        max_lot=100.0, lot_step=0.01, max_spread_pips=10.0,
        trailing_stop=True, trailing_start_r=1.0, trailing_distance_r=0.5,
    )
    # Bar 2 signals -> entry at bar 3's open (1.1000) + 1 pip spread = 1.1001.
    # Stop was set 10 pips below the reference -> 1.0990, so 1R = 11 pips here.
    # Bar 3 runs to 1.1020 (1.7R) -> trail = 1.1020 - 0.5R (5.5 pips) = 1.10145.
    # Bar 4 dips through that level, so the trade exits up +1.2R instead of -1R.
    frame = make_frame(
        [1.1000, 1.1000, 1.1000, 1.1020, 1.1005],
        opens=[1.1000, 1.1000, 1.1000, 1.1000, 1.1020],
        highs=[1.1000, 1.1000, 1.1000, 1.1020, 1.1020],
        lows=[1.1000, 1.1000, 1.1000, 1.1000, 1.1000],
    )
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({2: 1}, stop_pips=10.0, tp_pips=100.0))

    assert len(engine.trades) == 1
    trade = engine.trades[0]
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price == pytest.approx(1.10145)
    assert trade.exit_price > trade.entry_price
    assert trade.r_multiple == pytest.approx(1.227, abs=0.01)


def test_break_even_moves_the_stop_to_entry():
    cfg = make_config()
    cfg.risk = RiskConfig(
        risk_per_trade_pct=0.5, max_daily_loss_pct=50.0, max_drawdown_pct=90.0,
        max_concurrent_positions=1, max_trades_per_day=100, min_lot=0.01,
        max_lot=100.0, lot_step=0.01, max_spread_pips=10.0,
        trailing_stop=False, break_even_at_r=1.0,
    )
    # Bar 3 reaches 1.9R -> stop moves to the entry price (1.1001). Bar 4 falls
    # back through it, so the trade scratches instead of losing a full R.
    frame = make_frame(
        [1.1000, 1.1000, 1.1000, 1.1020, 1.1000],
        opens=[1.1000, 1.1000, 1.1000, 1.1000, 1.1020],
        highs=[1.1000, 1.1000, 1.1000, 1.1020, 1.1020],
        lows=[1.1000, 1.1000, 1.1000, 1.1000, 1.1000],
    )
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({2: 1}, stop_pips=10.0, tp_pips=100.0))

    assert len(engine.trades) == 1
    trade = engine.trades[0]
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price == pytest.approx(1.1001)
    assert trade.net_pnl == pytest.approx(0.0, abs=0.5)


def test_equity_curve_has_one_point_per_bar():
    frame = make_frame([1.1000] * 12)
    cfg = make_config()
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({}))
    assert len(engine.equity_curve) == len(frame)
    assert all(s.equity == pytest.approx(10_000.0) for s in engine.equity_curve)


def test_finish_always_records_the_closing_account_state():
    """`len(equity_curve) == bars_processed + 1` must not depend on luck.

    Recording the final snapshot only when a position happened to be open makes
    the last point of the curve an accident of the data: "final equity" would be
    whatever the last bar with a position showed. Nothing open here, so this is
    the case that used to skip it.
    """
    frame = make_frame([1.1000] * 12)
    cfg = make_config()
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({}))

    assert engine.stats.bars_processed == 12
    assert len(engine.equity_curve) == 12

    closed = engine.finish(frame.index[-1].to_pydatetime())
    assert closed == []
    assert len(engine.equity_curve) == 13
    assert engine.equity_curve[-1].equity == pytest.approx(broker.equity())


def test_finish_flattens_open_positions():
    frame = make_frame([1.1000] * 10)
    cfg = make_config()
    broker = make_broker(cfg)
    risk = build_risk_manager(cfg)
    engine = Engine(cfg, broker, ScriptedStrategy({2: 1}, stop_pips=100.0, tp_pips=100.0), risk)
    engine.prepare_symbol("EURUSD", frame, SPEC)
    times = frame.index.to_pydatetime()
    for i in range(len(frame)):
        row = frame.iloc[i]
        engine.on_bar("EURUSD", Bar(times[i], row.open, row.high, row.low, row.close, row.volume, "EURUSD"), i)

    assert broker.position_for("EURUSD") is not None
    engine.finish(times[-1])
    assert broker.position_for("EURUSD") is None
    assert engine.trades[-1].exit_reason is ExitReason.END_OF_DATA


def test_engine_reports_why_entries_were_blocked():
    frame = make_frame([1.1000] * 20)
    cfg = make_config()
    engine, broker = run_engine(cfg, frame, ScriptedStrategy({i: 1 for i in range(2, 20)}, stop_pips=50.0, tp_pips=50.0))
    summary = engine.summary()
    assert summary["engine"]["entries_blocked"] > 0
    assert summary["engine"]["block_reasons"]
    assert summary["entry_timing"] == "next bar open"
