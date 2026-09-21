"""Command line interface.

    scalper backtest  --config config/config.yaml
    scalper paper     --config config/config.yaml
    scalper live      --config config/config.yaml --live --i-understand-the-risk
    scalper download  --config config/config.yaml
    scalper strategies
    scalper doctor

`live` needs three separate confirmations (a real-money MT5 account, `--live`,
and `SCALPER_ALLOW_LIVE=yes`). That is deliberate: the failure mode of a
mis-typed flag should be "it refused", never "it bought".
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import AppConfig, load_config, load_env_file

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
LOG_DATE = "%H:%M:%S"


def setup_logging(level: str = "INFO", quiet: bool = False) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO) if not quiet else logging.WARNING,
        format=LOG_FORMAT,
        datefmt=LOG_DATE,
        stream=sys.stdout,
    )
    # The terminal-polling feeds are chatty at DEBUG.
    logging.getLogger("scalper.data.mt5").setLevel(logging.INFO)


def _load(args: argparse.Namespace) -> AppConfig:
    load_env_file(args.env)
    overrides = _collect_overrides(args)
    cfg = load_config(args.config, overrides)
    setup_logging(cfg.meta.log_level, quiet=getattr(args, "quiet", False))
    return cfg


def _collect_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Turn friendly CLI flags into config overrides."""
    overrides: dict[str, Any] = {}
    if getattr(args, "strategy", None):
        overrides["strategy.name"] = args.strategy
    if getattr(args, "symbol", None):
        overrides["instruments"] = [
            {"symbol": s, "pip_size": 0.0001, "pip_value_per_lot": 10.0, "spread_pips": 1.0, "digits": 5}
            for s in args.symbol
        ]
    if getattr(args, "timeframe", None):
        overrides["data.timeframe"] = args.timeframe
    if getattr(args, "balance", None):
        overrides["account.initial_balance"] = float(args.balance)
    if getattr(args, "bars", None):
        overrides["data.synthetic.bars"] = int(args.bars)
    if getattr(args, "risk", None):
        overrides["risk.risk_per_trade_pct"] = float(args.risk)
    if getattr(args, "source", None):
        overrides["data.source"] = args.source
    if getattr(args, "csv", None):
        overrides["data.csv.path"] = args.csv
    if getattr(args, "spread", None):
        overrides["data.synthetic.spread_pips"] = float(args.spread)
    for name in ("fast_ema", "slow_ema", "trend_ema", "rsi_period", "reward_risk"):
        value = getattr(args, name, None)
        if value is not None:
            overrides[f"strategy.params.{name}"] = value
    return overrides


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------
def cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest import run_backtest
    from .brokers import build_broker
    from .data import build_feed
    from .report import write_all_reports, write_portfolio_report
    from .strategies import get_strategy

    cfg = _load(args)
    log = logging.getLogger("scalper.cli")
    log.info("Backtest | %s | strategy=%s | data=%s", cfg.meta.name, cfg.strategy.name, cfg.data.source)

    frames = build_feed(cfg).load()
    symbols = args.symbols or cfg.symbols
    synthetic = str(cfg.data.source).lower() == "synthetic"

    if getattr(args, "portfolio", False):
        return _run_portfolio_cli(args, cfg, frames, symbols, synthetic)

    results = []
    for symbol in symbols:
        if symbol not in frames:
            log.warning("No data for %s, skipping", symbol)
            continue
        df = frames[symbol]
        for warning in df.attrs.get("warnings", []):
            log.warning("data: %s", warning)

        broker = build_broker(cfg)
        strategy = get_strategy(cfg.strategy.name, symbol=symbol, **cfg.strategy.params)
        try:
            result = run_backtest(cfg, broker, strategy, symbol, df)
        except (ValueError, RuntimeError) as exc:
            log.error("%s: backtest failed — %s", symbol, exc)
            continue
        results.append(result)
        log.info("%s | %s", symbol, result.metrics.headline())
        if not args.no_reports:
            write_all_reports(result, cfg, synthetic=synthetic)

    if not results:
        log.error("No backtests completed. Check your config and data source.")
        return 1

    print()
    print("=" * 100)
    print(f"{'SYMBOL':<10}{'TRADES':>8}{'NET':>13}{'RET%':>9}{'WIN%':>8}{'PF':>7}{'EXP(R)':>9}{'MAXDD%':>9}{'SHARPE':>8}")
    print("-" * 100)
    for r in sorted(results, key=lambda x: x.metrics.expectancy_r, reverse=True):
        m = r.metrics
        pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        print(
            f"{r.symbol:<10}{m.total_trades:>8}{m.net_profit:>13,.2f}{m.return_pct:>9.2f}"
            f"{m.win_rate_pct:>8.1f}{pf:>7}{m.expectancy_r:>9.3f}{m.max_drawdown_pct:>9.2f}{m.sharpe:>8.2f}"
        )
    print("=" * 100)
    if synthetic:
        print("\n!! SYNTHETIC DATA — these numbers validate the pipeline, not the strategy.")
        print("!! Re-run with real history: data.source: csv (or mt5).\n")
    if len(results) > 1 and not args.no_reports:
        paths = write_portfolio_report(results, cfg, synthetic=synthetic)
        for kind, path in paths.items():
            log.info("wrote %s: %s", kind, path)
    return 0


def _run_portfolio_cli(
    args: argparse.Namespace, cfg: Any, frames: dict[str, Any], symbols: list[str], synthetic: bool
) -> int:
    """`scalper backtest --portfolio`: one account, every symbol, one clock."""
    from .backtest import run_portfolio_backtest
    from .brokers import build_broker
    from .report import write_all_reports
    from .strategies import get_strategy

    log = logging.getLogger("scalper.cli")
    used = {s: frames[s] for s in symbols if s in frames}
    missing = [s for s in symbols if s not in frames]
    for symbol in missing:
        log.warning("No data for %s, skipping", symbol)
    if not used:
        log.error("No data for any configured symbol.")
        return 1

    strategies = {
        symbol: get_strategy(cfg.strategy.name, symbol=symbol, **cfg.strategy.params)
        for symbol in used
    }
    broker = build_broker(cfg)
    try:
        result = run_portfolio_backtest(cfg, broker, strategies, used)
    except (ValueError, RuntimeError) as exc:
        log.error("portfolio backtest failed — %s", exc)
        return 1

    log.info("PORTFOLIO | %s", result.metrics.headline())
    print()
    print("=" * 100)
    print(f"PORTFOLIO  {len(used)} symbols on one account: {', '.join(sorted(used))}")
    print("-" * 100)
    print(result.metrics.headline())
    print("-" * 100)
    print("per-symbol contribution (net P&L, % of trades)")
    by_symbol: dict[str, list[float]] = {}
    for trade in result.trades:
        by_symbol.setdefault(trade.symbol, []).append(trade.net_pnl)
    total_trades = max(1, len(result.trades))
    for symbol, pnls in sorted(by_symbol.items(), key=lambda kv: -sum(kv[1])):
        share = 100.0 * len(pnls) / total_trades
        print(f"  {symbol:<8}{sum(pnls):>12,.2f}{share:>8.1f}%  ({len(pnls)} trades)")
    print("=" * 100)
    if synthetic:
        print("\n!! SYNTHETIC DATA — these numbers validate the pipeline, not the strategy.")
        print("!! Re-run with real history: data.source: csv (or mt5).\n")

    if not getattr(args, "no_reports", False):
        paths = write_all_reports(result, cfg, synthetic=synthetic)
        for kind, path in paths.items():
            log.info("wrote %s: %s", kind, path)
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    """Live paper trading: real market data, simulated fills."""
    from .brokers import build_broker
    from .data import build_feed
    from .live import LiveTrader
    from .risk import build_risk_manager
    from .strategies import get_strategy

    cfg = _load(args)
    log = logging.getLogger("scalper.cli")

    frames = build_feed(cfg).load()
    broker = build_broker(cfg)
    broker.connect()
    strategy = get_strategy(cfg.strategy.name, symbol=(args.symbols or cfg.symbols)[0], **cfg.strategy.params)
    risk = build_risk_manager(cfg)

    trader = LiveTrader(
        cfg=cfg,
        broker=broker,
        strategy=strategy,
        risk=risk,
        frames=frames,
        symbols=args.symbols or cfg.symbols,
        poll_seconds=args.poll,
        max_bars=args.max_bars,
        max_idle_polls=args.max_idle_polls,
    )
    log.info("Paper trading started. Ctrl-C to stop.")
    trader.run()
    log.info("Paper session finished: %s", broker.stats() if hasattr(broker, "stats") else "")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    from .config import is_live_allowed
    from .live import LiveTrader
    from .risk import build_risk_manager
    from .strategies import get_strategy

    log = logging.getLogger("scalper.cli")
    cfg = _load(args)

    if str(cfg.broker.mode).lower() != "mt5":
        log.error(
            "Live trading requires `broker.mode: mt5` in your config (currently %r). "
            "Set it explicitly — that edit is your first confirmation.",
            cfg.broker.mode,
        )
        return 2
    if not args.live:
        log.error(
            "Refusing to trade real money without --live.\n"
            "  1. Test with `scalper paper` first.\n"
            "  2. Start on a DEMO MT5 account with broker.mode: mt5.\n"
            "  3. Only then add --live."
        )
        return 2
    if not is_live_allowed():
        log.error(
            "SCALPER_ALLOW_LIVE is not set to 'yes' in your environment/.env.\n"
            "This second switch exists so a single mistake cannot spend real money."
        )
        return 2
    if not args.i_understand_the_risk:
        log.error("Add --i-understand-the-risk to acknowledge that this can lose real money.")
        return 2

    from .brokers.mt5 import build_mt5_broker

    broker = build_mt5_broker(cfg, allow_live=True)
    broker.connect()
    summary = broker.summary()
    log.warning(
        "Connected to MT5 %s%s | balance %.2f %s",
        summary["login"],
        " (DEMO)" if summary["is_demo"] else " (LIVE MONEY)",
        summary["balance"],
        summary["currency"],
    )

    strategy = get_strategy(cfg.strategy.name, symbol=(args.symbols or cfg.symbols)[0], **cfg.strategy.params)
    risk = build_risk_manager(cfg)
    risk.state.peak_equity = broker.equity()

    trader = LiveTrader(
        cfg=cfg,
        broker=broker,
        strategy=strategy,
        risk=risk,
        frames=None,
        symbols=args.symbols or cfg.symbols,
        poll_seconds=args.poll,
        max_bars=args.max_bars,
        max_idle_polls=args.max_idle_polls,
    )
    try:
        trader.run()
    finally:
        log.info("Live session finished: %s", broker.summary())
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    """Save bars to CSV so backtests are reproducible offline."""
    from .data import build_feed, write_csv

    cfg = _load(args)
    log = logging.getLogger("scalper.cli")
    feed = build_feed(cfg)
    frames = feed.load()
    out_dir = Path(args.out or "data")
    out_dir.mkdir(parents=True, exist_ok=True)
    for symbol, frame in frames.items():
        path = out_dir / f"{symbol}_{cfg.data.timeframe.upper()}.csv"
        write_csv(frame, path)
        log.info(
            "wrote %s (%d bars, %s -> %s)",
            path,
            len(frame),
            frame.index[0],
            frame.index[-1],
        )
    log.info("Now set `data.source: csv` and `data.csv.path: data/{{symbol}}_%s.csv`", cfg.data.timeframe.upper())
    return 0


def cmd_strategies(args: argparse.Namespace) -> int:
    from .strategies import available_strategies

    print("\nRegistered strategies\n" + "=" * 70)
    for name, cls in sorted(available_strategies().items()):
        doc = (cls.__doc__ or "").strip().splitlines()
        summary = next((line.strip() for line in doc if line.strip()), "")
        print(f"\n  {name}\n    {summary}")
        params = cls.default_params()
        if params:
            print("    params:")
            for key, value in params.items():
                print(f"      {key} = {value!r}")
    print("\nUse one with:  strategy:\\n  name: <name>\\n  params: {...}\n")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the optional Streamlit dashboard over `results/`."""
    import importlib.util
    import subprocess

    if importlib.util.find_spec("streamlit") is None:
        print(
            "Streamlit is not installed.\n\n"
            '  pip install ".[dashboard]"      # or: pip install streamlit altair\n',
            file=sys.stderr,
        )
        return 2

    app = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"
    if not app.is_file():
        print(f"Dashboard app not found at {app}", file=sys.stderr)
        return 2

    cfg = _load(args)
    address = getattr(args, "address", "0.0.0.0")
    port = int(getattr(args, "port", 8501))
    print(f"Dashboard reading {cfg.reporting.output_dir}  ->  http://localhost:{port}")
    print("Press Ctrl-C to stop.")
    try:
        return subprocess.call(
            [
                sys.executable, "-m", "streamlit", "run", str(app),
                "--server.address", address,
                "--server.port", str(port),
                "--server.headless", "true",
                "--browser.gatherUsageStats", "false",
            ]
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130


def cmd_doctor(args: argparse.Namespace) -> int:
    """Environment check: config, data, dependencies, and live-readiness."""
    import importlib.util
    import platform

    from .config import TIMEFRAME_MINUTES

    print("\nfx-scalper doctor\n" + "=" * 60)
    print(f"python            : {platform.python_version()} ({platform.system()})")

    for pkg in ("numpy", "pandas", "yaml", "MetaTrader5", "streamlit"):
        found = importlib.util.find_spec(pkg) is not None
        note = ""
        if pkg == "MetaTrader5" and not found:
            note = "  <- needed only for broker.mode: mt5 (Windows)"
        if pkg == "streamlit" and not found:
            note = "  <- needed only for the optional dashboard"
        print(f"{pkg:<18}: {'installed' if found else 'MISSING'}{note}")

    try:
        load_env_file(args.env)
        cfg = load_config(args.config, _collect_overrides(args))
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        print(f"\nconfig            : INVALID — {exc}")
        return 1

    print(f"config            : ok ({cfg.config_path})")
    print(f"strategy          : {cfg.strategy.name}")
    print(f"data source       : {cfg.data.source} ({cfg.data.timeframe}, known: {', '.join(TIMEFRAME_MINUTES)})")
    print(f"broker mode       : {cfg.broker.mode}")
    print(f"instruments       : {', '.join(cfg.symbols)}")
    print(f"risk per trade    : {cfg.risk.risk_per_trade_pct}% of equity")
    print(f"sessions          : {len(cfg.session.windows)} window(s), flat-at-close={cfg.session.flat_at_close}")

    if cfg.data.source == "csv":
        path = cfg.data.csv.path
        exists = Path(path).exists() or "{symbol}" in path
        print(f"csv path          : {path} ({'found' if exists else 'NOT FOUND'})")
    if cfg.data.source == "mt5":
        import importlib.util as iu

        print(f"mt5 package       : {'ok' if iu.find_spec('MetaTrader5') else 'MISSING (pip install -r requirements-mt5.txt)'}")

    print("\nlive-trading gates")
    from .config import is_live_allowed

    print(f"  SCALPER_ALLOW_LIVE : {'yes' if is_live_allowed() else 'not set'}")
    print(f"  broker.mode        : {cfg.broker.mode}")
    print(f"  -> live routing    : {'ENABLED' if (is_live_allowed() and cfg.broker.mode == 'mt5') else 'disabled (safe)'}")

    print("\nSuggested next step")
    if cfg.data.source == "synthetic":
        print("  You are on synthetic data. Export real M1 history (MT5 > Tools > History Center,")
        print("  or Dukascopy) and set data.source: csv before trusting any result.")
    else:
        print("  Run:  scalper backtest --config " + str(cfg.config_path))
    print()
    return 0


# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scalper",
        description="Forex scalping bot: backtest, paper trade, and optionally trade via MetaTrader 5.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  scalper doctor\n"
            "  scalper backtest --config config/config.yaml\n"
            "  scalper backtest --strategy vwap_pullback --symbol EURUSD --bars 300000\n"
            "  scalper backtest --source csv --csv 'data/{symbol}_M1.csv'\n"
            "  scalper paper --config config/config.yaml --max-bars 500\n"
            "  scalper strategies\n"
        ),
    )
    parser.add_argument("--config", default="config/config.yaml", help="path to the YAML config")
    parser.add_argument("--env", default=".env", help="path to a .env file with credentials")
    parser.add_argument("--quiet", action="store_true", help="warnings and errors only")

    # Also accept --config/--env/--quiet *after* the subcommand, because that is
    # how everyone writes it. SUPPRESS keeps the sub-parser from clobbering a
    # value that was already supplied before the subcommand.
    globals_parent = argparse.ArgumentParser(add_help=False)
    globals_parent.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    globals_parent.add_argument("--env", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    globals_parent.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command")

    common = {
        "--strategy": {"help": "override strategy.name"},
        "--symbol": {"action": "append", "help": "restrict to a symbol (repeatable)"},
        "--timeframe": {"help": "override data.timeframe (M1, M5, M15, H1...)"},
        "--source": {"choices": ["synthetic", "csv", "mt5"], "help": "override data.source"},
        "--csv": {"help": "override data.csv.path (supports {symbol})"},
        "--balance": {"help": "override account.initial_balance"},
        "--risk": {"help": "override risk.risk_per_trade_pct"},
        "--bars": {"help": "override data.synthetic.bars"},
        "--spread": {"help": "override data.synthetic.spread_pips"},
        "--fast-ema": {"type": int, "dest": "fast_ema"},
        "--slow-ema": {"type": int, "dest": "slow_ema"},
        "--trend-ema": {"type": int, "dest": "trend_ema"},
        "--rsi-period": {"type": int, "dest": "rsi_period"},
        "--reward-risk": {"type": float, "dest": "reward_risk"},
    }

    def add_common(target: argparse.ArgumentParser) -> None:
        for flag, kwargs in common.items():
            target.add_argument(flag, **kwargs)

    bt = sub.add_parser("backtest", help="run a historical simulation", parents=[globals_parent])
    add_common(bt)
    bt.add_argument("--symbols", nargs="*", help="symbols to test (defaults to all in config)")
    bt.add_argument("--no-reports", action="store_true", help="skip writing report files")
    bt.add_argument("--walk-forward", type=int, default=0, metavar="FOLDS",
                    help="split history into N folds and report stability")
    bt.add_argument("--portfolio", action="store_true",
                    help="trade every symbol on ONE shared account instead of one run per symbol")
    bt.set_defaults(func=cmd_backtest)

    pp = sub.add_parser("paper", help="live paper trading on real market data", parents=[globals_parent])
    add_common(pp)
    pp.add_argument("--symbols", nargs="*", help="symbols to trade")
    pp.add_argument("--poll", type=float, default=2.0, help="seconds between price polls")
    pp.add_argument("--max-bars", type=int, default=0, help="stop after N new bars (0 = run forever)")
    pp.add_argument("--max-idle-polls", type=int, default=0,
                    help="stop after N polls with no new bar (0 = never; useful on a closed market)")
    pp.set_defaults(func=cmd_paper)

    lv = sub.add_parser("live", help="real order routing via MetaTrader 5 (needs explicit opt-in)", parents=[globals_parent])
    add_common(lv)
    lv.add_argument("--symbols", nargs="*", help="symbols to trade")
    lv.add_argument("--poll", type=float, default=2.0, help="seconds between price polls")
    lv.add_argument("--max-bars", type=int, default=0, help="stop after N new bars (0 = run forever)")
    lv.add_argument("--max-idle-polls", type=int, default=0,
                    help="stop after N polls with no new bar (0 = never)")
    lv.add_argument("--live", action="store_true", help="confirm you intend real-money routing")
    lv.add_argument("--i-understand-the-risk", action="store_true", help="final acknowledgement")
    lv.set_defaults(func=cmd_live)

    dl = sub.add_parser("download", help="save bars to CSV for offline backtests", parents=[globals_parent])
    add_common(dl)
    dl.add_argument("--out", help="output directory (default: data)")
    dl.set_defaults(func=cmd_download)

    st = sub.add_parser("strategies", help="list registered strategies and their parameters", parents=[globals_parent])
    st.set_defaults(func=cmd_strategies)

    dr = sub.add_parser("doctor", help="check config, data and dependencies", parents=[globals_parent])
    dr.set_defaults(func=cmd_doctor)

    db = sub.add_parser(
        "dashboard",
        help="open the optional results dashboard in a browser (needs the `dashboard` extra)",
        parents=[globals_parent],
    )
    db.add_argument("--port", type=int, default=8501, help="port to serve on (default 8501)")
    db.add_argument("--address", default="0.0.0.0", help="interface to bind (default 0.0.0.0)")
    db.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
