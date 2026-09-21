#!/usr/bin/env python3
"""Programmatic example: use the package as a library.

    python scripts/example_backtest.py

Shows the five objects you need — config, data, broker, strategy, backtest — and
how to reach into the results afterwards. Everything the CLI does is available
here, so this is the starting point for your own research scripts.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scalper.backtest import run_backtest  # noqa: E402
from scalper.brokers import build_broker  # noqa: E402
from scalper.config import load_config  # noqa: E402
from scalper.data import build_feed  # noqa: E402
from scalper.metrics import monthly_returns  # noqa: E402
from scalper.strategies import get_strategy  # noqa: E402


def main() -> int:
    # 1. Config — typed, validated, overrides applied without touching the file.
    cfg = load_config(
        ROOT / "config" / "config.yaml",
        {
            "data.synthetic.bars": 120_000,
            "risk.risk_per_trade_pct": 0.25,
            "strategy.name": "ema_rsi_momentum",
        },
    )

    # 2. Data.
    frames = build_feed(cfg).load()
    symbol = cfg.symbols[0]
    df = frames[symbol]

    # 3. Broker + strategy (the broker is injected, so swapping in MT5 here
    #    would run the identical engine against a live account).
    broker = build_broker(cfg)
    strategy = get_strategy(cfg.strategy.name, symbol=symbol, **cfg.strategy.params)

    # 4. Run.
    result = run_backtest(cfg, broker, strategy, symbol, df)

    # 5. Read the results.
    m = result.metrics
    print(f"\n{symbol} / {strategy.name}")
    print("-" * 72)
    print(f"  {m.headline()}")
    print(f"  final balance : {result.final_balance:,.2f} {cfg.account.currency}")
    print(f"  total R       : {m.total_r:+.2f}R over {m.total_trades} trades")
    print(f"  worst streak  : {m.max_consecutive_losses} consecutive losses")
    print(f"  avg hold      : {m.average_duration_minutes:.1f} minutes")

    if result.trades:
        wins = [t for t in result.trades if t.is_win]
        print(f"  best trade    : {max(result.trades, key=lambda t: t.net_pnl).net_pnl:+,.2f}")
        print(f"  on the long side : {sum(1 for t in result.trades if t.direction.value == 'long')} trades")
        print(f"  win rate (long)  : {len([t for t in wins if t.direction.value == 'long']) / max(1, len([t for t in result.trades if t.direction.value == 'long'])) * 100:.1f}%")

        monthly = monthly_returns(result.equity_curve, initial_balance=cfg.account.initial_balance)
        if not monthly.empty:
            print("\n  monthly returns:")
            for row in monthly.itertuples():
                bar = "+" * int(abs(row.return_pct)) if row.return_pct > 0 else "-" * int(abs(row.return_pct))
                print(f"    {row.year}-{row.month:02d}  {row.return_pct:+7.2f}%  {bar}")

    print(f"\n  {result.engine_summary['engine']['entries_blocked']} entries were blocked; top reasons:")
    for reason, count in list(result.engine_summary["engine"]["block_reasons"].items())[:4]:
        print(f"    {count:>5}  {reason}")

    print("\n  (synthetic data — these numbers test the pipeline, not the strategy)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
