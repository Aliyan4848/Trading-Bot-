# Getting real market data

Synthetic data validates the pipeline. Only real bars can tell you whether a strategy works, so this
is the step that actually matters.

## Option 1 — Export from MetaTrader 5 (recommended)

1. Open MT5 and log in to **any** account (a demo account is fine — history is the same).
2. `Tools` → `History Center` (or press `F2`).
3. Pick a symbol, select **M1**, and press **Download** if the history is not already local.
4. Right-click the symbol → **Export** → save as CSV.

MT5 exports tab-separated files with separate `<DATE>` and `<TIME>` columns:

```
<DATE>	<TIME>	<OPEN>	<HIGH>	<LOW>	<CLOSE>	<TICKVOL>	<VOL>	<SPREAD>
2024.01.02	00:00:00	1.10421	1.10433	1.10405	1.10411	12	0	6
```

Save one file per symbol under `data/`, then point the config at the pattern:

```yaml
data:
  source: "csv"
  timeframe: "M1"
  csv:
    path: "data/{symbol}_M1.csv"   # {symbol} matches each entry in `instruments:`
    tz: "UTC"                      # MT5 exports are usually broker time, not UTC
```

> **Timezone matters.** MT5's History Center exports in the *broker's* server time, which is often
> UTC+2 or UTC+3, not UTC. Session windows and daily loss resets use these timestamps, so set
> `tz:` to whatever your broker's server clock runs at. If in doubt, compare a known London-open bar
> against `docs/configuration.md`.

## Option 2 — Pull it from the terminal

With MT5 installed, running, and logged in on Windows:

```bash
python run.py download --out data
```

That writes `data/{SYMBOL}_M1.csv` for every symbol in `instruments:` using the same loader the
backtester uses, so what you backtest is exactly what the live bot sees.

## Option 3 — Dukascopy / HistData

Both publish free M1 FX history. Dukascopy's "CSV" downloads use `Date,Time,Open,High,Low,Close,Volume`
and HistData uses `YYYYMMDD HHMMSS;O;H;L;C;V`. Both parse without configuration:

```yaml
data:
  csv:
    path: "data/{symbol}.csv"
    tz: "UTC"        # Dukascopy timestamps are UTC
```

## What the loader accepts

`normalize_ohlc` handles, without any configuration:

| Format | Example columns |
| --- | --- |
| MT5 export | `<DATE>` + `<TIME>`, tab or comma separated |
| Generic | `time` / `timestamp` / `datetime` / `date` |
| Epoch seconds | a numeric `time` column (as the MT5 Python API returns) |
| Aliases | `Open`/`o`/`<OPEN>`, `TickVol`/`Vol`/`Volume`, `bidopen`/`askclose`, … |
| Separators | `,` `;` tab, or whitespace |

It also:

- localises naive timestamps to the configured `tz` and converts to UTC,
- sorts by time and drops duplicate bars,
- repairs rows where `high < max(open, close)` instead of discarding real price action,
- warns (without failing) about suspicious gaps and short history.

## A data quality checklist

Before you believe a backtest:

- [ ] **At least 3 months of M1 bars.** Less than that and a scalping result is noise. 12+ months if
      you intend to trade it.
- [ ] **Gaps are real.** Weekends should show a ~48h hole; a random 6-hour hole on a Tuesday means
      missing history.
- [ ] **Spreads match your broker.** `spread_pips` in `instruments:` should be the *typical* spread
      you actually see. If your broker charges 1.4 pips on EURUSD, do not leave 0.6 in the config.
- [ ] **Pip values match your account.** `pip_value_per_lot: 10.0` for EURUSD assumes a USD account
      and a 100k contract. Verify per symbol against your broker's contract specification.
- [ ] **Timezone is right.** A London-session strategy tested on UTC+3 timestamps is a different
      strategy.

Run `python run.py doctor` at any time to see what the config currently resolves to.
