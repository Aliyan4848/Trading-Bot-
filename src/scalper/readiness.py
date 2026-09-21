"""Go-live readiness: does the evidence actually support risking money?

"Should I deploy this?" is not a question the code can answer with an opinion,
but it can answer it with a checklist evaluated against the artifacts you
already have in ``results/``. That is what this module does.

Two rules make it worth more than a pep talk:

1. **Synthetic data can never pass.** Results generated from a random process are
   a wiring test, not evidence, and no amount of favourable randomness changes
   that. Any run whose warnings mention synthetic data forces a failure.
2. **The typical result has to be profitable, not the best one.** Gating on the
   best run of a sweep is how people ship strategies that lose. The gates use the
   *median* run and require agreement across symbols.

Passing every check is necessary, not sufficient. It means "you have not
obviously fooled yourself yet", which is a much lower bar than "this will make
money" — the disclaimer at the end says so, and stays even on a pass.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Thresholds. Deliberately conservative; these are floors, not targets.
MIN_TRADES = 100          # fewer than this and a result is noise, not evidence
MIN_DAYS = 365            # at least a year of history, so one regime cannot carry it
MIN_SYMBOLS_POSITIVE = 2  # agreement across instruments
MIN_MEDIAN_PF = 1.10      # comfortably above 1.0 after costs
MIN_MEDIAN_EXPECTANCY_R = 0.02

PASS = "pass"
WARN = "warn"
FAIL = "fail"

_STATUS_ORDER = {FAIL: 0, WARN: 1, PASS: 2}
_MARK = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}


@dataclass(slots=True)
class Check:
    """One gate, the number behind it, and what to do about it."""

    key: str
    label: str
    status: str
    detail: str
    fix: str = ""


@dataclass(slots=True)
class ReadinessReport:
    checks: list[Check] = field(default_factory=list)
    disclaimer: str = (
        "Passing these gates means you have not obviously fooled yourself yet. It is not a "
        "prediction that the strategy makes money. Simulated results do not predict future "
        "results, and nothing here is financial advice."
    )

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def verdict(self) -> str:
        if self.failures:
            return "NOT READY"
        if self.warnings:
            return "PROBABLY NOT READY"
        return "READY FOR A SMALL PROBE (demo first)"

    def next_action(self) -> str:
        """The single most useful next step — the first gate that is not passing."""
        for check in sorted(self.checks, key=lambda c: _STATUS_ORDER[c.status]):
            if check.status is not PASS and check.fix:
                return check.fix
        return "Run the same configuration on a demo account for a few weeks and compare fills."

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "counts": {
                "pass": len(self.checks) - len(self.failures) - len(self.warnings),
                "warn": len(self.warnings),
                "fail": len(self.failures),
            },
            "checks": [
                {"key": c.key, "label": c.label, "status": c.status, "detail": c.detail, "fix": c.fix}
                for c in self.checks
            ],
            "next_action": self.next_action(),
            "disclaimer": self.disclaimer,
        }


# --------------------------------------------------------------------------- #
# Loading artifacts
# --------------------------------------------------------------------------- #
def load_result_summaries(results_dir: Path | str) -> list[dict[str, Any]]:
    """Read every ``*_report.json`` in a results directory into flat dicts.

    Unreadable or half-written files are skipped rather than raising: a readiness
    check that crashes on one bad file tells you nothing about the rest.
    """
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("*_report.json")):
        if path.name.startswith("portfolio_"):
            continue  # aggregate summary, not a run
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metrics = payload.get("metrics") or {}
        period = payload.get("period") or {}
        warnings = [str(w) for w in (payload.get("data_warnings") or [])]
        summaries.append(
            {
                "path": str(path),
                "symbol": str(payload.get("symbol") or "?"),
                "strategy": str(payload.get("strategy") or "?"),
                "trades": int(metrics.get("total_trades") or 0),
                "expectancy_r": float(metrics.get("expectancy_r") or 0.0),
                "profit_factor": float(metrics.get("profit_factor") or 0.0),
                "net_profit": float(metrics.get("net_profit") or 0.0),
                "max_drawdown_pct": float(metrics.get("max_drawdown_pct") or 0.0),
                "days": float(period.get("days") or 0.0),
                "bars": int(period.get("bars") or 0),
                "synthetic": any("synthetic" in w.lower() for w in warnings),
                "warnings": warnings,
            }
        )
    return summaries


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #
def _check_results_exist(runs: list[dict[str, Any]]) -> Check:
    if not runs:
        return Check(
            "results",
            "Backtest evidence exists",
            FAIL,
            "No results/*_report.json found — there is nothing to judge. The evidence gates "
            "(sample size, expectancy, profit factor, consistency) are not shown because no run "
            "can answer them yet.",
            "python run.py backtest",
        )
    return Check("results", "Backtest evidence exists", PASS, f"{len(runs)} run(s) found.")


def _check_real_data(runs: list[dict[str, Any]]) -> Check:
    synthetic = [r for r in runs if r["synthetic"]]
    if synthetic and len(synthetic) == len(runs):
        return Check(
            "data",
            "History is real market data",
            FAIL,
            f"All {len(runs)} run(s) are on synthetic data. Generated bars validate that the "
            "pipeline runs; they say nothing about whether the strategy has an edge.",
            "Export real M1 history (see docs/getting-data.md), set data.source: csv, and re-run.",
        )
    if synthetic:
        return Check(
            "data",
            "History is real market data",
            FAIL,
            f"{len(synthetic)} of {len(runs)} run(s) are synthetic; mixing them makes the "
            "aggregate meaningless.",
            "Delete the synthetic runs or move them to another directory, then re-run.",
        )
    return Check("data", "History is real market data", PASS, "All runs used non-synthetic data.")


def _check_sample_size(runs: list[dict[str, Any]]) -> Check:
    trades = sum(r["trades"] for r in runs)
    if trades < 30:
        return Check(
            "trades",
            "Enough trades to mean anything",
            FAIL,
            f"{trades} trades total. Below ~30 a result is a story about a handful of outcomes.",
            "Backtest a longer history or more symbols before reading the numbers.",
        )
    if trades < MIN_TRADES:
        return Check(
            "trades",
            f"Enough trades to mean anything (>={MIN_TRADES})",
            WARN,
            f"{trades} trades total — thin. A single unlucky month could flip the conclusion.",
            "Backtest at least a year of history across several symbols.",
        )
    return Check("trades", f"Enough trades to mean anything (>={MIN_TRADES})", PASS, f"{trades} trades.")


def _check_history_span(runs: list[dict[str, Any]]) -> Check:
    days = max((r["days"] for r in runs), default=0.0)
    if days < 90:
        return Check(
            "span",
            f"History covers a real cycle (>={MIN_DAYS} days)",
            FAIL,
            f"Longest run spans {days:.0f} days. One regime is not a sample of regimes.",
            "Backtest at least a year, ideally two, spanning trending and ranging periods.",
        )
    if days < MIN_DAYS:
        return Check(
            "span",
            f"History covers a real cycle (>={MIN_DAYS} days)",
            WARN,
            f"Longest run spans {days:.0f} days — under a year, so it may be one kind of market.",
            "Extend the history to at least a year before trusting the result.",
        )
    return Check("span", f"History covers a real cycle (>={MIN_DAYS} days)", PASS, f"{days:.0f} days.")


def _check_expectancy(runs: list[dict[str, Any]]) -> Check:
    values = [r["expectancy_r"] for r in runs]
    median = statistics.median(values)
    positive = sum(1 for v in values if v > 0)
    if median <= 0:
        return Check(
            "expectancy",
            "Profitable after spread and commission",
            FAIL,
            f"Median expectancy is {median:+.3f}R across {len(values)} run(s); {positive} run(s) "
            "are positive. The typical configuration loses money per trade.",
            "Do not deploy. Change the strategy or the market — not the lot size.",
        )
    if median < MIN_MEDIAN_EXPECTANCY_R:
        return Check(
            "expectancy",
            f"Profitable after costs (median >= {MIN_MEDIAN_EXPECTANCY_R}R)",
            WARN,
            f"Median expectancy is only {median:+.4f}R — smaller than the modelling error in "
            "fills, spread widening and slippage.",
            "Treat this as no edge until the margin is wider than the assumptions.",
        )
    return Check(
        "expectancy",
        f"Profitable after costs (median >= {MIN_MEDIAN_EXPECTANCY_R}R)",
        PASS,
        f"Median expectancy {median:+.4f}R across {len(values)} run(s).",
    )


def _check_profit_factor(runs: list[dict[str, Any]]) -> Check:
    values = [r["profit_factor"] for r in runs if r["profit_factor"] > 0]
    if not values:
        return Check(
            "pf",
            f"Profit factor above {MIN_MEDIAN_PF}",
            FAIL,
            "No run reported a usable profit factor.",
            "Re-run the backtest; check the config and data source.",
        )
    median = statistics.median(values)
    if median < 1.0:
        return Check(
            "pf",
            f"Profit factor above {MIN_MEDIAN_PF}",
            FAIL,
            f"Median profit factor is {median:.2f} — less money won than lost.",
            "Do not deploy; a PF below 1.0 is a losing system by definition.",
        )
    if median < MIN_MEDIAN_PF:
        return Check(
            "pf",
            f"Profit factor above {MIN_MEDIAN_PF}",
            WARN,
            f"Median profit factor is {median:.2f} — above break-even but with almost no margin "
            "for the costs a live account adds.",
            "Improve the edge or reduce trade frequency, then re-test out of sample.",
        )
    return Check("pf", f"Profit factor above {MIN_MEDIAN_PF}", PASS, f"Median profit factor {median:.2f}.")


def _check_consistency(runs: list[dict[str, Any]]) -> Check:
    """One symbol carrying everything is not an edge, it is a coincidence."""
    per_symbol: dict[str, float] = {}
    for run in runs:
        per_symbol.setdefault(run["symbol"], run["expectancy_r"])
    positive = [s for s, v in per_symbol.items() if v > 0]
    if not positive:
        return Check(
            "consistency",
            f"Edge shows on >={MIN_SYMBOLS_POSITIVE} symbols",
            FAIL,
            f"No symbol is profitable ({len(per_symbol)} tested).",
            "There is no edge to deploy.",
        )
    if len(positive) < MIN_SYMBOLS_POSITIVE:
        return Check(
            "consistency",
            f"Edge shows on >={MIN_SYMBOLS_POSITIVE} symbols",
            WARN,
            f"Only {positive[0]} is profitable out of {len(per_symbol)} symbols tested — that is "
            "as likely to be that symbol's month as a repeatable edge.",
            "Test the same rules across more symbols and timeframes before believing it.",
        )
    return Check(
        "consistency",
        f"Edge shows on >={MIN_SYMBOLS_POSITIVE} symbols",
        PASS,
        f"{len(positive)}/{len(per_symbol)} symbols profitable: {', '.join(sorted(positive))}.",
    )


def _check_out_of_sample(runs: list[dict[str, Any]], results_dir: Path) -> Check:
    sweeps = list(results_dir.glob("sweep_*.csv")) if results_dir.is_dir() else []
    strategies = {r["strategy"] for r in runs}
    if sweeps:
        return Check(
            "oos",
            "Checked out of sample",
            PASS,
            f"{len(sweeps)} sweep file(s) present, which carry fold results.",
        )
    if len(strategies) > 1:
        return Check(
            "oos",
            "Checked out of sample",
            WARN,
            f"{len(strategies)} strategies compared, but no walk-forward or fold evidence found.",
            "python scripts/optimize.py --folds 4  (the top row of a sweep is the most overfitted one).",
        )
    return Check(
        "oos",
        "Checked out of sample",
        WARN,
        "No fold or sweep evidence: every result here may be an in-sample fit.",
        "python scripts/optimize.py --folds 4",
    )


def _check_drawdown(runs: list[dict[str, Any]], limit_pct: float | None) -> Check:
    worst = max((r["max_drawdown_pct"] for r in runs), default=0.0)
    if limit_pct is None or limit_pct <= 0:
        return Check("dd", "Drawdown inside the configured limit", WARN, f"Worst {worst:.2f}%; no limit configured.", "Set risk.max_drawdown_pct.")
    if worst > limit_pct:
        return Check(
            "dd",
            "Drawdown inside the configured limit",
            WARN,
            f"Worst run drew down {worst:.2f}% against a {limit_pct:.2f}% limit. The kill switch "
            "is evaluated on bar closes, so it can overshoot by one bar's move.",
            "Size positions so the account survives the observed overshoot, not the limit.",
        )
    return Check(
        "dd",
        "Drawdown inside the configured limit",
        PASS,
        f"Worst run drew down {worst:.2f}% of {limit_pct:.2f}% allowed.",
    )


def _check_live_gates(cfg: Any) -> Check:
    """Informational: arming the switches is not evidence, but leaving them off is safe."""
    from .config import is_live_allowed

    mode = str(getattr(getattr(cfg, "broker", None), "mode", "paper")).lower()
    env_ok = is_live_allowed()
    if mode == "mt5" or env_ok:
        return Check(
            "gates",
            "Live routing switches",
            WARN,
            f"Live routing is ARMED (broker.mode={mode}, SCALPER_ALLOW_LIVE={'yes' if env_ok else 'no'}). "
            "Arming the gates is not evidence — the gates exist to stop accidents, not to judge edge.",
            "Leave the gates off until every other check passes.",
        )
    return Check(
        "gates",
        "Live routing switches",
        PASS,
        "Live routing is off (broker.mode=paper, SCALPER_ALLOW_LIVE unset) — nothing can place a real order.",
    )


def assess_readiness(
    cfg: Any,
    results_dir: Path | str | None = None,
    runs: list[dict[str, Any]] | None = None,
) -> ReadinessReport:
    """Evaluate every gate against the artifacts on disk."""
    directory = Path(results_dir or getattr(cfg.reporting, "output_dir", "results"))
    if runs is None:
        runs = load_result_summaries(directory)

    limit = float(getattr(getattr(cfg, "risk", None), "max_drawdown_pct", 0.0) or 0.0)
    checks = [
        _check_results_exist(runs),
        _check_real_data(runs) if runs else None,
        _check_sample_size(runs) if runs else None,
        _check_history_span(runs) if runs else None,
        _check_expectancy(runs) if runs else None,
        _check_profit_factor(runs) if runs else None,
        _check_consistency(runs) if runs else None,
        _check_out_of_sample(runs, directory) if runs else None,
        _check_drawdown(runs, limit) if runs else None,
        _check_live_gates(cfg),
    ]
    return ReadinessReport(checks=[c for c in checks if c is not None])


def format_report(report: ReadinessReport) -> str:
    """Plain-text rendering for the terminal."""
    width = max((len(c.label) + 2 for c in report.checks), default=40)
    lines = ["", "Go-live readiness", "=" * 96]
    for check in report.checks:
        lines.append(f"  [{_MARK[check.status]}] {check.label:<{width}} {check.detail}")
        if check.fix and check.status is not PASS:
            lines.append(f"         {'':<{width}} -> {check.fix}")
    passed = len(report.checks) - len(report.failures) - len(report.warnings)
    lines += [
        "=" * 96,
        f"  {report.verdict}  ({passed} pass, {len(report.warnings)} warn, {len(report.failures)} fail)",
        f"  Next: {report.next_action()}",
        "",
        f"  {report.disclaimer}",
        "",
    ]
    return "\n".join(lines)
