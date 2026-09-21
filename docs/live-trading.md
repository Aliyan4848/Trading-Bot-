# Live trading with MetaTrader 5

Read this fully before pointing the bot at an account that can lose money.

## Before any of this: `scalper readiness`

Arming the gates below proves nothing about whether the strategy works — they exist to prevent
accidents, not to judge edge. Run the checklist first:

```bash
python run.py readiness
```

It reads `results/` and fails if the evidence is synthetic, too thin (under 100 trades), shorter than a
year, unprofitable after costs, inconsistent across symbols, or an in-sample fit. It exits non-zero
while blocked, so it can gate a deployment script. Two rules it will not bend:

- synthetic data can never pass, no matter how favourable the numbers;
- the *median* run decides, not the best one.

Read it as "you have not obviously fooled yourself yet", not as a green light.


## The three gates

Live orders require **all three**, deliberately, because a single mistake should not be able to
spend real money:

| Gate | Where | Purpose |
| --- | --- | --- |
| `broker.mode: mt5` | `config/config.yaml` | An edit you have to mean |
| `--live` | command line | Stops a stray `paper` typo from going live |
| `SCALPER_ALLOW_LIVE=yes` | `.env` | Keeps a cloned config from trading on its own |

If any gate is closed the bot exits with an explanation and does nothing. Even with all three open,
the MT5 broker **refuses to attach to a real-money account** unless it also received
`allow_live=True`, which only happens when all three gates are open. On a **demo** account it
proceeds without the ceremony.

## Setup (Windows)

The `MetaTrader5` package is Windows-only and talks to a terminal running on the same machine.

```bash
pip install -r requirements.txt -r requirements-mt5.txt
```

1. Install and open the MT5 terminal; log in to a **demo** account first.
2. `Tools` → `Options` → `Expert Advisors` → enable **Allow algorithmic trading**.
3. Copy credentials:

   ```bash
   copy .env.example .env
   ```

   ```
   MT5_LOGIN=12345678
   MT5_PASSWORD=your-demo-password
   MT5_SERVER=YourBroker-Demo
   # MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
   ```

   Use the **investor (read-only) password** while validating — it cannot place a trade even if the
   code tries to.
4. Switch the config to MT5 and disable synthetic data:

   ```yaml
   broker:
     mode: "mt5"
   data:
     source: "mt5"          # or "csv" to feed it your own history
   ```
5. Confirm what the bot thinks it may do:

   ```bash
   python run.py doctor
   ```

## Recommended progression

1. **Backtest** on months of real M1 history. Look at expectancy in R, not net profit.
2. **Walk-forward**: `python run.py backtest --walk-forward 4`. An edge that only exists in one
   slice of history is not an edge.
3. **Replay**: `python run.py paper --max-bars 5000` re-runs the engine bar-by-bar with simulated
   fills and prints a session report. Cheap, instant, catches logic surprises.
4. **MT5 demo, signal-only**: set `risk.risk_per_trade_pct` to something tiny (0.01) and watch real
   orders flow for a week. Check the fills against your expectations in the terminal's History tab.
5. **MT5 demo, full size**: leave it running for at least a month. Compare the bot's trade log with
   `results/*_trades.csv`.
6. **Small live account.** Start at a size where a bad week is survivable.

## What the bot does with the terminal

- **SL and TP are attached to the order**, so the venue holds them server-side and keeps working if
  your process dies. The engine detects positions the venue closed (`_reconcile_live`) rather than
  simulating exits.
- **A magic number** (`broker.mt5.magic`, default 20260921) tags every order. The bot ignores any
  position with a different magic, so **your manual trades are never touched**.
- **Contract specs come from `symbol_info`**, not the config: pip size, tick value and lot limits are
  read from the terminal, so sizing mathematics matches the broker's own.
- **Requotes and unsupported filling modes are retried**; hard rejections raise with the terminal's
  own wording and the entry is skipped (never forced).
- **`deviation_points` caps slippage**: a market order that cannot fill within that many points is
  rejected rather than filled at a bad price.

## Before your first demo session

- [ ] `python run.py doctor` shows `live routing: disabled (safe)` when you are *not* intending live.
- [ ] `SCALPER_ALLOW_LIVE` is unset or `no` while you test paper mode.
- [ ] The account is a **demo** account (`python run.py doctor` does not tell you this — check the
      terminal's title bar).
- [ ] `risk.risk_per_trade_pct` is small enough that 20 consecutive losses would still be annoying
      rather than fatal.
- [ ] `risk.max_daily_loss_pct` and `risk.max_drawdown_pct` are set. They are your last line of
      defence when a strategy behaves in a way the backtest never showed.
- [ ] The symbols in `instruments:` exist in Market Watch **under exactly that name** — brokers add
      suffixes (`EURUSD.a`, `EURUSDm`, `EURUSD-ECN`).
- [ ] `python run.py paper --max-bars 2000` has run cleanly, with no broker errors in the log.

## Stopping

`Ctrl-C` triggers a graceful shutdown: open positions are flattened, a session report is written, and
the log states what happened. If the process is killed hard, your SL/TP still live on the server —
which is exactly why they are attached to the order rather than managed in memory.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Refusing to trade: this MT5 account is a REAL-money account` | Working as designed. Use a demo account, or set all three gates if you truly mean it. |
| `Could not connect to MetaTrader 5` | Terminal not running / not logged in / Algo Trading disabled / wrong `MT5_PATH`. |
| `MT5 does not offer symbol 'EURUSD'` | Wrong symbol name — use the exact Market Watch spelling. |
| `volume ... below the broker minimum` | Risk per trade is too small for the stop distance, or the account balance is too low for a 0.01 lot. |
| `Order rejected ... retcode=10019` | "No money": free margin insufficient for the size. |
| Positions open but nothing closes them | The bot reconciles venue-side closes each poll; check the terminal's History for the reason. |
