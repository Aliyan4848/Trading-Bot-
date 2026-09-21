#!/usr/bin/env python3
"""Parameter sweep across a strategy's parameters.

    python scripts/optimize.py --strategy ema_rsi_momentum --symbol EURUSD \
        --param fast_ema=5,9,13 --param slow_ema=21,34 --param reward_risk=1.5,2.0,2.5

Two warnings, both learned the hard way:

1. **The best parameter set is usually the most overfitted.** With 18 combinations and a few hundred
   trades, the winner is mostly luck. Read the *distribution* of results: if nearby parameter values
   give wildly different outcomes, the "edge" is noise.
2. **Out-of-sample matters more than in-sample.** `--folds 3` splits the history and reports the
   best combination's behaviour in each slice. A combination that wins in slice 1 and loses in
   slices 2-3 has told you everything you need to know.

Results are written to `results/sweep_<strategy>_<symbol>_<stamp>.csv`.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scalper.backtest import run_backtest  # noqa: E402
from scalper.brokers import build_broker  # noqa: E402
from scalper.config import load_config  # noqa: E402
from scalper.data import build_feed  # noqa: E402
from scalper.strategies import get_strategy  # noqa: E402


def parse_params(pairs: list[str]) -> dict[str, list[str]]:
    """Turn ['fast_ema=5,9', 'reward_risk=1.5,2.0'] into {name: [values]}."""
    grid: dict[str, list[str]] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param expects name=v1,v2,... (got {pair!r})")
        name, _, values = pair.partition("=")
        grid[name.strip()] = [v.strip() for v in values.split(",") if v.strip()]
    return grid


def coerce(raw: str):
    """Best-effort typing so '9' becomes an int and 'true' a bool."""
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid-search a strategy's parameters.")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--symbol", default=None, help="defaults to the first instrument")
    parser.add_argument("--param", action="append", default=[], metavar="NAME=V1,V2,...")
    parser.add_argument("--folds", type=int, default=1, help="also report out-of-sample slices")
    parser.add_argument("--min-trades", type=int, default=20, help="flag results below this")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if not args.param:
        raise SystemExit("Give at least one --param, e.g. --param fast_ema=5,9,13")

    base = load_config(args.config)
    symbol = (args.symbol or base.symbols[0]).upper()
    frame = build_feed(base).load()[symbol]

    grid = parse_params(args.param)
    unknown = set(grid) - set(get_strategy(args.strategy, symbol=symbol).default_params())
    if unknown:
        raise SystemExit(
            f"Unknown parameter(s) for {args.strategy}: {sorted(unknown)}\n"
            f"Run `python run.py strategies` for the full list."
        )

    names = list(grid)
    combos = list(itertools.product(*(grid[name] for name in names)))
    print(f"\n{args.strategy} on {symbol}: {len(combos)} combinations × {len(frame):,} bars")
    print(f"Splitting history into {args.folds} slice(s) for stability checking\n")

    rows = []
    started = time.perf_counter()
    for i, combo in enumerate(combos, 1):
        params = {name: coerce(raw) for name, raw in zip(names, combo, strict=True)}
        label = ", ".join(f"{k}={v}" for k, v in params.items())

        row: dict[str, object] = dict(params)
        try:
            strategy = get_strategy(args.strategy, symbol=symbol, **params)
        except ValueError as exc:
            print(f"  [{i:>3}/{len(combos)}] skipped ({exc})")
            continue

        # In-sample: the whole history.
        result = run_backtest(base, build_broker(base), strategy, symbol, frame)
        m = result.metrics
        row.update(
            trades=m.total_trades,
            net=m.net_profit,
            return_pct=m.return_pct,
            win_rate=m.win_rate_pct,
            profit_factor=None if m.profit_factor == float("inf") else m.profit_factor,
            expectancy_r=m.expectancy_r,
            max_dd_pct=m.max_drawdown_pct,
            sharpe=m.sharpe,
            low_sample=m.total_trades < args.min_trades,
        )

        # Out-of-sample slices: does the same parameter set survive elsewhere?
        if args.folds > 1:
            size = len(frame) // args.folds
            for fold in range(args.folds):
                chunk = frame.iloc[fold * size : (fold + 1) * size]
                if len(chunk) < base.backtest.warmup_bars + 100:
                    continue
                fold_result = run_backtest(
                    base, build_broker(base), get_strategy(args.strategy, symbol=symbol, **params),
                    symbol, chunk,
                )
                row[f"fold{fold + 1}_r"] = fold_result.metrics.expectancy_r
                row[f"fold{fold + 1}_trades"] = fold_result.metrics.total_trades

        rows.append(row)
        print(
            f"  [{i:>3}/{len(combos)}] {label:<52} "
            f"trades={m.total_trades:>4} expR={m.expectancy_r:+.3f} "
            f"net={m.net_profit:+9.2f} DD={m.max_drawdown_pct:5.2f}%"
        )

    if not rows:
        print("No valid combinations.")
        return 1

    frame_out = pd.DataFrame(rows).sort_values("expectancy_r", ascending=False)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out or base.reporting.output_dir) / f"sweep_{args.strategy}_{symbol}_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame_out.to_csv(out_path, index=False)

    elapsed = time.perf_counter() - started
    print(f"\n{'=' * 110}")
    print("WARNING: the top row is the most overfitted row. Look at the spread, not the winner.")
    print(f"{'=' * 110}")
    print(frame_out.head(10).to_string(index=False, max_colwidth=40))
    print(f"\n{len(frame_out)} combinations in {elapsed:.1f}s -> {out_path}")
    print(
        f"Median expectancy {frame_out['expectancy_r'].median():+.3f}R, "
        f"best {frame_out['expectancy_r'].max():+.3f}R, "
        f"worst {frame_out['expectancy_r'].min():+.3f}R"
    )
    flagged = int(frame_out["low_sample"].sum())
    if flagged:
        print(f"{flagged} combination(s) produced fewer than {args.min_trades} trades — ignore those.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
