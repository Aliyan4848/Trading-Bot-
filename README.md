# AI Trading Bot

An AI-assisted **demo** trading system for Exness. It collects broker market
data, generates strategy signals on short timeframes (5–10 s bar analysis
built from ticks), passes every decision through a **deterministic
risk-management service**, executes through the broker, and exposes a real-time
dashboard.

> **This is a research/education system for demo accounts. It is not financial
> advice. No version of this software guarantees profit. Live trading does not
> exist in this codebase — `TRADING_MODE` only accepts `simulation` or `demo`,
> and the settings validator refuses to start otherwise.**

## Repository layout

```
tradingbot/            Python backend + trading engine (single package)
  core/                config, structured logging, event bus, time utils
  broker/              broker abstraction: models, interface, paper adapter
                       (Exness API + MT5 adapters land in Phases 3/10)
  engine/              market state: tick → 5s/10s/60s bar aggregation
  db/                  SQLAlchemy models + session factory
  api/                 FastAPI service: REST + WebSocket hub + auth
  worker/              trading engine loop entrypoint
alembic/               database migrations
frontend/              Next.js 15 + TypeScript + Tailwind dashboard (Vercel)
tests/                 pytest suite
docs/                  phase reports and research
```

## Safety model (highest priority)

- **No live mode exists.** `TRADING_MODE=simulation|demo`, enforced by the
  config validator; `ALLOW_LIVE_TRADING=true` prevents startup.
- **Exness broker is demo-only in code** (`EXN_ACCOUNT_IS_DEMO=true` required).
- **Deterministic risk service** has final authority over every order; the
  optional AI layer only *recommends* and can never bypass:
  max risk/trade, max daily loss, max open positions, max consecutive losses,
  max spread, max slippage, trade frequency caps, session windows,
  mandatory stop-loss, and the **global kill switch**.
- **Idempotent execution**: every order carries a unique
  `client_request_id`; duplicates are rejected/returned, never re-sent.
- **Execution verification**: an order is only recorded as opened after the
  broker ACK + fill event (or MT5 fill confirmation).

## Quickstart (development)

```bash
# 1. Python environment (Python 3.11+)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Environment
cp .env.example .env

# 3. Run the API + trading engine (paper broker, synthetic market)
uvicorn tradingbot.api.app:app --host 0.0.0.0 --port 8000

# 4. Dashboard
cd frontend && npm install && npm run dev
# open http://localhost:3000
```

## Tests

```bash
ruff check tradingbot tests
mypy
pytest
```

## Phases

See `docs/PHASE-1-RESEARCH-AND-ARCHITECTURE.md` for the full plan.

| Phase | Status |
|---|---|
| 1 — Audit + broker/API research | done |
| 2 — Project skeleton (this commit) | in progress |
| 3 — Exness broker client (official API, signed REST + WS) | planned |
| 4 — Strategy engine + signals | planned |
| 5 — Deterministic risk service | planned |
| 6 — Execution engine + AI layer | planned |
| 7 — Dashboard sections | planned |
| 8 — DB hardening + crash recovery | planned |
| 9 — Backtesting + simulation | planned |
| 10 — Exness demo connection (gated) | planned |
| 11 — Testing & performance evaluation | planned |
| 12 — Deployment + monitoring | planned |
