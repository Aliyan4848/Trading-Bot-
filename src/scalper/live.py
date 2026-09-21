"""Live loops: accelerated replay for paper sessions, polling for MT5.

Two modes, one engine:

* **Replay** (`frames` supplied) — walks historical or synthetic bars through
  the engine with a delay between them. This is how you watch the bot operate —
  risk checks, position management, session handling and all — without waiting
  for the market or risking a cent. It is a rehearsal, not a backtest: the
  engine only ever sees bars in order, exactly as it would live.
* **Polling** (`frames=None`) — asks the venue for the latest *closed* bar every
  few seconds and feeds each new one through the engine. Signals are recomputed
  on the rolling window as bars arrive, so the bot always trades the same rules
  it backtested.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pandas as pd

from .brokers.base import Broker
from .config import AppConfig
from .data.base import build_timeline
from .engine import Engine
from .models import AccountSnapshot, Bar, Trade
from .risk import RiskManager
from .strategies.base import Strategy

log = logging.getLogger("scalper.live")


class LiveTrader:
    """Drives the engine from a live/replayed feed."""

    def __init__(
        self,
        cfg: AppConfig,
        broker: Broker,
        strategy: Strategy,
        risk: RiskManager,
        *,
        frames: dict[str, pd.DataFrame] | None = None,
        symbols: list[str] | None = None,
        poll_seconds: float = 2.0,
        max_bars: int = 0,
        delay_seconds: float = 0.0,
        heartbeat_bars: int = 500,
        max_history_bars: int = 20_000,
        max_idle_polls: int = 0,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.frames = {k.upper(): v for k, v in (frames or {}).items()}
        self.symbols = [s.upper() for s in (symbols or list(self.frames) or cfg.symbols)]
        self.poll_seconds = max(poll_seconds, 0.05)
        self.max_bars = max_bars
        self.delay_seconds = delay_seconds
        self.heartbeat_bars = max(heartbeat_bars, 1)
        self.max_history_bars = max_history_bars
        #: Stop after this many consecutive polls with no new bar (0 = never).
        #: Useful for demos and for not sitting forever on a closed market.
        self.max_idle_polls = max_idle_polls

        self.engine = Engine(cfg, broker, strategy, risk)
        self._history: dict[str, pd.DataFrame] = {}
        self._last_seen: dict[str, datetime] = {}
        self._stopping = False
        self.started_at: datetime | None = None

    # -- public ---------------------------------------------------------------
    def run(self) -> None:
        """Run until max_bars is reached, the feed ends, or Ctrl-C."""
        self.started_at = datetime.now()
        self.broker.connect()
        try:
            if self.frames:
                self._run_replay()
            else:
                self._run_polling()
        except KeyboardInterrupt:
            log.info("Interrupted by user — flattening open positions.")
        finally:
            closed = self.engine.finish()
            if closed:
                log.info("Flattened %d position(s) on shutdown", len(closed))
            self._report()

    def stop(self) -> None:
        self._stopping = True

    # -- replay ---------------------------------------------------------------
    def _run_replay(self) -> None:
        timeline = build_timeline([self._history_frame(s) for s in self.symbols])

        # Prime the engine with the same bars it will later "see live": signals
        # are computed up front, and only rows up to the current bar are read.
        for symbol in self.symbols:
            self._prime(symbol)

        log.info(
            "Replay mode: %d bars from %s to %s across %d symbol(s)%s",
            len(timeline),
            timeline[0] if len(timeline) else "n/a",
            timeline[-1] if len(timeline) else "n/a",
            len(self.symbols),
            f", {self.delay_seconds:g}s per bar" if self.delay_seconds else ", full speed",
        )

        bar_count = 0
        for stamp in timeline:
            if self._stopping:
                break
            for symbol in self.symbols:
                frame = self._history_frame(symbol)
                if stamp not in frame.index or stamp <= self._last_seen.get(symbol, stamp - pd.Timedelta("1s")):
                    continue
                row = frame.loc[stamp]
                bar = Bar(
                    time=stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0)),
                    symbol=symbol,
                )
                index = frame.index.get_loc(stamp)
                self.engine.on_bar(symbol, bar, int(index))
                self._last_seen[symbol] = stamp
                bar_count += 1

            if bar_count and bar_count % self.heartbeat_bars == 0:
                self._heartbeat()
            if self.max_bars and bar_count >= self.max_bars:
                log.info("Reached --max-bars %d, stopping replay.", self.max_bars)
                break
            if self.delay_seconds:
                time.sleep(self.delay_seconds)

    def _history_frame(self, symbol: str) -> pd.DataFrame:
        frame = self._history.get(symbol)
        if frame is None:
            frame = self.frames[symbol]
            self._history[symbol] = frame
        return frame

    def _prime(self, symbol: str) -> None:
        frame = self._history_frame(symbol)
        spec = self.cfg.instrument(symbol)
        self.engine.prepare_symbol(symbol, frame, spec)

    # -- polling (MT5) --------------------------------------------------------
    def _run_polling(self) -> None:
        fetcher = self._build_bar_fetcher()
        for symbol in self.symbols:
            history = fetcher(symbol, warmup=True)
            if history is None or len(history) == 0:
                raise RuntimeError(
                    f"{symbol}: no history available. Is the terminal connected and the symbol "
                    f"in Market Watch?"
                )
            self._history[symbol] = history
            self._prime(symbol)
            last = history.index[-1]
            self._last_seen[symbol] = last
            log.info("%s: primed with %d bars (latest %s)", symbol, len(history), last)

        log.info(
            "Polling every %.1fs. Ctrl-C to stop.%s",
            self.poll_seconds,
            f" Will stop after {self.max_bars} new bars." if self.max_bars else "",
        )

        new_bars = 0
        idle_polls = 0
        while not self._stopping:
            polled_something = False
            for symbol in self.symbols:
                bar = fetcher(symbol, warmup=False)
                if bar is None or bar.time <= self._last_seen.get(symbol, bar.time - pd.Timedelta("1s")):
                    continue
                self._append_bar(symbol, bar)
                history = self._history[symbol]
                self.engine.on_bar(symbol, bar, len(history) - 1)
                self._last_seen[symbol] = bar.time
                new_bars += 1
                polled_something = True
                log.info("new bar %s %s close=%.5f", symbol, bar.time, bar.close)

            idle_polls = 0 if polled_something else idle_polls + 1
            if new_bars and new_bars % self.heartbeat_bars == 0:
                self._heartbeat()
            if self.max_bars and new_bars >= self.max_bars:
                log.info("Reached --max-bars %d, stopping.", self.max_bars)
                break
            if self.max_idle_polls and idle_polls >= self.max_idle_polls:
                log.info(
                    "No new bars for %d consecutive polls, stopping (max_idle_polls).",
                    idle_polls,
                )
                break
            time.sleep(self.poll_seconds)

    def _append_bar(self, symbol: str, bar: Bar) -> None:
        """Add a newly closed bar to the rolling window and refresh signals."""
        frame = self._history[symbol]
        stamp = pd.Timestamp(bar.time)
        if frame.index.tz is None:
            stamp = stamp.tz_localize(None) if stamp.tzinfo else stamp
        row = pd.DataFrame(
            {
                "open": [bar.open],
                "high": [bar.high],
                "low": [bar.low],
                "close": [bar.close],
                "volume": [bar.volume],
            },
            index=pd.DatetimeIndex([stamp]),
        )
        updated = pd.concat([frame, row])
        updated = updated[~updated.index.duplicated(keep="last")].sort_index()
        if len(updated) > self.max_history_bars:
            updated = updated.iloc[-self.max_history_bars :]
        updated.attrs.update(frame.attrs)
        self._history[symbol] = updated
        # Recompute signals on the extended window: the newest bar can change the
        # previous bar's indicator values only for indicators that are causal
        # (they all are), so this is cheap insurance, not a correction.
        self.engine.refresh_symbol(symbol, updated, self.cfg.instrument(symbol))

    def _build_bar_fetcher(self) -> Callable[..., Any]:
        """Return a callable that yields bars from the venue."""
        if hasattr(self.broker, "last_closed_bar"):
            def fetch(symbol: str, warmup: bool = False) -> Any:
                if warmup:
                    return self._warmup_history(symbol)
                return self.broker.last_closed_bar(symbol, self.cfg.data.timeframe)  # type: ignore[attr-defined]

            return fetch
        raise RuntimeError(
            f"{type(self.broker).__name__} cannot supply live bars. Use broker.mode: mt5, "
            f"or pass --source synthetic/csv for a replay session."
        )

    def _warmup_history(self, symbol: str) -> pd.DataFrame | None:
        """History used to prime indicators before live bars start arriving."""
        loader = getattr(self.broker, "history_frame", None)
        if loader is not None:
            return loader(symbol, self.cfg.data.timeframe, self.cfg.data.mt5.bars)
        from .data import build_feed

        frames = build_feed(self.cfg).load()
        return frames.get(symbol)

    # -- reporting ------------------------------------------------------------
    def _heartbeat(self) -> None:
        equity = self.broker.equity()
        summary = self.risk.summary()
        log.info(
            "heartbeat | equity %.2f %s | open %d | trades today %d | day P&L %+.2f | daily loss %.2f%%/%s%%",
            equity,
            self.cfg.account.currency,
            len(self.broker.positions()),
            summary["trades_today"],
            summary["day_realized_pnl"],
            summary["daily_loss_pct"],
            summary["daily_loss_limit_pct"],
        )

    def _report(self) -> None:
        """Print (and save) what the session produced."""
        from .metrics import compute_metrics
        from .report import write_all_reports

        trades: list[Trade] = self.engine.trades
        equity_curve: list[AccountSnapshot] = self.engine.equity_curve
        if not equity_curve:
            log.info("Session finished with no equity samples to report.")
            return

        metrics = compute_metrics(
            trades=trades,
            equity_curve=equity_curve,
            initial_balance=self.cfg.account.initial_balance,
            bars_per_year=self.cfg.annualization_bars,
        )
        print()
        print("=" * 96)
        print("SESSION RESULT: " + metrics.headline())
        print("=" * 96)

        result = SessionResult.from_trader(self, metrics)
        try:
            paths = write_all_reports(result, self.cfg, synthetic=self.cfg.data.source == "synthetic")
            for kind, path in paths.items():
                log.info("wrote %s: %s", kind, path)
        except Exception as exc:  # noqa: BLE001 - reporting must never kill a session log
            log.warning("Could not write reports: %s", exc)


class SessionResult:
    """Report-shaped view of a live/paper session.

    Mirrors `BacktestResult` closely enough for the report writers, without
    pretending a live session had a clean start/end period.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.symbol: str = kwargs["symbol"]
        self.strategy: str = kwargs["strategy"]
        self.timeframe: str = kwargs["timeframe"]
        self.bars: int = kwargs["bars"]
        self.initial_balance: float = kwargs["initial_balance"]
        self.final_balance: float = kwargs["final_balance"]
        self.trades: list[Trade] = kwargs["trades"]
        self.equity_curve: list[AccountSnapshot] = kwargs["equity_curve"]
        self.metrics: Any = kwargs["metrics"]
        self.engine_summary: dict[str, Any] = kwargs["engine_summary"]
        self.data_warnings: list[str] = kwargs.get("data_warnings", [])
        self.runtime_sec: float = kwargs["runtime_sec"]
        self.config_path: str | None = kwargs.get("config_path")

    @classmethod
    def from_trader(cls, trader: LiveTrader, metrics: Any) -> SessionResult:
        curve = trader.engine.equity_curve
        started = trader.started_at or datetime.now()
        return cls(
            symbol=",".join(trader.symbols),
            strategy=trader.strategy.name,
            timeframe=str(trader.cfg.data.timeframe).upper(),
            bars=trader.engine.stats.bars_processed,
            initial_balance=trader.cfg.account.initial_balance,
            final_balance=trader.broker.balance(),
            trades=list(trader.engine.trades),
            equity_curve=list(curve),
            metrics=metrics,
            engine_summary=trader.engine.summary(),
            runtime_sec=(datetime.now() - started).total_seconds(),
            config_path=trader.cfg.config_path,
        )

    def to_dict(self) -> dict[str, Any]:
        start = self.equity_curve[0].time if self.equity_curve else None
        end = self.equity_curve[-1].time if self.equity_curve else None
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "mode": "paper" if not getattr(self, "is_live", False) else "live",
            "timeframe": self.timeframe,
            "period": {
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "bars": self.bars,
                "days": round((end - start).total_seconds() / 86400, 3) if start and end else None,
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
