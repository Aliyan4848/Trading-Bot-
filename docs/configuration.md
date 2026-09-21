# Configuration reference

Every option lives in [`config/config.yaml`](../config/config.yaml), is validated at startup, and can
be overridden from the command line. An unknown key is a **hard error** — a typo never silently
becomes a default.

## CLI overrides

| Flag | Equivalent config path |
| --- | --- |
| `--strategy NAME` | `strategy.name` |
| `--symbol EURUSD` (repeatable) | replaced `instruments` list |
| `--timeframe M5` | `data.timeframe` |
| `--source csv` | `data.source` |
| `--csv 'data/{symbol}_M1.csv'` | `data.csv.path` |
| `--balance 25000` | `account.initial_balance` |
| `--risk 0.25` | `risk.risk_per_trade_pct` |
| `--bars 300000` | `data.synthetic.bars` |

`data.synthetic.bars` counts *wall-clock minutes* from `data.synthetic.start`, not
finished bars: weekend and holiday minutes are removed afterwards, so the frame
handed to the engine is roughly 72% of this value (the default 200,000 becomes
about 143,800 M1 bars — a bit over three months of forex trading). Budget for the
difference when sizing a run.
| `--spread 1.2` | `data.synthetic.spread_pips` |
| `--fast-ema 5 --slow-ema 13 --trend-ema 100` | `strategy.params.*` |
| `--rsi-period 21 --reward-risk 2.5` | `strategy.params.*` |

## `account`

| Key | Default | Meaning |
| --- | --- | --- |
| `currency` | `USD` | Account denomination. `pip_value_per_lot` is expressed in this currency. |
| `initial_balance` | `10000.0` | Starting balance for backtests and paper sessions. |
| `leverage` | `30` | Used for margin and the paper broker's stop-out emulation. |

## `data`

| Key | Default | Meaning |
| --- | --- | --- |
| `source` | `synthetic` | `synthetic`, `csv`, or `mt5`. |
| `timeframe` | `M1` | `M1 M5 M15 M30 H1 H4 D1`. Also sets the annualisation factor for Sharpe/CAGR. |
| `csv.path` | | File path; `{symbol}` expands per instrument, case-insensitively. |
| `csv.timestamp_column` | auto | Force a specific column if auto-detection picks the wrong one. |
| `csv.tz` | `UTC` | Timezone of naive timestamps in the file. **Set this to your broker's server time.** |
| `mt5.bars` | `50000` | Bars pulled per symbol when priming indicators. |
| `synthetic.*` | | Generator settings; see the comments in the config file. |

## `broker`

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `paper` | `paper` = simulated fills against real data. `mt5` = real routing (gated). |
| `paper.slippage_pips` | `0.3` | Extra adverse price movement on top of the spread, both on entry and stop exits. |
| `paper.commission_per_lot` | `7.0` | Round-turn commission per 1.00 lot, split across entry and exit. |
| `paper.stop_out_level_pct` | `50.0` | Margin call: liquidate when equity < this % of used margin. |
| `mt5.magic` | `20260921` | Order tag. Positions with a different magic are never touched. |
| `mt5.deviation_points` | `20` | Max slippage accepted on a market order before it is rejected. |
| `mt5.filling_mode` | `IOC` | `IOC` / `FOK` / `RETURN`. Falls back automatically if the symbol refuses it. |
| `mt5.order_retries` | `3` | Attempts for requotes and transient failures. Hard rejections are never retried. |

## `risk`

| Key | Default | Meaning |
| --- | --- | --- |
| `risk_per_trade_pct` | `0.5` | % of **equity** risked between entry and stop. Drives lot size. |
| `max_daily_loss_pct` | `3.0` | Stop opening trades after this much realized daily loss. Resets at the date change. |
| `max_drawdown_pct` | `12.0` | Kill switch on peak-to-trough equity. Flattens everything and stops for good. |
| `max_concurrent_positions` | `2` | Across all symbols. |
| `max_trades_per_day` | `12` | Per calendar day (UTC). |
| `min_lot` / `max_lot` / `lot_step` | `0.01 / 5.0 / 0.01` | Broker limits. Sizing rounds **down** to the step, so rounding never increases risk. |
| `max_spread_pips` | `2.0` | Entries are skipped above this. |
| `min_seconds_between_trades` | `0` | Cooldown between entries. |
| `trailing_stop` | `false` | Trail behind the best price seen. Applied from the **next** bar (see below). |
| `trailing_start_r` | `1.0` | Only start trailing after this many R of profit. |
| `trailing_distance_r` | `0.5` | Trail this far behind the best price, in R. |
| `break_even_at_r` | `null` | Move the stop to entry at this many R (e.g. `1.0`). |

**Why trails apply from the next bar:** within a single candle we cannot know whether the high came
before the low. Applying a tightened stop mid-bar, then checking that same bar's range against it,
would let the simulation pick whichever sequence was more favourable. Instead the stop is raised at
the bar's close and becomes active on the following bar.

## `session`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | When false, the bot trades 24h. |
| `timezone` | `UTC` | Timezone the windows are expressed in. |
| `windows` | London + New York | Entries are blocked outside the union of these. Windows may cross midnight. |
| `skip_friday_after` | `19:00` | No new entries from this time on Friday. |
| `skip_weekend` | `true` | No entries on Saturday, or Sunday before 21:00. |
| `flat_at_close` | `true` | Close open positions on the last bar of a session. |

`flat_at_close` triggers when the *next* bar would fall outside the session, so it works on any
timeframe and also catches the Friday close.

## `strategy`

`name` selects a registered strategy; `params` are validated by the strategy itself. Run
`python run.py strategies` for the full parameter list of each.

| Strategy | Key parameters |
| --- | --- |
| `ema_rsi_momentum` | `fast_ema`, `slow_ema`, `trend_ema`, `rsi_period`, `rsi_long_min`, `rsi_short_max`, `atr_period`, `atr_stop_mult`, `reward_risk`, `min_atr_pips`, `max_atr_pips`, `one_signal_per_cross`, `allow_shorts` |
| `bollinger_reversion` | `bb_period`, `bb_std`, `atr_period`, `atr_stop_mult`, `min_reward_risk`, `max_adx`, `adx_period`, `min_bandwidth_pct`, `max_bandwidth_pct`, `target`, `require_reversal_candle`, `allow_shorts` |
| `vwap_pullback` | `vwap_reset`, `vwap_period`, `atr_period`, `atr_stop_mult`, `reward_risk`, `touch_tolerance_pips`, `ema_period`, `require_stack`, `max_distance_pips`, `allow_shorts` |

## `instruments`

One entry per tradeable symbol. **Verify these against your broker's contract specification** — they
differ between account types, and wrong pip values mean wrong position sizes.

| Key | Meaning |
| --- | --- |
| `symbol` | Must match the broker's spelling exactly (including suffixes like `.a`). |
| `pip_size` | `0.0001` for 5-digit FX, `0.01` for JPY pairs, `0.1` for XAUUSD on most brokers. |
| `contract_size` | Units per 1.00 lot (usually `100000`). Used for margin. |
| `pip_value_per_lot` | Account-currency value of a 1-pip move on 1.00 lot. USD account: `10.0` for EURUSD. |
| `spread_pips` | Typical spread. This is charged on every round trip. |
| `digits` | Price precision, used for rounding order levels. |

## `backtest`

| Key | Default | Meaning |
| --- | --- | --- |
| `warmup_bars` | `250` | Bars skipped before entries are allowed. Also floors the strategy's own requirement. |
| `entry_on_next_open` | `true` | Signal at close of bar *t*, fill at the open of *t+1*. Turning this off fills at the signal bar's close, which is optimistic. |
| `intrabar_priority` | `stop` | When one bar contains both the stop and the target, assume the stop came first. `target` is the optimistic alternative. On M1 data the two rarely differ, because a bar needs a range of roughly 3× ATR to touch both. |
| `annualization_bars` | `null` | Annualisation factor for Sharpe/CAGR. `null` derives it from `data.timeframe`. |
| `risk_free_rate` | `0.0` | Subtracted from returns before the Sharpe ratio. |
| `export_trades` | `true` | Write `trades.csv` alongside the reports. |

## `reporting`

| Key | Default | Meaning |
| --- | --- | --- |
| `output_dir` | `results` | Where reports are written. |
| `write_json` | `true` | Machine-readable results, including the engine's block reasons. |
| `write_markdown` | `true` | Human-readable summary. |
| `write_equity_csv` | `true` | Equity curve (metrics use the full curve; the file is sampled above 20k rows) and the trade list. |
| `plot_equity_curve` | `true` | The HTML report always includes inline SVG charts, so no plotting library is required. |

## Environment (`.env`)

| Variable | Purpose |
| --- | --- |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | Terminal credentials. |
| `MT5_PATH` | Full path to `terminal64.exe` if MT5 is not in the default location. |
| `SCALPER_ALLOW_LIVE` | Must be `yes` for real-money routing. One of the three live gates. |

`.env` is git-ignored. Never commit credentials.
