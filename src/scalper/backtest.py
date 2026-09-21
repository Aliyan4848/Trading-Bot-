"""Event-driven backtester.

Feeds historical bars through the *same* `Engine` that paper and live trading
use, so there is no second implementation to drift out of sync.

The lookahead rules it enforces:

  * a signal computed on bar ``t`` is filled at the open of bar ``t + 1``
    (`backtest.entry_on_next_open: true`, the default),
  * stops/targets on the entry bar are evaluated against the entry bar's own
    range, so a position can be stopped out the moment it opens,
  * indicators are computed once, vectorised, and never index past the current
    row — `tests/test_no_lookahead.py` asserts this by truncating the data and
    re-running.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from .brokers.base import Broker
from .config import AppConfig, InstrumentSpec
from .engine import Engine
from .metrics import PerformanceMetrics, compute_metrics
from .models import AccountSnapshot, Bar, Trade
from .risk import RiskManager
from .strategies.base import Strategy

log = logging.getLogger("scalper.backtest")


@dataclass
class BacktestResult:
    """Everything a backtest produced, ready for reporting."""

    symbol: str
    strategy: str
    timeframe: str
    start: datetime | None
    end: datetime | None
    bars: int
    initial_balance: float
    final_balance: float
    trades: list[Trade]
    equity_curve: list[AccountSnapshot]
    metrics: PerformanceMetrics
    engine_summary: dict[str, Any] = field(default_factory=dict)
    data_warnings: list[str] = field(default_factory=list)
    runtime_sec: float = 0.0
    config_path: str | None = None

    def trades_frame(self) -> pd.DataFrame:
        """Trades as a DataFrame (the 'show me every fill' view)."""
        if not self.trades:
            return pd.DataFrame(
                columns=[
                    "entry_time", "exit_time", "symbol", "direction", "lots", "entry_price",
                    "exit_price", "stop_price", "take_profit_price", "pips", "gross_pnl",
                    "commission", "net_pnl", "r_multiple", "exit_reason", "strategy",
                    "bars_held", "duration_minutes",
                ]
            )
        rows = []
        for t in self.trades:
            rows.append(
                {
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time,
                    "symbol": t.symbol,
                    "direction": t.direction.value,
                    "lots": t.lots,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "stop_price": t.stop_price,
                    "take_profit_price": t.take_profit_price,
                    "pips": round(t.pips, 2),
                    "gross_pnl": round(t.gross_pnl, 2),
                    "commission": round(t.commission, 2),
                    "net_pnl": round(t.net_pnl, 2),
                    "r_multiple": round(t.r_multiple, 3),
                    "exit_reason": t.exit_reason.value,
                    "strategy": t.strategy,
                    "bars_held": t.bars_held,
                    "duration_minutes": round(t.duration_minutes, 1),
                }
            )
        return pd.DataFrame(rows)

    def equity_frame(self) -> pd.DataFrame:
        if not self.equity_curve:
            return pd.DataFrame(columns=["time", "balance", "equity", "unrealized", "open_positions"])
        frame = pd.DataFrame(
            {
                "time": [s.time for s in self.equity_curve],
                "balance": [s.balance for s in self.equity_curve],
                "equity": [s.equity for s in self.equity_curve],
                "unrealized": [s.unrealized for s in self.equity_curve],
                "open_positions": [s.open_positions for s in self.equity_curve],
            }
        )
        return frame.set_index("time")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "timeframe": self.timeframe,
            "period": {
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None,
                "bars": self.bars,
                "days": round((self.end - self.start).total_seconds() / 86400, 2)
                if self.start and self.end
                else None,
            },
            "account": {
                "initial_balance": self.initial_balance,
                "final_balance": round(self.final_balance, 2),
                "net_profit": round(self.final_balance - self.initial_balance, 2),
            },
            "metrics": self.metrics.to_dict(),
            "engine": self.engine_summary,
            "data_warnings": self.data_warnings,
            "runtime_sec": round(self.runtime_sec, 3),
            "config_path": self.config_path,
        }


def run_backtest(
    cfg: AppConfig,
    broker: Broker,
    strategy: Strategy,
    symbol: str,
    df: pd.DataFrame,
    *,
    risk: RiskManager | None = None,
    record_equity: bool = True,
) -> BacktestResult:
    """Run one symbol through the engine over its full history."""
    started = time.perf_counter()
    symbol = symbol.upper()
    spec: InstrumentSpec = cfg.instrument(symbol)
    if len(df) < 10:
        raise ValueError(f"{symbol}: only {len(df)} bars — not enough to backtest")

    from .risk import build_risk_manager

    risk_manager = risk or build_risk_manager(cfg)
    engine = Engine(cfg, broker, strategy, risk_manager, record_equity=record_equity)

    broker.connect()
    engine.prepare_symbol(symbol, df, spec)

    # Pull the loop's inputs out of pandas once: this is the hot path.
    opens = df["open"].to_numpy(dtype="float64")
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    closes = df["close"].to_numpy(dtype="float64")
    volumes = df["volume"].to_numpy(dtype="float64")
    times = df.index.to_pydatetime()

    n = len(df)
    for i in range(n):
        bar = Bar(
            time=times[i],
            open=opens[i],
            high=highs[i],
            low=lows[i],
            close=closes[i],
            volume=volumes[i],
            symbol=symbol,
        )
        engine.on_bar(symbol, bar, i)

    engine.finish(times[-1])

    metrics = compute_metrics(
        trades=engine.trades,
        equity_curve=engine.equity_curve,
        initial_balance=cfg.account.initial_balance,
        bars_per_year=cfg.annualization_bars,
        risk_free_rate=cfg.backtest.risk_free_rate,
    )
    runtime = time.perf_counter() - started

    # A synthetic-data warning belongs IN the artifact, not only in the console
    # scrollback: the JSON/HTML outlive the terminal, and a report that reads
    # like real evidence is worse than no report. `data.source` is the ground
    # truth here because per-frame feed warnings are optional.
    data_warnings = list(df.attrs.get("warnings", []))
    if str(cfg.data.source).lower() == "synthetic":
        banner = (
            "SYNTHETIC DATA: bars were generated by a random process, not taken from the "
            "market. These results validate that the pipeline runs end to end; they say "
            "nothing about whether the strategy is profitable. Re-run with data.source: "
            "csv or mt5 and real history before drawing any conclusion."
        )
        if banner not in data_warnings:
            data_warnings.insert(0, banner)

    summary = engine.summary()
    result = BacktestResult(
        symbol=symbol,
        strategy=strategy.name,
        timeframe=cfg.data.timeframe.upper(),
        start=times[0],
        end=times[-1],
        bars=n,
        initial_balance=cfg.account.initial_balance,
        final_balance=broker.balance(),
        trades=list(engine.trades),
        equity_curve=list(engine.equity_curve),
        metrics=metrics,
        engine_summary=summary,
        data_warnings=data_warnings,
        runtime_sec=runtime,
        config_path=cfg.config_path,
    )
    log.info(
        "%s backtest finished in %.2fs: %d bars, %d trades, net %+.2f, PF %.2f",
        symbol,
        runtime,
        n,
        len(engine.trades),
        result.final_balance - cfg.account.initial_balance,
        metrics.profit_factor if metrics.profit_factor != float("inf") else 0.0,
    )
    return result


def walk_forward(
    cfg: AppConfig,
    broker_factory: Any,
    strategy: Strategy,
    symbol: str,
    df: pd.DataFrame,
    *,
    folds: int = 4,
    train_ratio: float = 0.7,
) -> list[BacktestResult]:
    """Split the history into consecutive folds and backtest each one.

    A single backtest tells you a strategy worked *on this data*. Splitting the
    history and seeing whether the edge survives in later, unseen slices is the
    cheapest defence against curve fitting. This is a coarse version: it does
    not re-optimise parameters per fold (see `scripts/optimize.py` for grid
    search), it just checks stability across time.
    """
    if folds < 2:
        raise ValueError("folds must be >= 2")
    if not 0 < train_ratio < 1:
        raise ValueError("train_ratio must be between 0 and 1")

    n = len(df)
    fold_size = n // folds
    if fold_size < cfg.backtest.warmup_bars + 100:
        raise ValueError(
            f"Not enough data for {folds} folds: {n} bars gives {fold_size} per fold, "
            f"but at least {cfg.backtest.warmup_bars + 100} are needed "
            f"(warmup {cfg.backtest.warmup_bars} + 100)."
        )

    results: list[BacktestResult] = []
    for fold in range(folds):
        start = fold * fold_size
        end = n if fold == folds - 1 else (fold + 1) * fold_size + cfg.backtest.warmup_bars
        chunk = df.iloc[start:end]
        if len(chunk) < cfg.backtest.warmup_bars + 100:
            continue
        log.info("walk-forward fold %d/%d: %s -> %s (%d bars)",
                 fold + 1, folds, chunk.index[0], chunk.index[-1], len(chunk))
        broker = broker_factory()
        results.append(run_backtest(cfg, broker, strategy, symbol, chunk))
    return results


# ----------------------------------------------------------------------------- #
# Multi-symbol portfolio backtest
# ----------------------------------------------------------------------------- #
def _portfolio_timeline(frames: dict[str, pd.DataFrame]) -> list[tuple[datetime, str]]:
    """Every (bar time, symbol) pair, ordered by time then symbol.

    Symbols are interleaved on one clock so the single account sees them in the
    order the market did. Ties are broken by symbol name so a run is
    deterministic — without that, two symbols printing the same minute could
    swap order through dict iteration and produce a different equity curve.
    """
    events: list[tuple[datetime, str]] = []
    for symbol, df in frames.items():
        events.extend((ts, symbol) for ts in df.index.to_pydatetime())
    events.sort(key=lambda item: (item[0], item[1]))
    return events


def run_portfolio_backtest(
    cfg: AppConfig,
    broker: Broker,
    strategy_by_symbol: dict[str, Strategy],
    frames: dict[str, pd.DataFrame],
    *,
    risk: RiskManager | None = None,
    record_equity: bool = True,
    max_bars: int | None = None,
) -> BacktestResult:
    """Run every symbol through ONE engine sharing one account.

    This is the honest way to size several pairs at once. Summing independent
    per-symbol runs is wrong in two directions:

      * **Overstated.** Each run starts with the full balance, so five symbols
        each risking 0.5% look like five accounts, and every one of them can be
        at its own `max_concurrent_positions` limit.
      * **Understated.** Open risk is never netted. Being long EURUSD, long
        GBPUSD and short USDJPY is one dollar bet, not three; the sum cannot
        show you that the three stop-outs arrive together.

    Here every position is sized from one live equity figure, the concurrency
    and daily-trade caps are enforced portfolio-wide, and correlated drawdown
    is whatever one account actually experienced. Note that positions are
    marked to market on the bars they trade: with well-behaved per-symbol data
    that is exact, and symbols with sparse or non-overlapping history simply
    contribute nothing while they are flat.
    """
    started = time.perf_counter()
    if not frames:
        raise ValueError("portfolio backtest needs at least one symbol's data")

    from .risk import build_risk_manager

    risk_manager = risk or build_risk_manager(cfg)
    unknown = sorted(set(frames) - set(strategy_by_symbol))
    if unknown:
        raise ValueError(f"no strategy supplied for {unknown}")
    any_strategy = strategy_by_symbol[sorted(frames)[0]]
    engine = Engine(cfg, broker, any_strategy, risk_manager, record_equity=record_equity)

    broker.connect()
    for symbol in sorted(frames):
        engine.prepare_symbol(symbol, frames[symbol], cfg.instrument(symbol))

    events = _portfolio_timeline({s: frames[s] for s in sorted(frames)})
    if max_bars is not None:
        events = events[: int(max_bars)]
    if not events:
        raise ValueError("portfolio backtest has no bars to process")

    # Convert each frame once; per-bar `.loc` lookups in the hot loop would be
    # an order of magnitude slower.
    arrays: dict[str, dict[str, Any]] = {}
    for symbol, df in frames.items():
        arrays[symbol] = {
            "open": df["open"].to_numpy(dtype="float64"),
            "high": df["high"].to_numpy(dtype="float64"),
            "low": df["low"].to_numpy(dtype="float64"),
            "close": df["close"].to_numpy(dtype="float64"),
            "volume": df["volume"].to_numpy(dtype="float64"),
            "index": {ts: i for i, ts in enumerate(df.index)},
        }

    for when, symbol in events:
        row = arrays[symbol]["index"][when]
        engine.on_bar(
            symbol,
            Bar(
                time=when,
                open=float(arrays[symbol]["open"][row]),
                high=float(arrays[symbol]["high"][row]),
                low=float(arrays[symbol]["low"][row]),
                close=float(arrays[symbol]["close"][row]),
                volume=float(arrays[symbol]["volume"][row]),
                symbol=symbol,
            ),
            row,
        )

    engine.finish(events[-1][0])

    metrics = compute_metrics(
        trades=engine.trades,
        equity_curve=engine.equity_curve,
        initial_balance=cfg.account.initial_balance,
        bars_per_year=cfg.annualization_bars,
        risk_free_rate=cfg.backtest.risk_free_rate,
    )
    runtime = time.perf_counter() - started

    data_warnings: list[str] = []
    if str(cfg.data.source).lower() == "synthetic":
        data_warnings.append(
            "SYNTHETIC DATA: bars were generated by a random process, not taken from the "
            "market. These results validate that the pipeline runs end to end; they say "
            "nothing about whether the strategy is profitable. Re-run with data.source: "
            "csv or mt5 and real history before drawing any conclusion."
        )
    data_warnings.append(
        "PORTFOLIO: every symbol traded one shared account (sizing, concurrency cap and "
        "daily loss cap applied to the account as a whole), so this is not the sum of the "
        "per-symbol runs."
    )
    for symbol in sorted(frames):
        for warning in frames[symbol].attrs.get("warnings", []):
            data_warnings.append(f"{symbol}: {warning}")

    summary = engine.summary()
    summary["symbols"] = sorted(frames)
    summary["bars_processed"] = engine.stats.bars_processed
    result = BacktestResult(
        symbol="PORTFOLIO",
        strategy=any_strategy.name,
        timeframe=cfg.data.timeframe.upper(),
        start=events[0][0],
        end=events[-1][0],
        bars=len(events),
        initial_balance=cfg.account.initial_balance,
        final_balance=broker.balance(),
        trades=list(engine.trades),
        equity_curve=list(engine.equity_curve),
        metrics=metrics,
        engine_summary=summary,
        data_warnings=data_warnings,
        runtime_sec=runtime,
        config_path=cfg.config_path,
    )
    log.info(
        "PORTFOLIO backtest finished in %.2fs: %d symbols, %d bars, %d trades, net %+.2f, PF %.2f",
        runtime,
        len(frames),
        len(events),
        len(engine.trades),
        result.final_balance - cfg.account.initial_balance,
        metrics.profit_factor if metrics.profit_factor != float("inf") else 0.0,
    )
    return result
