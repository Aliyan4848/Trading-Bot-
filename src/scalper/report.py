"""Reporting: JSON, CSV, Markdown, and a self-contained HTML report.

No plotting library required. The charts are hand-built SVG, which keeps the
HTML report a single portable file you can open offline, email, or drop into a
repo — and it means `pip install matplotlib` is never a prerequisite for
reading your own results.
"""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import AppConfig
from .metrics import compute_metrics, drawdown_series
from .models import AccountSnapshot, Trade

log = logging.getLogger("scalper.report")

SYNTHETIC_BANNER = (
    "**These results come from SYNTHETIC data.** The generator produces plausible "
    "price action, but it is not the market. Treat every number below as a test of the "
    "pipeline, not as evidence that the strategy makes money. Re-run with real M1 "
    "history (`data.source: csv` or `mt5`) before drawing conclusions."
)


# -----------------------------------------------------------------------------
# File writers
# -----------------------------------------------------------------------------
def write_json(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    return path


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):  # Enum
        return value.value
    return str(value)


def write_equity_csv(equity_curve: Sequence[AccountSnapshot], path: Path, max_rows: int = 20_000) -> Path:
    """Write the equity curve, evenly downsampled if it is very long.

    Metrics are computed on the FULL curve; only the file is sampled, so a huge
    M1 run does not produce a 30 MB CSV.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not equity_curve:
        path.write_text("time,balance,equity,unrealized,open_positions\n", encoding="utf-8")
        return path
    frame = pd.DataFrame(
        {
            "time": [s.time for s in equity_curve],
            "balance": [round(s.balance, 2) for s in equity_curve],
            "equity": [round(s.equity, 2) for s in equity_curve],
            "unrealized": [round(s.unrealized, 2) for s in equity_curve],
            "open_positions": [s.open_positions for s in equity_curve],
        }
    )
    if len(frame) > max_rows:
        step = int(np.ceil(len(frame) / max_rows))
        sampled = frame.iloc[::step]
        # Always keep the true final row: `frame.iloc[::step]` stops at the last
        # multiple of `step`, so the run's closing equity could be dropped
        # whenever the curve length is not an exact multiple.
        frame = pd.concat([sampled, frame.iloc[[-1]]])
        frame = frame[~frame.index.duplicated(keep="first")]
    frame.to_csv(path, index=False)
    return path


def write_trades_csv(trades: Sequence[Trade], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not trades:
        path.write_text("no trades\n", encoding="utf-8")
        return path
    rows = [
        {
            "entry_time": t.entry_time.isoformat(),
            "exit_time": t.exit_time.isoformat(),
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
            "duration_minutes": round(t.duration_minutes, 2),
        }
        for t in trades
    ]
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def report_warnings(result: Any, synthetic: bool = False) -> list[str]:
    """The warning list a report should carry, de-duplicated.

    ``synthetic=True`` is the caller telling us the data was generated. Since
    :attr:`BacktestResult.data_warnings` already contains that banner for
    synthetic runs, this only adds it when it is missing — otherwise the same
    warning would be printed twice.
    """
    warnings = list(getattr(result, "data_warnings", []) or [])
    if synthetic and not any("synthetic" in w.lower() for w in warnings):
        warnings.insert(0, SYNTHETIC_BANNER)
    return warnings


# -----------------------------------------------------------------------------
# Markdown
# -----------------------------------------------------------------------------
def build_markdown(result: Any, *, synthetic: bool = False) -> str:
    m = result.metrics
    d = result.to_dict()
    warnings = report_warnings(result, synthetic)
    lines: list[str] = []
    add = lines.append

    add(f"# Backtest report — {result.symbol} {result.strategy}")
    add("")
    for warning in warnings:
        if "synthetic" in warning.lower():
            add(f"> [!WARNING]\n> {warning}\n")
    add(f"- **Timeframe:** {result.timeframe}")
    add(f"- **Period:** {d['period']['start']} → {d['period']['end']} ({d['period']['days']} days, {result.bars:,} bars)")
    add(f"- **Initial balance:** {result.initial_balance:,.2f}")
    add(f"- **Final balance:** {result.final_balance:,.2f} ({m.net_profit:+,.2f})")
    add(f"- **Runtime:** {result.runtime_sec:.2f}s")
    other_warnings = [w for w in warnings if "synthetic" not in w.lower()]
    if other_warnings:
        add("")
        add("**Data warnings**")
        for warning in other_warnings:
            add(f"- {warning}")
    add("")
    add("## Headline")
    add("")
    add("| Metric | Value |")
    add("| --- | --- |")
    for label, value in [
        ("Net profit", f"{m.net_profit:+,.2f}"),
        ("Return", f"{m.return_pct:+.2f}%"),
        ("CAGR", f"{m.cagr_pct:+.2f}%"),
        ("Max drawdown", f"{m.max_drawdown:,.2f} ({m.max_drawdown_pct:.2f}%)"),
        ("Sharpe", f"{m.sharpe:.2f}"),
        ("Sortino", f"{m.sortino:.2f}"),
        ("Calmar", f"{m.calmar:.2f}"),
    ]:
        add(f"| {label} | {value} |")

    add("")
    add("## Trades")
    add("")
    add("| Metric | Value |")
    add("| --- | --- |")
    pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    for label, value in [
        ("Total trades", f"{m.total_trades}"),
        ("Wins / losses", f"{m.wins} / {m.losses}"),
        ("Win rate", f"{m.win_rate_pct:.2f}%"),
        ("Profit factor", pf),
        ("Expectancy", f"{m.expectancy:+,.2f} ({m.expectancy_r:+.3f}R)"),
        ("Total R", f"{m.total_r:+.2f}R"),
        ("Average win / loss", f"{m.average_win:+,.2f} / {m.average_loss:+,.2f}"),
        ("Best / worst", f"{m.largest_win:+,.2f} / {m.largest_loss:+,.2f}"),
        ("Payoff ratio", f"{m.payoff_ratio:.2f}"),
        ("Max consecutive wins", f"{m.max_consecutive_wins}"),
        ("Max consecutive losses", f"{m.max_consecutive_losses}"),
        ("Average hold", f"{m.average_duration_minutes:.1f} min ({m.average_bars_held:.1f} bars)"),
        ("Commission paid", f"{m.total_commission:,.2f} ({m.commission_per_trade:,.2f}/trade)"),
        ("Time in market", f"{m.time_in_market_pct:.2f}%"),
        ("Trades per day", f"{m.trades_per_day:.2f}"),
        ("Max DD duration", f"{m.max_drawdown_duration_days:.1f} days"),
    ]:
        add(f"| {label} | {value} |")

    if m.exits_by_reason:
        add("")
        add("## Exit breakdown")
        add("")
        add("| Reason | Count | Share |")
        add("| --- | --- | --- |")
        for reason, count in m.exits_by_reason.items():
            share = count / m.total_trades * 100 if m.total_trades else 0
            add(f"| `{reason}` | {count} | {share:.1f}% |")

    engine = result.engine_summary.get("engine", {})
    if engine:
        add("")
        add("## Engine activity")
        add("")
        add("| Stage | Count |")
        add("| --- | --- |")
        for label, value in [
            ("Bars processed", engine.get("bars_processed", 0)),
            ("Signals seen", engine.get("signals_seen", 0)),
            ("Entries attempted", engine.get("entries_attempted", 0)),
            ("Entries filled", engine.get("entries_filled", 0)),
            ("Entries blocked", engine.get("entries_blocked", 0)),
            ("Broker errors", engine.get("broker_errors", 0)),
        ]:
            add(f"| {label} | {value:,} |")
        if engine.get("block_reasons"):
            add("")
            add("**Why entries were blocked** — the risk layer is doing its job when this list is long:")
            add("")
            add("| Reason | Count |")
            add("| --- | --- |")
            for reason, count in engine["block_reasons"].items():
                add(f"| {reason} | {count:,} |")

    add("")
    add("## Configuration")
    add("")
    add(f"- Strategy params: `{json.dumps(result.engine_summary.get('strategy_params', {}))}`")
    add(f"- Entry timing: {result.engine_summary.get('entry_timing', 'n/a')}")
    add(f"- Sessions: {result.engine_summary.get('session_filter', 'n/a')}")
    risk = result.engine_summary.get("risk_config", {})
    if risk:
        add(f"- Risk: {json.dumps(risk)}")
    add("")
    add("---")
    add("")
    add(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by fx-scalper._")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# SVG charts (no plotting dependency)
# -----------------------------------------------------------------------------
def _sparkline_path(
    values: np.ndarray, width: float, height: float, pad: float = 4.0, max_points: int = 1_200
) -> str:
    """Build an SVG path, decimated to at most `max_points` vertices.

    A 150k-bar backtest has 150k equity samples; emitting one SVG vertex each
    produces a 4 MB file that renders identically to a 1,000-point version.
    Decimation keeps the report a document you can actually open and email.
    """
    if len(values) < 2:
        return ""
    if len(values) > max_points:
        # Stride sampling can miss a spike, so keep the extremes of each bucket.
        step = int(np.ceil(len(values) / max_points))
        trimmed = len(values) - (len(values) % step)
        bucketed = values[:trimmed].reshape(-1, step)
        values = np.concatenate(
            [
                bucketed.min(axis=1, keepdims=True),
                bucketed.max(axis=1, keepdims=True),
                values[trimmed:].reshape(-1, 1),
            ]
        ).ravel()
        # Interleaving min/max preserves the visual envelope in order.

    lo, hi = float(np.nanmin(values)), float(np.nanmax(values))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi == lo:
        hi = lo + 1.0
    xs = np.linspace(pad, width - pad, len(values))
    ys = height - pad - (values - lo) / (hi - lo) * (height - 2 * pad)
    points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys, strict=True))
    return f"M {points.replace(' ', ' L ')}"


def svg_equity_chart(
    equity: np.ndarray,
    times: Sequence[datetime],
    *,
    width: int = 900,
    height: int = 260,
    title: str = "Equity curve",
    initial: float | None = None,
) -> str:
    """Inline SVG equity curve with a drawn-down shading band."""
    if len(equity) < 2:
        return '<p class="muted">Not enough data to plot an equity curve.</p>'
    path = _sparkline_path(equity, width, height)
    lo, hi = float(np.nanmin(equity)), float(np.nanmax(equity))
    if hi == lo:
        hi = lo + 1.0
    line_colour = "#16a34a" if equity[-1] >= (initial if initial is not None else equity[0]) else "#dc2626"
    baseline = ""
    if initial is not None and lo <= initial <= hi:
        y = height - 4 - (initial - lo) / (hi - lo) * (height - 8)
        baseline = (
            f'<line x1="4" y1="{y:.2f}" x2="{width - 4}" y2="{y:.2f}" '
            f'stroke="#94a3b8" stroke-dasharray="4 4" stroke-width="1"/>'
        )
    start = times[0].strftime("%Y-%m-%d") if times else ""
    end = times[-1].strftime("%Y-%m-%d") if times else ""
    return f"""<figure class="chart">
  <figcaption>{html.escape(title)} <span class="muted">({start} → {end}, peak {hi:,.0f}, low {lo:,.0f})</span></figcaption>
  <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="{html.escape(title)}">
    <rect x="0" y="0" width="{width}" height="{height}" fill="#0f172a" rx="6"/>
    {baseline}
    <path d="{path}" fill="none" stroke="{line_colour}" stroke-width="1.6"/>
  </svg>
</figure>"""


def svg_drawdown_chart(equity: np.ndarray, *, width: int = 900, height: int = 160) -> str:
    """Inline SVG underwater (drawdown) chart."""
    if len(equity) < 2:
        return ""
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    path = _sparkline_path(dd, width, height)
    worst = float(np.nanmax(dd))
    return f"""<figure class="chart">
  <figcaption>Drawdown from peak <span class="muted">(worst {worst:,.2f})</span></figcaption>
  <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="Drawdown">
    <rect x="0" y="0" width="{width}" height="{height}" fill="#0f172a" rx="6"/>
    <path d="{path}" fill="none" stroke="#f59e0b" stroke-width="1.4"/>
  </svg>
</figure>"""


def svg_histogram(values: np.ndarray, *, width: int = 900, height: int = 200, bins: int = 30) -> str:
    """Inline SVG histogram of R multiples."""
    if len(values) < 2:
        return ""
    counts, edges = np.histogram(values, bins=bins)
    max_count = max(int(counts.max()), 1)
    bar_w = width / len(counts)
    bars = []
    for i, count in enumerate(counts):
        h = (count / max_count) * (height - 24)
        x = i * bar_w
        colour = "#16a34a" if edges[i] >= 0 else "#dc2626"
        bars.append(
            f'<rect x="{x:.2f}" y="{height - 20 - h:.2f}" width="{max(bar_w - 1, 1):.2f}" '
            f'height="{h:.2f}" fill="{colour}" opacity="0.85"/>'
        )
    return f"""<figure class="chart">
  <figcaption>Distribution of trade outcomes <span class="muted">({len(values)} trades, {edges[0]:.1f}R → {edges[-1]:.1f}R)</span></figcaption>
  <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="R histogram">
    <rect x="0" y="0" width="{width}" height="{height}" fill="#0f172a" rx="6"/>
    {''.join(bars)}
  </svg>
</figure>"""


# -----------------------------------------------------------------------------
# HTML
# -----------------------------------------------------------------------------
_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px; background: #020617; color: #e2e8f0;
       font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.02em; }
h2 { font-size: 17px; margin: 32px 0 12px; color: #f1f5f9;
     border-bottom: 1px solid #1e293b; padding-bottom: 6px; }
.sub { color: #94a3b8; margin-bottom: 24px; font-size: 14px; }
.wrap { max-width: 1000px; margin: 0 auto; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.card { background: #0f172a; border: 1px solid #1e293b; border-radius: 8px; padding: 14px; }
.card .k { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: #94a3b8; }
.card .v { font-size: 20px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }
.pos { color: #4ade80; } .neg { color: #f87171; } .muted { color: #94a3b8; font-weight: 400; font-size: 13px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1e293b; font-size: 14px; }
th { color: #94a3b8; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
td.num, th.num { text-align: right; }
.chart { margin: 0 0 8px; }
.chart figcaption { font-size: 13px; color: #cbd5e1; margin-bottom: 6px; }
svg { width: 100%; height: auto; display: block; }
.warn { background: #451a03; border: 1px solid #b45309; color: #fed7aa; padding: 14px 16px;
        border-radius: 8px; margin-bottom: 24px; font-size: 14px; }
.warn strong { color: #fdba74; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media (max-width: 760px) { .grid2 { grid-template-columns: 1fr; } body { padding: 16px; } }
.scroll { max-height: 420px; overflow: auto; border: 1px solid #1e293b; border-radius: 8px; }
footer { margin-top: 40px; color: #64748b; font-size: 12px; }
code { background: #1e293b; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
"""


def _card(label: str, value: str, klass: str = "") -> str:
    return f'<div class="card"><div class="k">{html.escape(label)}</div><div class="v {klass}">{value}</div></div>'


def build_html(result: Any, *, synthetic: bool = False) -> str:
    m = result.metrics
    d = result.to_dict()
    equity = np.array([s.equity for s in result.equity_curve], dtype="float64")
    times = [s.time for s in result.equity_curve]
    r_multiples = np.array([t.r_multiple for t in result.trades], dtype="float64")
    net_class = "pos" if m.net_profit >= 0 else "neg"
    pf_text = "∞" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"

    cards = "".join(
        [
            _card("Net profit", f"{m.net_profit:+,.2f}", net_class),
            _card("Return", f"{m.return_pct:+.2f}%", net_class),
            _card("Trades", f"{m.total_trades}"),
            _card("Win rate", f"{m.win_rate_pct:.1f}%"),
            _card("Profit factor", pf_text),
            _card("Expectancy", f"{m.expectancy_r:+.3f}R", "pos" if m.expectancy_r >= 0 else "neg"),
            _card("Max drawdown", f"{m.max_drawdown_pct:.2f}%", "neg"),
            _card("Sharpe", f"{m.sharpe:.2f}", "pos" if m.sharpe >= 0 else "neg"),
        ]
    )

    stat_rows = "".join(
        f"<tr><td>{html.escape(label)}</td><td class='num'>{value}</td></tr>"
        for label, value in [
            ("CAGR", f"{m.cagr_pct:+.2f}%"),
            ("Sortino", f"{m.sortino:.2f}"),
            ("Calmar", f"{m.calmar:.2f}"),
            ("Annualised volatility", f"{m.volatility_annual_pct:.2f}%"),
            ("Max drawdown", f"{m.max_drawdown:,.2f} ({m.max_drawdown_pct:.2f}%)"),
            ("Max DD duration", f"{m.max_drawdown_duration_days:.1f} days"),
            ("Wins / losses", f"{m.wins} / {m.losses}"),
            ("Average win", f"{m.average_win:+,.2f}"),
            ("Average loss", f"{m.average_loss:+,.2f}"),
            ("Payoff ratio", f"{m.payoff_ratio:.2f}"),
            ("Total R", f"{m.total_r:+.2f}R"),
            ("Best trade", f"{m.largest_win:+,.2f}"),
            ("Worst trade", f"{m.largest_loss:+,.2f}"),
            ("Max consecutive wins", f"{m.max_consecutive_wins}"),
            ("Max consecutive losses", f"{m.max_consecutive_losses}"),
            ("Average hold", f"{m.average_duration_minutes:.1f} min"),
            ("Time in market", f"{m.time_in_market_pct:.2f}%"),
            ("Trades per day", f"{m.trades_per_day:.2f}"),
            ("Commission paid", f"{m.total_commission:,.2f}"),
            ("Commission / trade", f"{m.commission_per_trade:,.2f}"),
        ]
    )

    exit_rows = "".join(
        f"<tr><td><code>{html.escape(reason)}</code></td><td class='num'>{count}</td>"
        f"<td class='num'>{count / max(m.total_trades, 1) * 100:.1f}%</td></tr>"
        for reason, count in (m.exits_by_reason or {}).items()
    )

    engine = result.engine_summary.get("engine", {})
    block_rows = "".join(
        f"<tr><td>{html.escape(reason)}</td><td class='num'>{count:,}</td></tr>"
        for reason, count in (engine.get("block_reasons") or {}).items()
    )

    trade_rows = ""
    for trade in result.trades[:400]:
        cls = "pos" if trade.net_pnl >= 0 else "neg"
        trade_rows += (
            f"<tr><td>{trade.entry_time:%Y-%m-%d %H:%M}</td>"
            f"<td>{trade.direction.value}</td>"
            f"<td>{trade.lots:.2f}</td>"
            f"<td class='num'>{trade.entry_price:.5f}</td>"
            f"<td class='num'>{trade.exit_price:.5f}</td>"
            f"<td class='num'>{trade.pips:+.1f}</td>"
            f"<td class='num {cls}'>{trade.net_pnl:+.2f}</td>"
            f"<td class='num'>{trade.r_multiple:+.2f}R</td>"
            f"<td>{trade.exit_reason.value}</td>"
            f"<td class='num'>{trade.duration_minutes:.0f}m</td></tr>"
        )
    if len(result.trades) > 400:
        trade_rows += (
            f"<tr><td colspan='10' class='muted'>Showing the first 400 of "
            f"{len(result.trades)} trades — see the trades CSV for all of them.</td></tr>"
        )

    warnings = report_warnings(result, synthetic)
    warnings_html = ""
    for warning in warnings:
        if "synthetic" in warning.lower():
            warnings_html += f'<div class="warn"><strong>Synthetic data.</strong> {html.escape(warning)}</div>'
    others = [w for w in warnings if "synthetic" not in w.lower()]
    if others:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in others)
        warnings_html += f'<div class="warn"><strong>Data warnings</strong><ul>{items}</ul></div>'

    params = html.escape(json.dumps(result.engine_summary.get("strategy_params", {}), indent=2))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(result.symbol)} {html.escape(result.strategy)} — backtest report</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>{html.escape(result.symbol)} · {html.escape(result.strategy)}</h1>
  <div class="sub">
    {html.escape(result.timeframe)} · {d['period']['start']} → {d['period']['end']}
    · {result.bars:,} bars · {d['period']['days']} days · runtime {result.runtime_sec:.1f}s
  </div>
  {warnings_html}
  <div class="cards">{cards}</div>

  <h2>Equity</h2>
  {svg_equity_chart(equity, times, initial=result.initial_balance)}
  {svg_drawdown_chart(equity)}

  <h2>Performance</h2>
  <div class="grid2">
    <table>{stat_rows}</table>
    <div>
      {svg_histogram(r_multiples) if len(r_multiples) else ''}
      <table>{"".join([f"<tr><th>{'Exit reason'}</th><th class='num'>Count</th><th class='num'>Share</th></tr>", exit_rows])}</table>
    </div>
  </div>

  <h2>Risk rails — why entries were blocked</h2>
  <table>
    <tr><th>Reason</th><th class="num">Count</th></tr>
    <tr><td>Bars processed</td><td class="num">{engine.get('bars_processed', 0):,}</td></tr>
    <tr><td>Signals seen</td><td class="num">{engine.get('signals_seen', 0):,}</td></tr>
    <tr><td>Entries filled</td><td class="num">{engine.get('entries_filled', 0):,}</td></tr>
    <tr><td>Entries blocked</td><td class="num">{engine.get('entries_blocked', 0):,}</td></tr>
    {block_rows}
    <tr><td>Broker errors</td><td class="num">{engine.get('broker_errors', 0):,}</td></tr>
  </table>

  <h2>Trades</h2>
  <div class="scroll">
    <table>
      <tr>
        <th>Entry</th><th>Side</th><th>Lots</th><th class="num">In</th><th class="num">Out</th>
        <th class="num">Pips</th><th class="num">P&amp;L</th><th class="num">R</th>
        <th>Exit</th><th class="num">Held</th>
      </tr>
      {trade_rows or "<tr><td colspan='10' class='muted'>No trades.</td></tr>"}
    </table>
  </div>

  <h2>Configuration</h2>
  <table>
    <tr><td>Entry timing</td><td class="num">{html.escape(str(result.engine_summary.get('entry_timing', 'n/a')))}</td></tr>
    <tr><td>Sessions</td><td class="num">{html.escape(str(result.engine_summary.get('session_filter', 'n/a')))}</td></tr>
    <tr><td>Risk per trade</td><td class="num">{result.engine_summary.get('risk_config', {}).get('risk_per_trade_pct', 'n/a')}%</td></tr>
    <tr><td>Daily loss limit</td><td class="num">{result.engine_summary.get('risk_config', {}).get('max_daily_loss_pct', 'n/a')}%</td></tr>
    <tr><td>Max drawdown stop</td><td class="num">{result.engine_summary.get('risk_config', {}).get('max_drawdown_pct', 'n/a')}%</td></tr>
    <tr><td>Config file</td><td class="num">{html.escape(str(result.config_path or 'n/a'))}</td></tr>
  </table>
  <pre style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px;overflow:auto"><code>{params}</code></pre>

  <footer>
    Generated {generated} by fx-scalper. Past performance on historical or synthetic data
    does not predict future results. Nothing here is financial advice.
  </footer>
</div>
</body>
</html>"""


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------
def write_all_reports(result: Any, cfg: AppConfig, *, synthetic: bool = False) -> dict[str, Path]:
    """Write every configured artifact and return the paths."""
    out_dir = Path(cfg.reporting.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.symbol}_{result.strategy}_{result.timeframe}"
    written: dict[str, Path] = {}

    if cfg.reporting.write_json:
        written["json"] = write_json(result.to_dict(), out_dir / f"{stem}_report.json")
    if cfg.reporting.write_markdown:
        path = out_dir / f"{stem}_report.md"
        path.write_text(build_markdown(result, synthetic=synthetic), encoding="utf-8")
        written["markdown"] = path
    if cfg.reporting.write_equity_csv:
        written["equity_csv"] = write_equity_csv(result.equity_curve, out_dir / f"{stem}_equity.csv")
        written["trades_csv"] = write_trades_csv(result.trades, out_dir / f"{stem}_trades.csv")

    html_path = out_dir / f"{stem}_report.html"
    html_path.write_text(build_html(result, synthetic=synthetic), encoding="utf-8")
    written["html"] = html_path

    for kind, path in written.items():
        log.info("wrote %s report: %s", kind, path)
    return written


def markdown_table(frame: pd.DataFrame) -> str:
    """Render a DataFrame as a GitHub markdown table.

    Hand-rolled because ``DataFrame.to_markdown`` needs the optional `tabulate`
    package, and a reporting path should not fail because of a missing
    presentation library.
    """
    if frame.empty:
        return "_(no rows)_"
    columns = [str(c) for c in frame.columns]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for _, row in frame.iterrows():
        cells = []
        for column in frame.columns:
            value = row[column]
            if isinstance(value, float):
                cells.append(f"{value:,.4f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_portfolio_report(results: Iterable[Any], cfg: AppConfig, *, synthetic: bool = False) -> dict[str, Path]:
    """Aggregate several symbol runs into one comparison report."""
    results = list(results)
    if not results:
        raise ValueError("no results to summarise")
    out_dir = Path(cfg.reporting.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    rows = []
    for result in results:
        m = result.metrics
        rows.append(
            {
                "symbol": result.symbol,
                "strategy": result.strategy,
                "trades": m.total_trades,
                "net_profit": round(m.net_profit, 2),
                "return_pct": round(m.return_pct, 3),
                "win_rate_pct": round(m.win_rate_pct, 2),
                "profit_factor": None if m.profit_factor == float("inf") else round(m.profit_factor, 3),
                "expectancy_r": round(m.expectancy_r, 4),
                "max_dd_pct": round(m.max_drawdown_pct, 3),
                "sharpe": round(m.sharpe, 3),
                "time_in_market_pct": round(m.time_in_market_pct, 3),
            }
        )
    frame = pd.DataFrame(rows).sort_values("expectancy_r", ascending=False)

    csv_path = out_dir / f"portfolio_{stamp}.csv"
    frame.to_csv(csv_path, index=False)

    total_net = float(frame["net_profit"].sum())
    md = [
        "# Portfolio comparison",
        "",
        f"_{len(results)} symbol run(s), combined net {total_net:+,.2f}_",
        "",
        "Sorted by expectancy in R, which is the most comparable column across symbols.",
        "",
        markdown_table(frame),
        "",
        "> [!NOTE]",
        "> Each symbol was backtested independently against the full starting balance.",
        "> Summing the net column overstates what a single account could achieve, because",
        "> in reality the trades compete for the same margin and the same daily loss budget.",
    ]
    if synthetic:
        md.insert(2, f"> [!WARNING]\n> {SYNTHETIC_BANNER}\n")
    md_path = out_dir / f"portfolio_{stamp}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    return {"portfolio_csv": csv_path, "portfolio_markdown": md_path}


def summarise_results(results: Sequence[Any]) -> dict[str, Any]:
    """One-line-per-symbol console summary dict."""
    return {
        "symbols": len(results),
        "total_trades": sum(len(r.trades) for r in results),
        "combined_net": round(sum(r.metrics.net_profit for r in results), 2),
        "best": max(results, key=lambda r: r.metrics.expectancy_r).symbol if results else None,
        "worst": min(results, key=lambda r: r.metrics.expectancy_r).symbol if results else None,
    }


def recompute_metrics(result: Any, cfg: AppConfig) -> Any:
    """Recompute metrics for an existing result (used by the dashboard)."""
    return compute_metrics(
        trades=result.trades,
        equity_curve=result.equity_curve,
        initial_balance=cfg.account.initial_balance,
        bars_per_year=cfg.annualization_bars,
        risk_free_rate=cfg.backtest.risk_free_rate,
    )


def drawdown_frame(equity_curve: Sequence[AccountSnapshot]) -> pd.DataFrame:
    return drawdown_series(equity_curve)
