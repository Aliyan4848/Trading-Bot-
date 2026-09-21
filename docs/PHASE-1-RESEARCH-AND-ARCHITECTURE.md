# Phase 1 — Repository Audit, Broker Connectivity Research, and Architecture Proposal

**Date:** 2026-09-19
**Repository:** Aliyan4848/Trading-Bot-
**Status:** Research complete — awaiting user confirmation on open questions (Section 9) before code is written.

---

## 1. Repository Audit — What Already Exists

| Item | Finding |
|---|---|
| Files | A single `LICENSE` file (MIT, Copyright (c) 2026 Aliyan Ahmed). Nothing else. |
| Git history | One commit: `a3fcf2d Initial commit` (adds LICENSE). |
| Branches | `main` (+ this session branch `arena/01a0ba1f-trading-bot` branched from `main`). |
| Framework / language | None. No package manifests (`package.json`, `pyproject.toml`, `requirements.txt`), no Docker, no CI. |
| Frontend | None. |
| Backend | None. |
| Environment variables | None. No `.env`, no `.env.example`, no `.gitignore` (must be added before any secrets exist). |
| Database | None. |
| Deployment config | None (no `vercel.json`, no Dockerfile, no CI/CD). |
| Trading-related code | None. |

**Conclusion:** The repository is effectively empty. Nothing needs to be preserved except `LICENSE`. A clean, professional architecture can be established from scratch with no risk of overwriting existing work.

---

## 2. Exness Broker Connectivity Research

### 2.1 Official Exness Public Trader API (PRIMARY candidate)

Source: official Exness Help Center article "Exness API" (get.exness.help) and the official Exness API Developer Portal at **https://www.exness-api.com** (Docusaurus docs, OpenAPI 3.1 spec v1.0.2 downloadable from `https://www.exness-api.com/exness_api.yaml`).

**What it is:** A broker-issued REST + WebSocket API that exposes the same functionality as the Exness Terminal, programmatic and language-agnostic. This is the officially supported direct API — not third-party.

**Authentication (verified from official docs):**
- API Key + Secret Key pair generated in **Key Management** (Personal Area). Secret shown once.
- Every request is **signed with Ed25519** using the private key. Headers: `EXN-API-KEY`, `EXN-IDEMPOTENCY-KEY`, `EXN-TIMESTAMP` (unix ms), `EXN-SIGN-VERSION: 1`, `EXN-DATA` (base64url, no padding, of JSON payload `{api_key, idempotency_key, timestamp, sign_version, method, path, body_hash}`), `EXN-SIGN` (Ed25519 signature of the decoded payload bytes).
- Env var convention used in docs: `EXN_API_KEY`, `EXN_PRIVATE_KEY`, `EXN_ACCOUNT_ID`, `EXN_API_BASE_URL`.
- Access point: host shown in Key Management (DNS widget) or programmatic host discovery (`GET /v1/trading/access-point?account_id=...`). Review hosts: `https://api.exness.com` (main), `https://api.exness-api.com` (alternative). Per-account access points look like `ap-xxxxxxxx.exness.com`.

**Capabilities (verified from reference docs / OpenAPI tags):**

| Capability | Exness API support |
|---|---|
| Market data (real-time) | **WebSocket ticks stream** `…/ws/ticks` (subscribe per instrument). Feed throttled to **100–500 ms** per official help article. |
| Market data (historical) | **REST candle history** (`GET /v1/.../get-candle-history`), limits endpoint exposes `max_candles_per_request`, `history_range_days`, `history_age_days`. |
| Instruments | `GET …/instruments` (list) + `GET …/instruments/{instrument}/conditions` (volume min/max/step, point_digits, etc.). **No stock or index instruments in the current API version** (forex/metals/crypto/energy subject to account eligibility). |
| Account info | Balance, used margin, currency, leverage, margin call / stop out levels (`GET …/account`), plus **trading state snapshot** for reconciliation. |
| Order execution | `POST …/positions` (market execution), `place-pending-order`, `modify-order`, `cancel-pending-order`, `close-position`. |
| Stop-loss / take-profit | **Yes** — `stop_loss_price` / `take_profit_price` on open/modify; `deviation` (max allowed slip in points) supported for instant execution. |
| Execution model | **Async**: mutating requests return an **ACK** (`operation_id`, echoed `client_request_id`, `status: accepted`); final result arrives via **`transaction_event`** on the WS events stream or `GET …/operation-status`. Public async error codes include `EXECUTION_REQUOTE`, `EXECUTION_REJECTED`, `MARKET_SESSION_CLOSED`, `TRADING_RULE_INSUFFICIENT_MARGIN`, `TRADING_RULE_TOO_MANY_OPEN_POSITIONS`, `ACCOUNT_CLOSE_ONLY`, etc. |
| Idempotency / duplicate prevention | **Built in**: mutating requests require `EXN-IDEMPOTENCY-KEY` (5–128 chars, `[A-Za-z0-9._~-]`). Reusing a key with the same payload returns the original `operation_id` without re-executing; same key with a different payload is rejected. |
| Trade history | `get-deals-history`, `get-orders-history` (closed orders/deals retained ~1 month; open state comes from snapshot + events). |
| Rate limits | **Dynamic, per account/key/endpoint** via `GET /v1/configuration/accounts/{account_id}/limits`. WS limits per node; subscription operation token buckets. Docs explicitly say: never hardcode limits; client must throttle locally; on HTTP 429 do not loop-retry. |
| Recovery semantics | WS streams have **no durable replay/resume tokens** — after reconnect: resubscribe, then reconcile against trading snapshot (open positions + pending orders). Documented pattern. |
| Demo compatibility | **UNVERIFIED — see critical caveat below.** |

**Critical caveat — account eligibility (from the official help center article):**
> "Only **Exness trading accounts** are eligible to use the Exness API. **Standard MetaTrader accounts cannot connect to this API.**"
> Connection flow requires: (1) registered Exness trading account, (2) **full verification (POI + POA)**, (3) **a first deposit** depending on account type and country of residence, (4) API key generated in Key Management.

Interpretation: the API is issued for the modern "Exness trading account" platform (Exness Terminal), **not** for legacy MT4/MT5 login/password accounts. Whether a **demo** Exness-platform account satisfies the verification/deposit prerequisites is **not stated in the public docs**. The demo environment does exist on the Exness platform (demo mode in Exness Terminal / Exness Trader app), so this is plausibly supported, but it must be verified from the user's own Personal Area: **if "Key Management" is visible for a demo account, the API is available; if not, fall back to MT5 (2.2).** This is the single most important open question for the project (see Section 9).

### 2.2 MetaTrader 5 integration (FALLBACK candidate)

- **Official tooling:** MetaQuotes' official **`MetaTrader5` Python package** (pip, from MetaQuotes — mql5.com integration docs). It communicates via IPC with a **running MT5 terminal**.
- **Platform limitation (decisive):** the official package is **Windows-only**. Linux/macOS are not officially supported (Wine workarounds are fragile and unsuitable for a persistent production worker). This forces a Windows machine or Windows VPS to host the terminal + bot.
- **Exness MT5 demo:** fully supported. Demo servers follow the `Exness-MT5Trial*` naming (e.g., `Exness-MT5Trial9`), live `Exness-MT5Real*`. Login = account number + password + server name. Demo accounts need no deposit or KYC.
- **Capabilities:** `account_info()`, `terminal_info()`, `symbol_info_tick()` (bid/ask), `copy_ticks()`, `copy_rates_from_pos()` (candles, **minimum timeframe M1** — no native sub-minute candles), `order_send()` (market/limit/stop, SL/TP, deviation, magic number, comment), `position_get()`, `history_deals_get()`/`history_orders_get()`, `trade_statistics()`, async transaction checks via `order_get()`.
- **Third-party ecosystem (do NOT rely on):** numerous community wrappers exist (e.g., `PythonMetaTrader5`, `mt5-connector` for NautilusTrader, various MCP servers). These wrap the official package; they are **not** officially supported by Exness or MetaQuotes and add supply-chain risk. If the MT5 path is needed, use the official `MetaTrader5` package directly.
- **MT4:** Exness still offers MT4, but there is **no official Python package for MT4**; automation is MQL4 EAs or unofficial bridges. Not recommended.

### 2.3 Comparison and decision

| Criterion | Exness Public API | MT5 (official Python pkg) |
|---|---|---|
| Officially supported | Yes (Exness) | Yes (MetaQuotes; Exness supports MT5 platform) |
| Demo account access | **Unverified — user must check Key Management on demo** | Yes (free, no KYC/deposit) |
| Hosting | Any OS, any cloud (language-agnostic) | **Windows only**, terminal must stay running |
| Real-time prices | WS tick stream, 100–500 ms throttle | Polling `symbol_info_tick` / `copy_ticks` (no push stream) |
| Sub-minute candles | Not native (ticks + candles); client-side 5–10 s aggregation needed | Not native (ticks + M1+); client-side aggregation needed |
| SL/TP on order | Yes | Yes |
| Idempotency | **Built into API** | Client-side only (magic number + order tracking) |
| Execution verification | ACK → `transaction_event`/operation status (explicit) | `order_send` result + `order_get` polling (explicit) |
| Trade history | Yes (deals, orders) | Yes (deals, orders) |
| Rate limits | Dynamic via `/limits` endpoint | n/a (local terminal; still keep throughput sane) |
| Instrument scope | Forex/metals/crypto/energy (no stocks/indices) | Same broker scope, incl. stocks/indices if on account |
| Cost | Free (broker API) | Free; but Windows VPS hosting cost if dedicated |

**Decision:**
1. **Primary integration: Exness Public Trader API** — best fit for every hard requirement (language-agnostic, deployable anywhere, WS ticks, built-in idempotency, explicit async execution verification, dynamic limits).
2. **Fallback: official `MetaTrader5` Python package** on a Windows host, if the user's demo account cannot obtain API keys.
3. **Architecture safeguard:** both are implemented behind a single `BrokerClient` interface (Phase 3), so the choice is a configuration decision, not a code rewrite.
4. **No third-party broker SDK** will be used for execution. No external market-data API will ever be used for execution or as the price source for order decisions (see Section 3 — broker feed is authoritative).

### 2.4 Feasibility of the 5–10 second timeframe (honest assessment)

- **Neither integration provides native 5–10 second candles.** Both provide ticks (Exness: push stream at 100–500 ms; MT5: fast polling). The strategy engine will aggregate ticks into client-side 5 s / 10 s bars for candle analysis, and use M1/M5 candles for context.
- The 100–500 ms tick throttle is **comfortably sufficient** for a 5–10 s decision cadence (each decision sees 10–100 fresh ticks).
- **Economic risk:** at 5–10 s horizons, spread + commission + slippage are a large fraction of typical price movement. A strategy that does not clear round-trip costs will lose money even with a high win rate. This is exactly why backtesting on real tick data and paper trading must precede any demo execution (Phase 9), and why no profitability is promised.
- Session/risk controls (no trading in low-liquidity sessions, max spread gates, event-calendar avoidance) are first-class features of the risk service, not afterthoughts.

---

## 3. Market-Data / Finance API Research (public-apis Finance section)

Method: pulled the current `Finance` section of `public-apis/public-apis` (raw README, 2026-09-19) — **60+ listed APIs** — then shortlisted candidates relevant to forex/short-timeframe work and verified free-tier claims via provider docs. **Do-not-use note:** IEX Cloud is listed but discontinued free access (2024) — dropped. All "free" claims below were verified this session unless marked *(unverified)*.

| # | API | Official site | Free tier (verified) | Key | Rate limit (free) | Instruments | Real-time vs delayed | Historical | WebSocket | Short-timeframe fit | Demo fit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **Exness API (broker)** | exness-api.com | Yes (broker API) | API key + Ed25519 secret | Dynamic via `/limits`; ticks 100–500 ms | Forex, metals, crypto, energy (no stocks/indices) | **Real-time** (authoritative execution feed) | Candles (limits per account) | **Yes** (ticks + events) | **Primary source** | Yes (subject to eligibility check) |
| 2 | **Twelve Data** | twelvedata.com | Yes — Basic | Yes | 8 credits/min, ~800/day | FX, crypto, US stocks | Real-time (FX covered on free) | Yes (years, per market) | Mostly paid | Cross-validation / chart history only | Yes |
| 3 | **Alpha Vantage** | alphavantage.co | Yes | Yes | **25 req/day**, 5/min | Stocks, FX, crypto | Real-time-ish, free data can lag | Yes (intraday→monthly) | No | Too low throughput for anything live; daily research only | Yes |
| 4 | **Finnhub** | finnhub.io | Yes (non-commercial) | Yes | 60 req/min | Stocks (US real-time), FX, crypto, news | Real-time US stocks; FX mostly EOD | Yes (limited depth on free) | Yes (US stocks) | Sentiment/news context only | Yes (non-commercial) |
| 5 | **FRED (St. Louis Fed)** | fred.stlouisfed.org | Yes | Yes | 120 req/min | Macro indicators (CPI, rates, M2…) | **Daily or slower — not intraday** | Yes (decades) | No | Macro regime filter only | Yes |
| 6 | **Econdb** | econdb.com | Yes, **no key** | No | Generous (self-throttle) | Global macro series | Daily+; some series delayed | Yes | No | Macro regime filter | Yes |
| 7 | **FXNewsBias** | fxnewsbias.com | Yes — 25 req/UTC day, key free, no card | Yes | 25/day (free); 3-hour **delayed** | 8 major currencies | Delayed (3 h) on free | — | No | **News-sentiment gate** (never an entry signal) | Yes (non-commercial) |
| 8 | **EconPulse** | econpulse.io | *(unverified — key required)* | Yes | — | CPI, PPI, energy, treasuries, BTC premium | Live | — | — | Macro events | Verify before use |
| 9 | **EODHD** | eodhd.com | Yes (small daily credits, *(limit unverified this session)*) | Yes | ~100 credits/day *(verify)* | 150+ exchanges incl. FX | Real-time on paid; EOD on free | Yes | No | History import for backtests (one-off) | Verify before use |
| 10 | **TickerLayer** | tickerlayer.com | Yes *(1,000 credits/mo — unverified this session)* | Yes | ~1,000 credits/mo *(verify)* | Stocks, **FX**, crypto | Real-time | Yes | Yes | Quote cross-check | Verify before use |
| 11 | **ExchangeRate-API** | exchangerate.host / exchange-rate-api | Yes — 1,500 req/mo | Yes | 1,500/mo | 170+ currencies, daily rates | **Daily**, not real-time | Limited on free | No | Dashboard currency conversion only | Yes |
| 12 | **FMP** | financialmodelingprep.com | Yes — ~250 req/day | Yes | 250/day | Stocks/funds; FX limited | Delayed/real-time mixed | Yes | No | Not needed (FX not core) | — |
| 13 | **Yahoo Finance API** | yahoofinanceapi.com | Yes (small daily allotment, *(unverified)*) | Yes | ~100/day *(verify)* | Stocks, FX, crypto | Real-time low-latency claims | Yes | No | Optional quote cross-check | Verify before use |
| 14 | **IG** | labs.ig.com | Demo available | Yes | Moderate | FX/CFD (IG's own market) | Real-time | Yes | **Yes** (streamer) | **Execution API — but different broker**; excluded: user's broker is Exness | — |
| 15 | **Alpaca / Tradier / SmartAPI** | alpaca.markets / trader / angel broking | Yes | Yes | Moderate | US equities (Alpaca/Tradier), NSE (SmartAPI) | Real-time (US) | Yes | Yes | **Execution APIs — different brokers/markets**; excluded for same reason | — |

**Recommendation (no forced free-API dependency):**
- **All prices for strategy and execution come from the Exness broker feed** (ticks + candles). It is authoritative, real-time, free, and the only feed where execution actually happens. Mixing an external quote into order decisions would create basis risk between decision and execution.
- **Optional, cheap, non-critical supplements** (each independently swappable, all disabled by default in config):
  - **FXNewsBias** (free, 3 h delayed) — sentiment *gate*: if news bias strongly opposes a signal direction, suppress the trade. Never an entry signal.
  - **FRED** (free, daily) — macro regime context (rate direction, DXY-style strength) shown in the dashboard and logged on each decision.
  - **Twelve Data** (free) — optional cross-validation of quotes and dashboard chart history; used only to *display* consistency, never to trigger trades.
- **No external API performs trade execution.** Execution exists only through the Exness broker integration. Verified: the only execution-capable entries in the free list are other brokers (IG, Alpaca, Tradier, SmartAPI) and are out of scope.

---

## 4. Technology Stack Recommendation

| Layer | Choice | Rationale |
|---|---|---|
| Frontend | **Next.js 15 (App Router) + TypeScript + Tailwind CSS + shadcn/ui** | User requirement; Vercel-native; best-in-class responsive dashboards; charts via **lightweight-charts** (TradingView's open-source charting, ideal for price panels). |
| Backend API | **FastAPI (Python 3.12)** | Async-native (needed for WS + broker client), typed (Pydantic), auto OpenAPI docs for the dashboard API. Serves both REST and the WebSocket hub. |
| Trading worker | **Python 3.12 async process** (separate entrypoint, same codebase) | Shares broker client, strategy engine, risk service with the API — one language, no IPC impedance. Process separation keeps the 5–10 s loop off the API's request path. |
| Broker client | **`exness` package (in-repo)**: signed REST client (Ed25519 via `PyNaCl`) + `websockets` WS client; later `mt5` adapter behind the same interface | Official spec only; no third-party broker SDK. |
| Strategy/indicators | **pandas + NumPy** (own thin indicator library: SMA/EMA/RSI/ATR/MACD/Bollinger, candle patterns, S/R, session logic) | No heavyweight TA frameworks (TA-Lib build friction, `pandas-ta` unmaintained). Deterministic, unit-testable, vectorized for backtests. |
| Database | **PostgreSQL 16** (events, trades, signals, account snapshots, risk state, config) | ACID for money-critical records; `LISTEN/NOTIFY` optional; JSONB for flexible payloads. Dev: Docker Compose; prod: on the worker host or managed (Railway/Neon). |
| Real-time to dashboard | **WebSocket hub in FastAPI** (positions, ticks, signals, risk events, system logs) | Single reliable mechanism; no polling for anything time-sensitive. |
| AI agent | **LLM via OpenAI-compatible API (optional module)** | Structured-input/structured-output (JSON schema) recommendation layer only; deterministic risk layer is downstream and final. Provider decided in Phase 6 (see open questions). |
| Testing | **pytest + pytest-asyncio + hypothesis**; deterministic seeded tick generator; backtester with realistic cost model | Requirement: no completion claims without verification. |
| Packaging/deps | **uv** (lockfile, fast, reproducible) for Python; **pnpm** for frontend | No `pip install` drift; lockfiles committed. |
| Containers | **Dockerfile + docker-compose** (worker, api, db) for local dev; single-image deploy for the worker host | One-command local setup; the same image runs in production. |
| Vercel | Frontend only (`vercel.json` optional; standard Next.js deploy) | **No persistent loops on Vercel** — hard rule. |

---

## 5. System Architecture

```
                        ┌────────────────────────────────────────────────────────┐
                        │                        Vercel                          │
                        │   Next.js dashboard (TypeScript, no secrets)           │
                        │   REST + WebSocket consumer (via NEXT_PUBLIC_API_URL)  │
                        └───────────────▲───────────────────────────────▲────────┘
                                        │ HTTPS (JWT auth)              │ WSS
┌───────────────────────────────────────┴───────────────────────────────┴─────────┐
│                        WORKER HOST (Hetzner VPS or equivalent)                 │
│                                                                                 │
│  ┌───────────────────────────────┐    ┌──────────────────────────────────────┐  │
│  │  FastAPI service              │    │  Trading worker (separate process)   │  │
│  │  • REST API (auth: JWT)       │    │  • Market data collector (WS ticks)  │  │
│  │  • WebSocket hub              │◄──►│  • 5s/10s bar aggregator             │  │
│  │  • Config / risk settings     │    │  • Strategy engine → Signal          │  │
│  │  • Kill switch, pause         │    │  • AI analysis layer (optional)      │  │
│  └──────────────┬────────────────┘    │  • Deterministic risk service (FINAL)│  │
│                 │                     │  • Execution engine (idempotent)     │  │
│                 │                     │  • Position monitor / reconciler     │  │
│                 │                     └──────────────┬───────────────────────┘  │
│                 │                                    │ signed REST + WS         │
│  ┌──────────────▼────────────────┐                   ▼                          │
│  │  PostgreSQL 16                │        ┌─────────────────────┐               │
│  │  trades, signals, risk_events,│        │  Exness broker API  │               │
│  │  account_snapshots, logs,     │        │  (primary) or MT5   │               │
│  │  config, order_journal        │        │  adapter (fallback) │               │
│  └───────────────────────────────┘        └─────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Trade lifecycle (state machine, persisted per order):**
`SIGNAL_GENERATED → RISK_APPROVED / RISK_REJECTED → ORDER_SUBMITTED → ORDER_ACCEPTED (ACK) → POSITION_OPENED | EXECUTION_ERROR/REJECTED`. Close: `POSITION_CLOSED` (SL/TP hit, manual, or strategy exit). Every transition is a row in `order_journal` + `system_logs`; the dashboard shows the exact state, never a guess.

**Risk service rules (deterministic, in-process + DB-backed, AI can never bypass):**
demo-only mode lock, max risk %/trade, max daily loss, max open positions, max consecutive losses, max spread, max slippage (deviation), max trade frequency, max trades/day, min equity, mandatory SL, trading-session windows, repeated-failure auto-pause, **global KILL SWITCH** (DB flag + instant API route; stops new orders immediately, optionally flattens), position-size validation against broker instrument conditions.

**Environment-level live-trade prevention:** `TRADING_MODE` ∈ {`simulation`, `demo`} — **there is no `live` value in v1 code.** The broker client refuses to start unless mode is non-live, the account currency/margin check confirms a demo environment where detectable, and an explicit env flag `ALLOW_LIVE_TRADING=false` is hard-required.

---

## 6. Hosting Research (trading worker — persistent process)

Vercel **cannot** host the trading loop (serverless = no persistent connections, no always-on state). The worker needs an always-on Linux VM (primary API path) — verified 2026 pricing:

| Option | Monthly cost (approx.) | Notes |
|---|---|---|
| **Hetzner CAX11 / CX22 (recommended)** | **€3.29–4.59** | Flat cost, no egress sensitivity, always-on Linux, best price/perf for a demo-grade worker. |
| Fly.io (shared-cpu-1x/2x) | ~$2–8 | Nice WS story, regions; no free tier anymore (2025+); pay-as-you-go. |
| Railway (Hobby) | $5 + usage | Easiest DX, built-in Postgres option; usage meter. |
| Render (Starter/Standard) | $7 / $25 | Fine, pricier per spec than Hetzner. |
| Oracle Cloud Always Free (ARM 4 vCPU/24 GB) | $0 | Most generous free tier, but shared capacity risk (reboot/reschedule) — acceptable only for local dev, not the worker of record. |
| Windows VPS (DO / Scaleway / OVH) — **only if MT5 fallback** | ~$12–25 | Required solely because the official MT5 Python package is Windows-only. |

**Database:** Postgres runs on the same VPS (docker-compose) for v1 — simple, zero egress cost, trivially backed up. Managed Postgres (Railway/Neon) is a later option if scale demands.

**Frontend:** Vercel free/Pro as user specified; dashboard consumes the worker host's API over HTTPS/WSS with JWT auth; secrets live only on the worker host and in Vercel's non-secret config (`NEXT_PUBLIC_API_URL`).

---

## 7. Limitations and Risks (explicit, no sugar-coating)

1. **Exness API demo eligibility is unverified.** Public docs tie API access to verified, funded "Exness trading accounts" and exclude MT accounts. If the user's demo account cannot generate an API key, we use the MT5 fallback (Windows hosting cost, polling instead of push ticks). **Must be confirmed from the user's Personal Area before Phase 3.**
2. **No native 5–10 s candles from any Exness path.** We aggregate ticks client-side. That's standard practice but means "candle analysis at 5 s" is our computation, not broker data.
3. **Short-horizon economics are the hardest risk.** Spread + slippage dominate 5–10 s moves; most such strategies are net-negative after costs. The backtester will model round-trip costs from *measured* Exness spreads/slippage, and we will report cost-adjusted metrics. No profitability is promised or implied.
4. **Demo ≠ live.** Demo spreads/slippage differ from live under stress; any future live migration requires its own safety review (out of scope for v1 — live mode does not exist in v1 code at all).
5. **External "free" APIs are unreliable for trading.** Verified: free tiers are heavily capped (Alpha Vantage 25/day, FXNewsBias 3 h delay, Twelve Data 8/min). They are used only as non-critical supplements and are individually disable-able.
6. **API is new (v1.0.2, hosts marked "review/prototype" in docs).** Contracts can shift; we pin to the OpenAPI spec, keep the client thin, and reconcile from snapshots on every reconnect (the documented recovery pattern).
7. **Ed25519 request signing is intricate** (base64url no-padding, body hashing, exact query-string preservation). This is the trickiest implementation detail of Phase 3 and gets exhaustive unit tests (including test vectors from the docs).
8. **Single-host dependency** for v1 (worker + API + DB on one VM). Mitigation: nightly DB backups, worker watchdog/auto-restart (systemd/supervisor), health checks. Acceptable at demo scale.
9. **No guaranteed profitability; no AI override of risk.** The AI layer (when added) only *recommends* on structured data; the deterministic risk service has final say and can block any AI-approved trade.
10. **Security surface:** JWT dashboard auth, broker keys only in server env vars (never in frontend, never in Git), `.gitignore` added in Phase 2 before any secret exists, API key IP-allowlisting if the portal supports it.

---

## 8. Development Phases (mapped to the requested 12)

| Phase | Work | Verification gate |
|---|---|---|
| **1** ✅ (this doc) | Repo audit; Exness connectivity research; market-data API research; architecture proposal | This document + user confirmations |
| **2** | Project skeleton: monorepo layout (`frontend/`, `worker/`, `packages/shared` or single Python pkg + Next app), uv + pnpm, Docker compose, `.gitignore`, `.env.example` (placeholders only), CI (lint + typecheck + tests), DB schema v1 (SQLAlchemy models + Alembic), `BrokerClient` interface | Empty app boots locally via `docker compose up`; CI green |
| **3** | Exness broker client: Ed25519 signing (unit-tested against doc vectors), host discovery, `/limits` bootstrap, account/instrument endpoints, WS ticks + events, reconnect/resubscribe/snapshot-reconcile; **paper adapter** (simulated fills on real ticks) for pre-credentials dev; optional MT5 adapter stub | Signing tests pass; live read-only smoke test with user's key (account info + 60 s tick capture) |
| **4** | Strategy engine: 5 s/10 s bar aggregation, indicators (SMA/EMA/RSI/ATR/MACD/Bollinger, S/R, candle patterns, session detection), signal contract (BUY/SELL/HOLD + full audit fields), no-trade conditions, config via YAML/DB | Indicator unit tests (known values); golden-signal replay tests |
| **5** | Risk service: all controls from Section 5, kill switch, state machine, DB-backed persistence, risk-settings API with safe-change validation | Property-based tests: no violating trade can pass (hypothesis); kill switch integration test |
| **6** | Execution engine: order builder with SL/TP/deviation from instrument conditions, idempotency key management, ACK→event correlation, slippage tracking, duplicate prevention, timeout/recovery, failed-order handling; AI analysis layer (optional, structured JSON in → recommendation out, provider TBD) | Simulated end-to-end runs; error-injection tests (requote, reject, timeout, disconnect mid-order) |
| **7** | Dashboard: Overview, Market Monitor (live chart), Signals, Open Positions, Trade History, Risk Controls, System Logs, Settings — all sections from the brief | Responsive Lighthouse pass; E2E smoke (Playwright) against seeded data |
| **8** | Database hardening + logging: event sourcing for orders, account snapshots (30 s), structured JSON logs, retention policy | Crash-recovery test: kill -9 worker mid-trade → restart → state reconciles correctly |
| **9** | Backtesting & simulation: historical candle+tick import (Exness API + optional Twelve Data), cost model from measured spread/slippage, full metric suite (win rate, PF, max DD, avg duration, costs, slippage, Sharpe/Sortino), clear separation of backtest vs paper vs demo results | Backtest of known strategy reproduces expected metrics; metrics unit tests |
| **10** | Connect Exness demo account: user provides credentials/env; read-only → paper → live-demo execution gates (each requires explicit approval); env-level live lock verified | Gate 1: account read OK; Gate 2: paper matches live tick stream; Gate 3: N demo trades, all reconciled |
| **11** | Testing & performance evaluation: multi-day paper/demo run, performance report, risk-event audit, error-simulation battery | Written performance report with cost-adjusted metrics; no unresolved risk events |
| **12** | Deploy: Vercel (frontend) + worker host (API + worker + DB), monitoring (health endpoint, uptime, log shipping), security review checklist, runbook | Pre-deploy checklist from the brief fully executed; 72 h monitored soak |

**Rules honored throughout:** no live mode exists in v1; AI never bypasses risk; no secrets in repo/frontend; no completion claims without a passing verification gate; existing work preserved (only `LICENSE` exists and stays).

---

## 9. Open Questions (need user confirmation before Phase 2/3)

1. **Exness API demo eligibility** — Does your Personal Area show **Key Management** (and can a demo account be selected as the key's account)? This decides primary vs fallback integration.
2. **Worker hosting** — OK to provision a **Hetzner VPS (~€4/mo)** for the worker? Alternatives: Fly.io, Railway, or Oracle Free (capacity risk). (MT5 fallback would instead need a Windows VPS ~$12–25/mo.)
3. **Instruments & sessions** — Which instruments to target initially (e.g., EURUSD, GBPUSD, XAUUSD)? Preferred trading sessions (London/NY overlap)?
4. **AI layer** — Start deterministic-only and add the LLM analysis layer in Phase 6 (recommended)? If so, which provider (OpenAI gpt-4o-mini / Anthropic Haiku class — ~$1–5/mo at demo volume)?
