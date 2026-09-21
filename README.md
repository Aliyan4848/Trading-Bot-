# fx-scalper

A config-driven **forex scalping bot in Python**: backtest it, paper trade it, and — only if you
explicitly ask three separate times — route real orders through MetaTrader 5.

The interesting part of this project is not the strategies. It is the **simulation honesty**:
spread is charged on the correct side of every fill, stops respect gaps, R is measured against the
risk actually taken, and the engine physically cannot act on a candle that has not closed yet.

---

## Quick start

No installation needed — `run.py` puts `src/` on the path for you.

```bash
git clone https://github.com/Aliyan4848/Trading-Bot-.git
cd Trading-Bot-

python run.py doctor                     # check config, data and dependencies
python run.py strategies                 # list strategies and their parameters
python run.py backtest                   # backtest every configured symbol
python run.py backtest --strategy vwap_pullback --symbol EURUSD
python run.py backtest --portfolio          # all symbols on ONE shared account
python run.py paper --max-bars 500       # watch it trade, with simulated fills
python run.py download --out data        # dump bars to CSV for offline runs
python run.py dashboard                 # browse results/ in a browser (optional)
```

Prefer a real install? `pip install -e .` gives you a `scalper` command with identical arguments.

Requirements: **Python 3.10+**, plus `numpy`, `pandas`, `PyYAML` (`pip install -r requirements.txt`).
Live MT5 trading additionally needs **Windows** and `pip install -r requirements-mt5.txt`.

Every backtest writes a self-contained HTML report to `results/` — open it in a browser, no server,
no CDN, no plotting library:

```
results/EURUSD_ema_rsi_momentum_M1_report.html
```

Prefer clicking to typing? `python run.py dashboard` (needs `pip install ".[dashboard]"`) opens a
read-only Streamlit view of `results/`: every run side by side, equity and drawdown curves, the R
distribution, exit-reason breakdown, and a per-trade table you can download. It reads the files the
backtest wrote and cannot place an order.

---

## Read this before you trust any number it prints

The default config uses `data.source: synthetic` so the pipeline runs on a fresh clone with no
broker account. The generated market is *plausible* — session volatility, trend regimes, weekend
gaps, fat tails — but it is not the market, and it contains no real edge to find.

The demo run therefore **loses money on all five symbols**:

| Symbol | Trades | Net | Win% | PF | Expectancy | Max DD | Sharpe |
| --- | --- | --- | --- | --- | --- | --- | --- |
| XAUUSD | 63 | -955.66 | 38.1% | 0.64 | -0.139R | 12.25% | -2.39 |
| USDJPY | 64 | -1,228.62 | 29.7% | 0.22 | -0.167R | 12.33% | -6.78 |
| EURUSD | 75 | -1,035.81 | 30.7% | 0.64 | -0.202R | 12.07% | -3.45 |
| GBPUSD | 68 | -1,168.42 | 27.9% | 0.55 | -0.270R | 12.12% | -4.03 |
| AUDUSD | 27 | -1,204.03 | 7.4% | 0.10 | -0.820R | 12.04% | -8.02 |

That is the correct result, and it is the most useful thing this repository can show you. A strategy
with a 1.8:1 reward/risk needs a **35.7% win rate just to break even**; paying one spread plus
commission per round trip on data with no edge produces exactly this. A backtester that showed a
profit here would be lying to you.

To get real answers you need real bars — see [Getting real data](#getting-real-data).

---

## How it works

```
                     ┌──────────────┐
   data feeds  ────► │   Engine     │ ────► broker
   synthetic          │  (engine.py) │       paper  (simulated fills)
   csv                │              │       mt5    (real routing, gated)
   mt5                │  1. service open positions vs this bar
                      │  2. update equity + risk state
                      │  3. act on the PREVIOUS bar's signal
                      │  4. trail stops (effective next bar)
                      │  5. flatten at session close
                      └──────┬───────┘
                             │
      strategy ──────────────┘        risk manager ────┘
      (vectorised signals)            (sizing, daily loss, drawdown stop)
```

**One engine, three modes.** The backtester, `paper` and `live` all drive the same `Engine`, so
there is no second implementation to drift out of sync. A test asserts that a replayed session
produces byte-identical trades to the equivalent backtest.

### The five rules that keep the simulation honest

1. **No lookahead, structurally.** A signal computed on the close of bar `t` is filled at the
   *open* of bar `t+1`. `tests/test_no_lookahead.py` proves it two ways: truncating history must not
   change earlier trades, and rewriting every future bar with nonsense must not change them either.
2. **Quotes are bid.** Bars are treated as bid prices, so a long pays the spread on entry and a
   short pays it on exit — exactly one spread per round trip, whichever way you trade. A short's
   stop triggers on the **ask**, which is what your broker will actually do.
3. **Gaps are gaps.** If a bar opens past your stop, you are filled at the open. Nobody gets filled
   at a price that never traded. `tests/test_paper_broker.py` asserts a weekend gap costs 1.59R.
4. **R measures real risk.** `initial_stop_price` is frozen at entry, so trailing a stop to
   break-even cannot silently inflate every R multiple in your report.
5. **Sizing is honest about the spread.** Risk is sized from the signal distance, but the fill is at
   the ask, so a "10 pip" stop on EURUSD really risks ~11 pips. That extra pip shows up in the
   results instead of being hidden.

### What it does *not* model

Disclosed because a backtest's blind spots are where live money dies:

- **Swap/rollover financing** — irrelevant for minutes-long scalps, but present if you hold
  overnight (and `session.flat_at_close: true` prevents that by default).
- **Spread widening around news.** The spread is a constant per instrument. Combine `max_spread_pips`
  with a news calendar of your own if you plan to hold through releases.
- **Partial fills, requotes, and broker-side slippage beyond `slippage_pips`.** MT5 order rejections
  and requotes *are* handled, but a live fill can always be worse than a simulated one.
- **Volume.** FX spot has no central volume. `indicators.rolling_vwap` equal-weights bars when
  volume is absent rather than pretending it knows better.
- **Symbols trading against each other.** Backtests run one symbol at a time against the full
  balance; a portfolio report is a comparison table, and it says so in the file.

---

## Strategies

| Name | Idea | Stop | Target |
| --- | --- | --- | --- |
| `ema_rsi_momentum` | fast/slow EMA cross, confirmed by RSI, filtered by a trend EMA and an ATR band | `atr_stop_mult × ATR` | `reward_risk × stop` |
| `bollinger_reversion` | fade a close outside the bands, but only when ADX says the market is *not* trending | ATR beyond the band | the middle band |
| `vwap_pullback` | pullback to the session VWAP, in the direction of the EMA stack, with a rejection candle | `atr_stop_mult × ATR` | `reward_risk × stop` |

Add your own by subclassing `Strategy` and implementing `prepare()` — it must return a `signal`
column (`1`/`-1`/`0`), `stop_pips`, `tp_pips` and a `reason`, all indexed like the input frame. Then:

```python
from scalper.strategies.base import Strategy, register

@register
class MyStrategy(Strategy):
    name = "my_strategy"
    ...
```

The `CompositeStrategy` runs several strategies and only trades when they agree.

---

## Configuration

Everything lives in [`config/config.yaml`](config/config.yaml) — commented, validated at startup, and
strict about typos (an unknown key is an error, never a silent default).

The sections that matter most:

```yaml
broker:
  mode: "paper"            # paper = simulated fills, real market data

risk:
  risk_per_trade_pct: 0.5  # % of equity risked between entry and stop
  max_daily_loss_pct: 3.0  # stop opening trades after this daily drawdown
  max_drawdown_pct: 12.0   # hard kill switch on peak-to-trough equity
  max_concurrent_positions: 2
  max_spread_pips: 2.0     # skip entries when the spread is wide
  trailing_stop: false
  break_even_at_r: null    # e.g. 1.0 to move SL to break-even at +1R

session:
  windows:                 # entries are blocked outside these windows (UTC)
    - { name: "London",   start: "07:00", end: "16:00" }
    - { name: "New York", start: "12:00", end: "20:30" }
  flat_at_close: true      # never hold overnight
```

Override anything from the command line without editing the file:

```bash
python run.py backtest --strategy bollinger_reversion --risk 0.25 --bars 300000
python run.py backtest --fast-ema 5 --slow-ema 13 --reward-risk 2.0
python run.py backtest --source csv --csv 'data/{symbol}_M1.csv'
```

See [`docs/configuration.md`](docs/configuration.md) for every field.

---

## Portfolio mode: one account, many pairs

```bash
python run.py backtest --portfolio                    # every configured symbol
python run.py backtest --portfolio --symbols EURUSD GBPUSD USDJPY
```

Without `--portfolio`, each symbol is a separate run with its own balance and its own limits, and the
portfolio report just adds the rows up. That sum cannot be traded: five symbols each risking 0.5% of a
full balance look like five accounts, every one of them gets its own `max_concurrent_positions`
allowance, and open risk is never netted — being long EURUSD, long GBPUSD and short USDJPY is one
dollar bet, not three.

`--portfolio` puts every symbol through one engine on one account:

- one equity figure sizes every position;
- `max_concurrent_positions`, `max_trades_per_day` and the loss caps apply to the account, not to
  each symbol;
- the kill switch flattens everything and stops the whole book;
- drawdown is the account's real peak-to-trough path, correlation included.

The demo numbers show why it matters:

| | Trades | Net | Max drawdown |
| --- | --- | --- | --- |
| Sum of five per-symbol runs | 297 | −5,592.53 | 12.07% each |
| `--portfolio` (one account) | 55 | −1,204.40 | 12.04% |

Summing claims a −55.9% loss that no single account could have suffered while obeying its own 12%
kill switch — the kill switch fires once, and after it does, trading is over for the whole book. The
sum is not conservative, it is simply a different question.

---

## Getting real data

You need M1 history. Two free routes:

**1. MetaTrader 5 (easiest if you already use MT5)**

Tools → History Center → select a symbol and M1 → Export. Save as `data/EURUSD_M1.csv`, then:

```yaml
data:
  source: "csv"
  timeframe: "M1"
  csv:
    path: "data/{symbol}_M1.csv"
    tz: "UTC"      # set to the zone your export was written in
```

The CSV loader auto-detects MT5's `<DATE>`/`<TIME>` export format, Dukascopy/HistData layouts,
comma/semicolon/tab separators, and Unix epoch seconds. It also repairs impossible OHLC rows and
warns about gaps.

**2. Straight from the terminal** (Windows, MT5 running and logged in):

```bash
python run.py download --out data      # saves data/{symbol}_M1.csv for every symbol
```

Full walkthrough: [`docs/getting-data.md`](docs/getting-data.md).

---

## Going live (MetaTrader 5)

Live routing needs **three independent confirmations**, because the failure mode of a mis-typed flag
should be "it refused", never "it bought":

1. `broker.mode: mt5` in the config — an edit you have to mean.
2. `--live` on the command line.
3. `SCALPER_ALLOW_LIVE=yes` in `.env`.

On top of that, the broker **refuses a real-money account** unless it was constructed with
`allow_live=True` (i.e. all three of the above). A demo account always passes through.

```bash
cp .env.example .env       # fill in MT5_LOGIN / MT5_PASSWORD / MT5_SERVER
# broker.mode: mt5 in config/config.yaml
python run.py doctor       # confirms what is and is not enabled
python run.py live --live --i-understand-the-risk
```

Recommended path: **backtest → paper trade → MT5 demo account → small live account.** Nothing in
this repository has been validated on your broker, your spreads, or your execution.

Details, including what to check before your first demo session: [`docs/live-trading.md`](docs/live-trading.md).

---

## Project layout

```
config/config.yaml          # every tunable, commented
run.py                      # zero-install launcher
src/scalper/
  engine.py                 # the trading loop (shared by backtest/paper/live)
  backtest.py               # historical driver, walk-forward, portfolio mode
  live.py                   # replay (paper) and MT5 polling loops
  risk.py                   # sizing, daily loss cap, drawdown kill switch
  metrics.py                # performance analytics
  report.py                 # JSON / CSV / Markdown / self-contained HTML
  indicators.py             # EMA, RSI, ATR, ADX, Bollinger, VWAP (Wilder-accurate)
  models.py                 # Bar, Signal, Order, Position, Trade
  config.py                 # typed config + validation
  strategies/               # the three built-ins + strategy registry
  brokers/                  # paper (simulated) and mt5 (real) venues
  data/                     # synthetic, csv, mt5 feeds
dashboard/app.py            # optional Streamlit reader for results/
tests/                      # 208 tests, including lookahead proofs
scripts/                    # parameter sweep and programmatic examples
```

---

## Tests

```bash
pytest -q                       # 208 tests, ~48s
pytest tests/test_no_lookahead.py -v
```

The suite is where the claims live. Highlights:

- `test_no_lookahead.py` — truncating history, and rewriting the future with nonsense, must not
  change a single earlier trade.
- `test_paper_broker.py` — hand-computed fills: spread side, stop/target prices, gap fills,
  commission split, margin stop-out.
- `test_engine.py` — a signal on bar `t` fills at bar `t+1`'s open, never at bar `t`'s price.
- `test_mt5_broker.py` — the Windows-only code path, tested against a fake terminal: demo
  detection, filling-mode fallback, requote retries, position-ticket resolution.
- `test_indicators.py` — Wilder's own RSI reference values, which is how an SMA-seeded
  `wilder_smooth` replaced `pandas.ewm` (a 4-point difference at bar 14).
- `test_risk_metrics.py` — a losing curve must not be able to report `+0.00%`; see the
  `monthly_returns` regression test for why "no data" is not the same as "flat".
- `test_portfolio.py` — one account, several pairs: the concurrency cap and kill switch are account
  limits, bars are consumed in time order, and `--portfolio` is asserted to differ from the sum of
  per-symbol runs rather than merely claimed to.

---

## Limitations, honestly

- **No strategy here has an edge on your data until you prove it does.** The demo numbers are
  negative and should stay that way on synthetic data.
- **`--portfolio` is the only way to size several pairs honestly.** Without it each symbol gets its
  own run, its own starting balance and its own copy of every risk cap, and the portfolio report
  simply adds them up — a sum that overstates what one account can do. See
  [Portfolio mode](#portfolio-mode-one-account-many-pairs); the demo numbers below differ by 4.6x
  depending on which you read.
- Portfolio runs interleave symbols on one clock and mark positions to market as their own bars
  arrive. If a symbol's history is sparse or the pairs do not overlap, its contribution while flat is
  not modelled — the account simply does not move for that symbol.
- MT5 is Windows-only. On Linux/macOS, use CSV history for research; execution needs a Windows VPS
  or Wine.
- Spreads are constants. Real spreads widen; `max_spread_pips` is your only defence in-simulator.

## Licence

MIT — see [LICENSE](LICENSE).

## Disclaimer

This is trading software. It can lose money, including more than you expect. Nothing here is
financial advice, and past or simulated performance says nothing about future results. Test on a
demo account first; only risk money you can afford to lose.
