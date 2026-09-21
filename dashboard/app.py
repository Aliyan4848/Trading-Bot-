#!/usr/bin/env python3
"""Optional dashboard: read what `scalper backtest` wrote into `results/`.

    pip install ".[dashboard]"
    python run.py dashboard            # or: streamlit run dashboard/app.py

This is a *reader*. It never re-runs a strategy and never talks to a broker, so
it cannot show you anything the backtest did not already produce. Everything it
draws comes from the files in `results/`:

    <symbol>_<strategy>_<timeframe>_report.json   headline metrics + config
    <symbol>_<strategy>_<timeframe>_trades.csv    every closed trade
    <symbol>_<strategy>_<timeframe>_equity.csv    downsampled equity curve

Run a backtest first; if `results/` is empty the app tells you the exact command
instead of showing an empty page.

The data-loading functions below are plain Python (no Streamlit calls), so they
can be unit tested — see `tests/test_dashboard.py`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

DEFAULT_RESULTS = ROOT / "results"

EXIT_REASON_HELP = {
    "stop_loss": "Protective stop was hit (-1R by construction).",
    "take_profit": "Target was reached.",
    "signal": "The strategy asked to close (e.g. an opposite signal).",
    "session_close": "Flat-at-close rule fired before the weekend/session end.",
    "trailing_stop": "Trailing stop was hit.",
    "kill_switch": "Daily/weekly loss limit stopped trading for the day.",
    "end_of_data": "The run ended with the position still open; closed at the last price.",
    "manual": "Closed by hand (live/paper only).",
}


@dataclass
class Run:
    """One backtest result set on disk."""

    symbol: str
    strategy: str
    timeframe: str
    stem: str
    metrics: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)
    account: dict = field(default_factory=dict)
    period: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    trades: pd.DataFrame | None = None
    equity: pd.DataFrame | None = None
    path: Path | None = None

    @property
    def label(self) -> str:
        return f"{self.symbol} · {self.strategy} · {self.timeframe}"

    @property
    def net_profit(self) -> float:
        return float(self.metrics.get("net_profit", 0.0) or 0.0)

    @property
    def return_pct(self) -> float:
        return float(self.metrics.get("return_pct", 0.0) or 0.0)

    @property
    def total_trades(self) -> int:
        return int(self.metrics.get("total_trades", 0) or 0)

    @property
    def is_synthetic(self) -> bool:
        joined = " ".join(str(w).lower() for w in self.warnings)
        return "synthetic" in joined


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def discover(results_dir: Path | str = DEFAULT_RESULTS) -> list[Path]:
    """Report JSONs in `results/`, newest first, ignoring portfolio summaries."""
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        return []
    files = [p for p in results_dir.glob("*_report.json") if not p.name.startswith("portfolio")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def load_run(report_path: Path | str) -> Run:
    """Load one report plus its trades/equity siblings (whichever exist)."""
    report_path = Path(report_path)
    payload = _read_json(report_path)
    stem = report_path.name[: -len("_report.json")]
    run = Run(
        symbol=str(payload.get("symbol") or stem.split("_")[0]),
        strategy=str(payload.get("strategy") or "?"),
        timeframe=str(payload.get("timeframe") or "?"),
        stem=stem,
        metrics=dict(payload.get("metrics") or {}),
        engine=dict(payload.get("engine") or {}),
        account=dict(payload.get("account") or {}),
        period=dict(payload.get("period") or {}),
        warnings=list(payload.get("data_warnings") or []),
        path=report_path,
    )

    trades_path = report_path.with_name(f"{stem}_trades.csv")
    if trades_path.is_file():
        frame = pd.read_csv(trades_path)
        if "entry_time" in frame:
            frame["entry_time"] = pd.to_datetime(frame["entry_time"], errors="coerce", utc=True)
        if "exit_time" in frame:
            frame["exit_time"] = pd.to_datetime(frame["exit_time"], errors="coerce", utc=True)
        run.trades = frame

    equity_path = report_path.with_name(f"{stem}_equity.csv")
    if equity_path.is_file():
        equity = pd.read_csv(equity_path)
        if "time" in equity:
            equity["time"] = pd.to_datetime(equity["time"], errors="coerce", utc=True)
        run.equity = equity

    return run


def load_runs(results_dir: Path | str = DEFAULT_RESULTS) -> list[Run]:
    return [load_run(p) for p in discover(results_dir)]


def comparison_table(runs: list[Run]) -> pd.DataFrame:
    """One row per run — the same columns the CLI prints, plus file names."""
    rows = []
    for r in runs:
        rows.append(
            {
                "symbol": r.symbol,
                "strategy": r.strategy,
                "timeframe": r.timeframe,
                "trades": r.total_trades,
                "net_profit": r.net_profit,
                "return_pct": r.return_pct,
                "win_rate_pct": float(r.metrics.get("win_rate_pct", 0.0) or 0.0),
                "profit_factor": float(r.metrics.get("profit_factor", 0.0) or 0.0),
                "expectancy_r": float(r.metrics.get("expectancy_r", 0.0) or 0.0),
                "max_drawdown_pct": float(r.metrics.get("max_drawdown_pct", 0.0) or 0.0),
                "sharpe": float(r.metrics.get("sharpe", 0.0) or 0.0),
                "synthetic": r.is_synthetic,
            }
        )
    return pd.DataFrame(rows)


def equity_frame(runs: list[Run]) -> pd.DataFrame:
    """Long-form equity curve for charting: time / equity / run label.

    Each run is rebased to its own starting equity so runs with different
    account sizes stay comparable on one axis.
    """
    frames = []
    for r in runs:
        if r.equity is None or r.equity.empty or "equity" not in r.equity:
            continue
        frame = r.equity[["time", "equity"]].copy()
        frame["run"] = r.label
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["time", "equity", "run"])
    return pd.concat(frames, ignore_index=True)


def monthly_returns_frame(runs: list[Run]) -> pd.DataFrame:
    """Monthly returns pulled back out of the stored equity curves."""
    from scalper.metrics import monthly_returns
    from scalper.models import AccountSnapshot

    frames = []
    for r in runs:
        if r.equity is None or r.equity.empty or "equity" not in r.equity:
            continue
        curve = [
            AccountSnapshot(time=row.time.to_pydatetime(), balance=float(row.balance), equity=float(row.equity))
            for row in r.equity.itertuples()
            if pd.notna(row.time)
        ]
        frame = monthly_returns(curve, initial_balance=float(r.account.get("initial_balance") or 0) or None)
        if frame.empty:
            continue
        frame = frame.copy()
        frame["run"] = r.label
        frame["period"] = frame["year"].astype(str) + "-" + frame["month"].astype(str).str.zfill(2)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["year", "month", "return_pct", "run", "period"])
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
# Streamlit UI (not executed on import, so the helpers above stay testable)
# --------------------------------------------------------------------------- #
def render() -> None:  # pragma: no cover - exercised by `streamlit run`, not pytest
    import altair as alt
    import streamlit as st

    st.set_page_config(page_title="fx-scalper results", page_icon="📉", layout="wide")
    st.title("fx-scalper — backtest results")
    st.caption(
        "Read-only view of `results/`. Nothing here re-runs a strategy or contacts a broker, "
        "so whatever you see is exactly what the backtest produced."
    )

    with st.sidebar:
        st.header("Results folder")
        results_dir = st.text_input("Path", value=str(DEFAULT_RESULTS))
        runs = load_runs(results_dir)
        if not runs:
            st.warning("No reports found.")
        if runs:
            st.header("Runs")
            labels = {r.label: r for r in runs}
            chosen = st.multiselect("Show", list(labels), default=list(labels)[:1])
            runs = [labels[c] for c in chosen] or runs
            st.caption(f"{len(runs)} of {len(labels)} selected")

    if not runs:
        st.error(
            "Nothing in `results/` yet.\n\n"
            "```bash\npython run.py backtest --config config/config.yaml\n```\n"
            "Then reload this page."
        )
        return

    if any(r.is_synthetic for r in runs):
        st.warning(
            "**Synthetic data.** These numbers prove the pipeline works end to end; they say "
            "nothing about whether the strategy makes money. Point `data.source` at real CSVs "
            "or MT5 history before drawing conclusions.",
            icon="⚠️",
        )

    table = comparison_table(runs)
    overview_tab, equity_tab, trades_tab, notes_tab = st.tabs(
        ["Overview", "Equity", "Trades", "Data & config"]
    )

    with overview_tab:
        total_net = table["net_profit"].sum()
        total_trades = int(table["trades"].sum())
        winning = int((table["net_profit"] > 0).sum())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Runs", len(table))
        c2.metric("Trades", f"{total_trades:,}")
        c3.metric("Net across runs", f"{total_net:,.2f}")
        c4.metric("Profitable runs", f"{winning}/{len(table)}")

        st.subheader("Head to head")
        st.dataframe(
            table,
            width="stretch",
            hide_index=True,
            column_config={
                "net_profit": st.column_config.NumberColumn("net", format="%.2f"),
                "return_pct": st.column_config.NumberColumn("return %", format="%.2f"),
                "expectancy_r": st.column_config.NumberColumn("exp R", format="%.3f"),
                "max_drawdown_pct": st.column_config.NumberColumn("max DD %", format="%.2f"),
                "synthetic": st.column_config.CheckboxColumn("synthetic"),
            },
        )
        st.caption(
            "`exp R` is expectancy per trade measured against the *initial* stop, so -1.000 is "
            "exactly a stop-out and a number below -0.10 on hundreds of trades is a real edge "
            "problem, not noise."
        )

        monthly = monthly_returns_frame(runs)
        if not monthly.empty:
            st.subheader("Monthly returns")
            chart = (
                alt.Chart(monthly)
                .mark_bar()
                .encode(
                    x=alt.X("period:N", title="month", axis=alt.Axis(labelAngle=-45)),
                    y=alt.Y("return_pct:Q", title="return %"),
                    color=alt.condition(
                        alt.datum.return_pct > 0, alt.value("#2e7d32"), alt.value("#c62828")
                    ),
                    column=alt.Column("run:N", title=None) if len(runs) > 1 else alt.Undefined,
                    tooltip=["run:N", "period:N", alt.Tooltip("return_pct:Q", format=".2f")],
                )
                .properties(height=260)
            )
            st.altair_chart(chart, width="stretch")
            st.caption(
                "The first month is measured against the account's starting balance; months with "
                "no trading activity are simply absent rather than shown as flat."
            )

    with equity_tab:
        for run in runs:
            st.subheader(run.label)
            if run.equity is None or run.equity.empty:
                st.info("No equity CSV for this run (`reporting.write_equity_csv` may be off).")
                continue
            curve = run.equity.rename(columns={"time": "time", "equity": "equity"})
            c1, c2 = st.columns([2, 1])
            with c1:
                line = (
                    alt.Chart(curve)
                    .mark_line(strokeWidth=1.4)
                    .encode(
                        x=alt.X("time:T", title=None),
                        y=alt.Y("equity:Q", title="equity", scale=alt.Scale(zero=False)),
                        tooltip=[alt.Tooltip("time:T"), alt.Tooltip("equity:Q", format=",.2f")],
                    )
                    .properties(height=280, title="Equity (mark-to-market)")
                )
                st.altair_chart(line, width="stretch")
            with c2:
                if "balance" in curve:
                    bal = (
                        alt.Chart(curve)
                        .mark_line(strokeWidth=1.4)
                        .encode(
                            x=alt.X("time:T", title=None),
                            y=alt.Y("balance:Q", title="balance", scale=alt.Scale(zero=False)),
                        )
                        .properties(height=280, title="Closed-trade balance")
                    )
                    st.altair_chart(bal, width="stretch")
            peak = curve["equity"].cummax()
            drawdown = (curve["equity"] / peak - 1.0) * 100.0
            dd = pd.DataFrame({"time": curve["time"], "drawdown_pct": drawdown})
            area = (
                alt.Chart(dd)
                .mark_area(color="#c62828", opacity=0.5)
                .encode(
                    x=alt.X("time:T", title=None),
                    y=alt.Y("drawdown_pct:Q", title="drawdown %"),
                    tooltip=[alt.Tooltip("drawdown_pct:Q", format=".2f")],
                )
                .properties(height=170, title="Drawdown from peak")
            )
            st.altair_chart(area, width="stretch")
            st.caption(
                "The equity CSV is downsampled to at most 20,000 rows (it always keeps the final "
                "row); headline metrics are computed on the full, undownsampled curve."
            )

    with trades_tab:
        for run in runs:
            st.subheader(run.label)
            trades = run.trades
            if trades is None or trades.empty:
                st.info("No trades were taken in this run.")
                continue
            left, right = st.columns([2, 3])
            with left:
                if "exit_reason" in trades:
                    counts = trades["exit_reason"].value_counts().rename_axis("exit_reason").reset_index(name="trades")
                    bar = (
                        alt.Chart(counts)
                        .mark_bar()
                        .encode(
                            y=alt.Y("exit_reason:N", sort="-x", title=None),
                            x=alt.X("trades:Q", title="trades"),
                            tooltip=["exit_reason:N", "trades:Q"],
                        )
                        .properties(height=240, title="How trades ended")
                    )
                    st.altair_chart(bar, width="stretch")
            with right:
                if "r_multiple" in trades:
                    hist = (
                        alt.Chart(trades)
                        .mark_bar()
                        .encode(
                            x=alt.X("r_multiple:Q", bin=alt.Bin(step=0.25), title="R multiple"),
                            y=alt.Y("count():Q", title="trades"),
                            tooltip=[alt.Tooltip("count():Q")],
                        )
                        .properties(height=240, title="Outcome distribution in R")
                    )
                    st.altair_chart(hist, width="stretch")
            st.caption(
                "Stops are placed so a clean stop-out is -1.0R against the *initial* stop. "
                "Values below -1.05 only appear when price gaps through the stop."
            )
            st.dataframe(trades, width="stretch", hide_index=True, height=320)
            st.download_button(
                "Download trades CSV",
                data=trades.to_csv(index=False).encode("utf-8"),
                file_name=f"{run.stem}_trades.csv",
                mime="text/csv",
            )

    with notes_tab:
        for run in runs:
            st.subheader(run.label)
            cols = st.columns(3)
            with cols[0]:
                st.markdown("**Account & period**")
                st.json({"account": run.account, "period": run.period}, expanded=False)
            with cols[1]:
                st.markdown("**Engine activity**")
                st.json(run.engine, expanded=False)
            with cols[2]:
                st.markdown("**Warnings**")
                if run.warnings:
                    for w in run.warnings:
                        st.warning(w)
                else:
                    st.success("No data warnings.")
            st.markdown("**Exit reasons**")
            st.table(
                pd.DataFrame(
                    [{"reason": k, "meaning": v} for k, v in EXIT_REASON_HELP.items()]
                )
            )

    st.divider()
    st.caption(
        "Simulated results on historical or synthetic data do not predict future results. "
        "This dashboard only displays files; it is not connected to a broker and cannot place "
        "an order. Nothing here is financial advice."
    )


if __name__ == "__main__" or __name__.startswith("__streamlit"):
    render()
