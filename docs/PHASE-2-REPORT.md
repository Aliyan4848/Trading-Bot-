# Phase 2 Report — Project Skeleton

**Date:** 2026-09-19 · **Branch:** `arena/01a0ba1f-trading-bot`

## What was completed

A complete, verified project skeleton — the system boots end-to-end in
**simulation mode** (paper broker + synthetic market) with a live dashboard
API, WebSocket stream, persistence, and a working global kill switch.

### Backend (Python 3.11+, package `tradingbot/`)
| Area | What exists now |
|---|---|
| Config | `Settings` (pydantic-settings) with **enforced safety locks**: `TRADING_MODE` only accepts `simulation`/`demo` (no live value exists), `ALLOW_LIVE_TRADING=true` refuses startup, `BROKER=exness` requires `EXN_ACCOUNT_IS_DEMO=true` |
| Logging | Dependency-free structured JSON logger (`JsonLogger`) |
| Events | In-process async `EventBus` (bounded, consumer-safe drop) |
| Broker layer | `BrokerClient` interface + **`PaperBroker`**: synthetic seeded tick market, async ACK→fill semantics (mirrors the Exness API), idempotent order placement (`client_request_id` + payload fingerprint, conflict = `IDEMPOTENCY_CONFLICT`), SL/TP evaluation per tick (SL wins on tie), settlement & margin accounting, deterministic `force_price` test hook |
| Market state | Tick → **5 s / 10 s / 60 s bar aggregation** with OHLC correctness, bounded deques |
| Engine | Persistent async loop: tick consumer, bar persistence (batched), 30 s account snapshots, heartbeat with kill-switch state |
| DB | SQLAlchemy 2.0 async models: `signals`, `order_journal`, `trades`, `risk_events`, `account_snapshots`, `candles`, `system_logs`, `app_settings` (kill switch persists across restarts); Alembic initial migration generated & verified |
| API | FastAPI: `/health` (unauthenticated), `/api/v1/account|market|positions|signals|trades|risk` (bearer token), `POST /api/v1/risk/kill-switch` + `/pause` (persisted + broadcast), `/ws` real-time event stream; settings read from `app.state` (test-safe) |
| Worker | `python -m tradingbot.worker.main` — the persistent process (API + engine, single async process; documented rationale + process-split escape hatch) |
| Deployment | `Dockerfile` (non-root, `alembic upgrade head && uvicorn …`), `docker-compose.yml` (app + Postgres 16 + healthchecks), GitHub Actions CI (ruff, mypy, pytest ×2 Python versions; frontend typecheck + build) |

### Frontend (`frontend/`, Next.js 15 + TypeScript + Tailwind 4)
- Overview page: live balance/equity/free-margin/positions cards, connection status, 5 s polling
- Risk page: **working kill-switch button** against the live API
- 6 section stubs (Market, Signals, Positions, Trades, Logs, Settings) — built out in Phase 7
- `lib/api.ts` reads `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_API_TOKEN` — **no secrets in the build**

## Files changed (all new)
```
pyproject.toml  .gitignore  .env.example  README.md
Dockerfile  docker-compose.yml  alembic.ini
alembic/env.py  alembic/script.py.mako  alembic/versions/20260919_4bfd2e3d1d1c_initial_schema.py
.github/workflows/ci.yml
tradingbot/__init__.py  core/{config,logging,events,timeutils}.py
tradingbot/broker/{models,interfaces,paper,registry}.py
tradingbot/db/{base,models}.py
tradingbot/engine/{market,engine}.py
tradingbot/api/{app,auth,ws_hub}.py  api/routes/{health,account,market,positions,signals,trades,risk,websocket}.py
tradingbot/worker/main.py
frontend/ (package.json, tsconfig, next.config, postcss, vercel.json, app/*, components/*, lib/api.ts)
tests/ (conftest, test_config, test_paper_broker, test_engine_market, test_api)
docs/PHASE-1-RESEARCH-AND-ARCHITECTURE.md  docs/PHASE-2-REPORT.md
```

## Verification (all run, all passing)
| Check | Result |
|---|---|
| `ruff check tradingbot tests` | ✅ clean |
| `mypy` (32 files) | ✅ no issues |
| `pytest` | ✅ **30 passed** (config safety locks, paper broker fills/idempotency/SL/TP, bar aggregation, API auth/account/kill-switch/WS) |
| Alembic `upgrade head` on fresh SQLite | ✅ 8 tables + `alembic_version` |
| `npm run typecheck` + `next build` | ✅ 11 routes, ~102 kB first load |
| Live smoke test (`python -m tradingbot.worker.main`) | ✅ `/health` ok, 401 without token, account OK, 5 s bars closing at 20 ticks/bar, kill switch engage→persist→release, graceful shutdown |

## Errors found and fixed during this phase
1. **Circular import** `db/base ↔ db/models` — removed the models import from `base.py`.
2. **Duplicate index** `ix_trades_entry_ts` (column `index=True` + explicit `Index`) — removed the explicit one.
3. **SQLite URL parsing** for directory creation (`sqlite:///./data` parsed as `/data`) — now uses `sqlalchemy.engine.make_url`.
4. **Auth design flaw**: routes used the global `get_settings()` LRU cache instead of app-specific settings, and `Depends` isn't supported on WebSocket endpoints — both now read `app.state.settings`.
5. **`_pending` bar dict KeyError** on first tick — `.get()` instead of indexing.
6. Paper broker: duplicated sell-fill expression and a nonexistent method call — fixed before first test run.

## Decisions & deviations (for transparency)
- **Single process for API + engine in v1** (asyncio): one container, zero IPC, engine is non-blocking on the request path. Documented escape hatch: split into a separate process in Phase 12 if profiling shows contention.
- **SQLite for local dev, Postgres for production** (compose/VPS) via `DATABASE_URL` — same code path (SQLAlchemy async).
- Exness/MT5 adapters intentionally **not implemented yet** (Phases 3/10); `registry.py` fails fast with a clear error if selected. The paper broker is the development substrate for Phases 4–9.

## Errors / open items
- None unresolved.
- **Open (user action, needed for Phase 3/10):** confirm whether Key Management is visible for your Exness **demo** account (decides Exness API vs MT5 fallback).
- `package-lock.json` exists in `frontend/` for reproducible CI installs.

## Next steps — Phase 3 (Exness broker client)
1. Ed25519 request-signing module with exhaustive unit tests (doc vectors: body hash of empty body, base64url no-padding, query-string preservation, idempotency header rules).
2. REST client: host/access-point handling, `/limits` bootstrap + client-side throttling, account/instruments/conditions/candles/history/operation-status endpoints.
3. WebSocket client: ticks + events streams, reconnect → resubscribe → trading-snapshot reconciliation (the documented recovery pattern).
4. Wire `ExnessBroker` behind the existing `BrokerClient` interface; add live read-only smoke test (account info + 60 s tick capture) gated on credentials.
5. Paper ↔ Exness adapter comparison tests (same interface contract).
