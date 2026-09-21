"""Report generation and the live/replay loop."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from scalper.backtest import run_backtest
from scalper.brokers.paper import PaperBroker
from scalper.config import load_config
from scalper.data import build_feed
from scalper.live import LiveTrader
from scalper.report import (
    build_html,
    build_markdown,
    svg_drawdown_chart,
    svg_equity_chart,
    write_all_reports,
    write_equity_csv,
    write_portfolio_report,
    write_trades_csv,
)
from scalper.risk import build_risk_manager
from scalper.strategies import get_strategy


@pytest.fixture(scope="module")
def small_result(tmp_path_factory):
    """A real (small) backtest to report on."""
    cfg = load_config("config/config.yaml", {"data.synthetic.bars": 30_000})
    cfg.reporting.output_dir = str(tmp_path_factory.mktemp("results"))
    frames = build_feed(cfg).load()
    from scalper.brokers import build_broker

    broker = build_broker(cfg)
    strategy = get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params)
    return cfg, run_backtest(cfg, broker, strategy, "EURUSD", frames["EURUSD"])


# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------
def test_report_files_are_written(small_result):
    cfg, result = small_result
    paths = write_all_reports(result, cfg, synthetic=True)

    assert {"json", "markdown", "html", "equity_csv", "trades_csv"} <= set(paths)
    for path in paths.values():
        assert Path(path).exists()
        assert Path(path).stat().st_size > 0


def test_json_report_is_valid_and_complete(small_result):
    cfg, result = small_result
    payload = json.loads(Path(write_all_reports(result, cfg)["json"]).read_text())

    assert payload["symbol"] == "EURUSD"
    assert payload["strategy"] == result.strategy
    assert payload["period"]["bars"] == result.bars
    assert "metrics" in payload and "engine" in payload
    assert payload["account"]["initial_balance"] == result.initial_balance
    # NaN/inf must never leak into JSON.
    text = json.dumps(payload)
    assert "NaN" not in text and "Infinity" not in text


def test_markdown_report_has_the_headline_numbers(small_result):
    cfg, result = small_result
    md = build_markdown(result, synthetic=False)
    m = result.metrics

    assert f"{m.net_profit:+,.2f}" in md
    assert "Max drawdown" in md
    assert "Profit factor" in md
    assert "## Trades" in md
    # The synthetic banner lives in `result.data_warnings`, so no caller can
    # produce a report that looks like real evidence by passing synthetic=False.
    assert "SYNTHETIC" in md
    assert build_markdown(result, synthetic=True).count("SYNTHETIC") == md.count("SYNTHETIC")

    from scalper.report import report_warnings

    clean = copy.copy(result)
    clean.data_warnings = []
    assert "SYNTHETIC" not in build_markdown(clean)
    assert "synthetic" in report_warnings(clean, synthetic=True)[0].lower()


def test_html_report_is_self_contained(small_result):
    cfg, result = small_result
    html = build_html(result, synthetic=True)

    assert html.startswith("<!DOCTYPE html>")
    assert "<svg" in html                      # charts are inline, no CDN
    assert "http://" not in html.replace("http://www.w3.org", "")  # no external fetches
    assert "Synthetic data" in html
    assert "fx-scalper" in html


def test_equity_csv_downsampling_keeps_the_final_point(tmp_path):
    """A downsampled equity file must still end on the run's closing equity."""
    from datetime import datetime, timedelta, timezone

    from scalper.models import AccountSnapshot

    T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    curve = [
        AccountSnapshot(T0 + timedelta(minutes=i), 10_000, 10_000 + i) for i in range(101)
    ]
    path = write_equity_csv(curve, tmp_path / "eq.csv", max_rows=10)
    frame = pd.read_csv(path, parse_dates=["time"])
    assert len(frame) <= 11
    assert frame["time"].iloc[-1] == T0 + timedelta(minutes=100)
    assert float(frame["equity"].iloc[-1]) == 10_100.0


def test_svg_charts_handle_degenerate_data():
    from datetime import datetime, timezone

    import numpy as np

    flat = np.full(50, 10_000.0)
    times = [datetime(2024, 1, 1, tzinfo=timezone.utc)] * 50
    svg = svg_equity_chart(flat, times)
    assert "<svg" in svg
    assert "nan" not in svg.lower()
    assert svg_drawdown_chart(flat).count("<svg") == 1

    assert "Not enough data" in svg_equity_chart(np.array([]), [])


def test_equity_csv_is_downsampled_but_keeps_the_last_row(small_result):
    cfg, result = small_result
    out = Path(cfg.reporting.output_dir) / "equity_small.csv"
    write_equity_csv(result.equity_curve, out, max_rows=100)

    frame = pd.read_csv(out)
    assert len(frame) <= 102
    assert frame["equity"].iloc[-1] == pytest.approx(result.equity_curve[-1].equity, abs=0.01)


def test_trades_csv_round_trips(small_result):
    cfg, result = small_result
    out = Path(cfg.reporting.output_dir) / "trades_small.csv"
    write_trades_csv(result.trades, out)

    frame = pd.read_csv(out)
    if result.trades:
        assert len(frame) == len(result.trades)
        assert {"entry_time", "exit_reason", "net_pnl", "r_multiple"} <= set(frame.columns)


def test_portfolio_report_aggregates(tmp_path: Path, small_result):
    cfg, result = small_result
    cfg.reporting.output_dir = str(tmp_path)
    paths = write_portfolio_report([result], cfg)

    assert paths["portfolio_csv"].exists()
    frame = pd.read_csv(paths["portfolio_csv"])
    assert list(frame["symbol"]) == ["EURUSD"]
    text = paths["portfolio_markdown"].read_text()
    assert "Portfolio comparison" in text
    assert "overstates" in text  # the honest caveat is present


# -----------------------------------------------------------------------------
# Live / replay
# -----------------------------------------------------------------------------
def test_replay_session_runs_and_reports(small_result, tmp_path: Path):
    cfg, _ = small_result
    cfg.data.synthetic.bars = 12_000
    cfg.reporting.output_dir = str(tmp_path)
    frames = build_feed(cfg).load()

    from scalper.brokers import build_broker

    broker = build_broker(cfg)
    trader = LiveTrader(
        cfg=cfg,
        broker=broker,
        strategy=get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params),
        risk=build_risk_manager(cfg),
        frames=frames,
        symbols=["EURUSD"],
        max_bars=2_000,
    )
    trader.run()

    assert trader.engine.stats.bars_processed > 0
    assert trader.engine.stats.bars_processed <= 2_000
    # One snapshot per processed bar, plus the final flatten mark from finish().
    assert trader.engine.stats.bars_processed <= len(trader.engine.equity_curve) <= (
        trader.engine.stats.bars_processed + 1
    )
    # The same broker is used, so the account must end where the engine says.
    assert broker.balance() == pytest.approx(cfg.account.initial_balance + sum(t.net_pnl for t in trader.engine.trades))
    # A session report is produced by the same writers as a backtest.
    assert list(tmp_path.glob("*_report.json"))


def test_replay_stops_at_max_bars(small_result):
    cfg, _ = small_result
    cfg.data.synthetic.bars = 8_000
    frames = build_feed(cfg).load()
    from scalper.brokers import build_broker

    broker = build_broker(cfg)
    trader = LiveTrader(
        cfg=cfg,
        broker=broker,
        strategy=get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params),
        risk=build_risk_manager(cfg),
        frames=frames,
        symbols=["EURUSD"],
        max_bars=500,
    )
    trader.run()
    assert trader.engine.stats.bars_processed == 500


def test_replay_never_sees_future_bars(small_result):
    """A replay must produce the same trades as a backtest on the same bars."""
    cfg, _ = small_result
    cfg.data.synthetic.bars = 20_000
    frames = build_feed(cfg).load()

    from scalper.brokers import build_broker

    broker_bt = build_broker(cfg)
    strategy = get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params)
    backtest = run_backtest(cfg, broker_bt, strategy, "EURUSD", frames["EURUSD"])

    broker_live = build_broker(cfg)
    trader = LiveTrader(
        cfg=cfg,
        broker=broker_live,
        strategy=get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params),
        risk=build_risk_manager(cfg),
        frames=frames,
        symbols=["EURUSD"],
    )
    trader.run()

    def signature(trades):
        return [(t.entry_time, t.direction.value, round(t.entry_price, 5), t.exit_reason.value) for t in trades]

    assert signature(trader.engine.trades) == signature(backtest.trades)


def test_stop_is_honoured_mid_session(small_result):
    """Ctrl-C must flatten, not abandon open positions."""
    cfg, _ = small_result
    cfg.data.synthetic.bars = 6_000
    frames = build_feed(cfg).load()
    from scalper.brokers import build_broker

    broker = build_broker(cfg)
    trader = LiveTrader(
        cfg=cfg,
        broker=broker,
        strategy=get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params),
        risk=build_risk_manager(cfg),
        frames=frames,
        symbols=["EURUSD"],
        max_bars=1_500,
    )
    trader.run()
    assert broker.positions() == []
    assert all(t.exit_reason.value != "" for t in trader.engine.trades)


def test_polling_exits_after_idle_polls(small_result):
    """A dead market must not trap the process in a silent infinite loop."""
    cfg, _ = small_result
    broker = PaperBroker(cfg.broker.paper, initial_balance=1_000)
    broker.connect()
    trader = LiveTrader(
        cfg=cfg,
        broker=broker,
        strategy=get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params),
        risk=build_risk_manager(cfg),
        frames=None,
        symbols=["EURUSD"],
        poll_seconds=0.05,
        max_idle_polls=3,
    )
    # The paper broker's "last bar" never advances, so it idles and stops.
    trader.run()
    assert trader.engine.stats.bars_processed == 0


def test_polling_trades_incoming_bars(small_result):
    """Polling must act on each new closed bar exactly once."""
    cfg, _ = small_result
    cfg.data.mt5.bars = 3_000
    frames = build_feed(cfg).load()
    history = frames["EURUSD"].iloc[:2_500]

    class FeedBroker(PaperBroker):
        """PaperBroker that also serves a finite, advancing bar stream."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stream = list(frames["EURUSD"].iloc[2_500:].itertuples())
            self.index = 0

        def history_frame(self, symbol, timeframe="M1", bars=50_000):
            return history

        def last_closed_bar(self, symbol, timeframe="M1"):
            if self.index >= len(self.stream):
                return None
            row = self.stream[self.index]
            self.index += 1
            from scalper.models import Bar

            return Bar(time=row.Index.to_pydatetime(), open=row.open, high=row.high,
                       low=row.low, close=row.close, volume=row.volume, symbol=symbol)

    broker = FeedBroker(cfg.broker.paper, initial_balance=cfg.account.initial_balance, leverage=30)
    broker.register_specs(list(cfg.instruments))
    broker.connect()
    trader = LiveTrader(
        cfg=cfg,
        broker=broker,
        strategy=get_strategy(cfg.strategy.name, symbol="EURUSD", **cfg.strategy.params),
        risk=build_risk_manager(cfg),
        frames=None,
        symbols=["EURUSD"],
        poll_seconds=0.0,
        max_bars=300,
        max_idle_polls=2,
    )
    trader.run()
    assert trader.engine.stats.bars_processed == 300
    # One point per bar, plus the closing snapshot that `finish()` always takes.
    assert len(trader.engine.equity_curve) == 301
