# Phase 3 Report — Official Exness API Client (Broker Adapter)

Date: 2026-09-21 · Branch: `arena/01a0ba1f-trading-bot`

## Goal

Replace the `BROKER=exness` stub with a complete, contract-verified client for the
official **Exness Public Trader API** (REST + WebSocket), behind the existing
`BrokerClient` interface, so that Phase 10 (demo connection) only needs
credentials + verification — no new adapter code.

## Completed

### 1. Request signing (`tradingbot/broker/exness/signing.py`)
- Ed25519 signing per the official contract: `EXN-API-KEY`, `EXN-IDEMPOTENCY-KEY`,
  `EXN-TIMESTAMP` (ms), `EXN-SIGN-VERSION=1`, `EXN-DATA` (base64url **no padding** of
  compact sorted-keys JSON), `EXN-SIGN` (base64url no padding) over the **decoded**
  EXN-DATA bytes.
- `body_hash` = SHA-256 of exact body bytes (empty-body vector
  `47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU` asserted in tests).
- Path is signed exactly as transmitted (query included) — the same string is signed
  and sent, so normalization drift is impossible.

### 2. REST client (`tradingbot/broker/exness/rest.py`)
- `httpx.AsyncClient` transport (client **or** transport injectable for tests).
- **Dynamic rate limiting**: buckets configured from `GET .../limits`
  (per-operation + global); conservative defaults until fetched. No loop-retry on 429 —
  a 429 raises `ExnessRateLimitError` and the caller decides (per official docs:
  refresh limits, recompute, wait).
- Endpoints (all paths verified against exness-api.com reference):
  account, limits, instruments, instrument conditions, candle history
  (native S1/S5/S15/M1… timeframes — Phase 4 can use broker-native 5s bars),
  deals history (cursor pagination, 10-page cap), trading state snapshot,
  operation status, `POST .../positions` (open), `DELETE .../positions/{id}` (close;
  query `volume` omitted = full close).
- Public error codes (1…6001) extracted from error bodies onto
  `ExnessApiError.code` — `code` is the primary business signal per the docs.
- **Deliberately not implemented**: pending-order place/modify/cancel — their exact
  contract will be verified before Phase 6 uses them (no fabricated endpoints).

### 3. WebSocket client (`tradingbot/broker/exness/ws.py`)
- Two streams: `/v1/server-events/accounts/{id}/ws/ticks` and `/ws/events`.
- Handshake signed as a GET with the EXN-DATA path **byte-identical** to the
  connection path (the single most error-prone contract point — covered by tests).
- Subscribe commands exactly as documented (one message per event type):
  ticks: `{id, subscribe:{event:"ticks", instruments:[…]}}`;
  events: `transactions`, `account_state`, `instruments`.
- Auto-reconnect with exponential backoff (1s → `EXN_WS_RECONNECT_MAX_S`, default 30s),
  resubscribe on every (re)connect. Because the stream has **no replay/resume**, the
  client queues locally-generated `__disconnected__` / `__reconnected__` markers so the
  broker layer re-baselines from a fresh snapshot.
- Defensive ingest: non-JSON / non-object messages and server error frames
  (`{id, code, error_message}`) are logged, not crashed on; queue overflow drops the
  oldest message and warns (slow-reader protection).

### 4. Broker adapter (`tradingbot/broker/exness/broker.py`)
Implements `BrokerClient` with the execution-reliability semantics the rest of the
system already assumes:
- **ACK ≠ executed.** Mutations return `accepted`; final state comes from the
  `transaction_event` WS stream (correlated by `operation_id` **and**
  `client_request_id` — both are envelope-level per the verified schema), with the
  documented REST `operation_status` + snapshot-reconcile fallback when no event
  arrives within `EXN_OP_TIMEOUT_S` (default 10s; operations retained 24h).
- **Idempotency** end-to-end: `client_request_id` is sent as `EXN-IDEMPOTENCY-KEY`;
  local `(crid, payload fingerprint)` check mirrors the paper broker
  (`IDEMPOTENCY_CONFLICT` on key reuse with a different payload).
- **Local state** = full rebuild from `trading_state_snapshot` (sent right after
  subscribing to `transactions`, and again on every reconnect) + incremental
  `transaction_event` payload application. Any WS gap triggers a REST snapshot
  re-baseline. SL/TP values are resolved from `sltp` orders (snapshot and events).
- **Pre-flight validation** against cached instrument conditions: volume min/max/step,
  price/SL/TP precision vs `point_digits`, `trade_mode=trading_disabled` gate;
  decimal-string formatting exactly as the API expects.
- `account_state_event` (~2s) maintains balance/equity/used_margin for `account_info`;
  `instrument_event` keeps conditions fresh.
- Unfilled P&L computed from the latest tick per instrument (contract-size aware).
- Deal history mapped to the shared `Deal` model (open/close only; balance-type deals
  filtered).

### 5. Wiring & config
- `registry.py`: `BROKER=exness` now constructs `ExnessBroker` (stub removed).
- `config.py`: new settings `EXN_REST_TIMEOUT_S` (10), `EXN_WS_RECONNECT_MAX_S` (30),
  `EXN_OP_TIMEOUT_S` (10); safety lock extended — `BROKER=exness` now also fails fast
  without `EXN_API_KEY`/`EXN_PRIVATE_KEY`/`EXN_ACCOUNT_ID` (in addition to the
  existing `EXN_ACCOUNT_IS_DEMO=true` requirement).
- `pyproject.toml`: `pynacl>=1.5` added. `.env.example`: new variables documented.

## Files changed

| File | Change |
| --- | --- |
| `tradingbot/broker/exness/__init__.py` | new (package) |
| `tradingbot/broker/exness/signing.py` | new — Ed25519 request signer |
| `tradingbot/broker/exness/rest.py` | new — REST client, dynamic rate limiter, error model |
| `tradingbot/broker/exness/ws.py` | new — two auto-reconnecting WS streams |
| `tradingbot/broker/exness/broker.py` | new — `ExnessBroker` (BrokerClient) |
| `tradingbot/broker/registry.py` | exness stub → `ExnessBroker` |
| `tradingbot/core/config.py` | 3 new EXN_* settings; exness credential lock |
| `pyproject.toml` | + `pynacl>=1.5` |
| `.env.example` | + EXN tuning vars |
| `tests/test_exness_signing.py` | new — 10 tests (official empty-body vector, b64url, verbatim query, determinism) |
| `tests/test_exness_rest.py` | new — 6 tests (signature re-verified server-side on every request) |
| `tests/test_exness_ws.py` | new — 2 tests (handshake re-verified, subscribe protocol, drop→reconnect→resubscribe→markers) |
| `tests/test_exness_broker.py` | new — 7 integration tests (full ACK→event lifecycle, rejection, idempotency, validation, polling fallback, reconnect re-baseline, history) |
| `tests/test_config.py` | exness lock updated for credential requirement (+1 test) |
| `docs/PHASE-3-REPORT.md` | this report |

## Verification

- `ruff check tradingbot tests` — clean.
- `mypy tradingbot` — clean (37 files).
- `pytest tests` — **56 passed** (30 pre-existing + 26 new), no skips.
  - The fake Exness server (in-process REST mock + `websockets.serve`) **re-verifies
    the Ed25519 signature on every REST and WS handshake** and enforces the
    byte-exact WS path rule, so the tests exercise the real wire contract.
  - Broker integration tests run the genuine async flow: 202 ACK →
    `transaction_event` → final `filled`/`closed` (and `rejected` with error codes),
    plus the REST polling fallback when the event is lost.
- Not verified (needs real credentials): live handshake against `api.exness.com`,
  actual demo account data, host-discovery endpoint (off by default — its contract is
  still "pending clarification" in the official docs).

## Errors found & fixed during this phase

1. `websockets` 17 handshake signing initially omitted the timestamp argument
   (caught by mypy) — WS handshake signature would have been invalid.
2. Reconnect marker logic used a per-iteration flag that was always reset before the
   second connect, so `__reconnected__` was never emitted — rewritten with an
   `ever_connected` flag (caught by the WS reconnect test).
3. Polling-fallback path (`operation_status` → `confirmed`) returned fill details but
   never refreshed local position state — now applies the reconciled snapshot.
4. `ExnessRestClient` accepted only an `AsyncClient` (tests need a transport) —
   constructor now accepts client or transport with correct ownership/close semantics.
5. Test-harness bugs fixed along the way: `VerifyKey` must be derived from the seed
   (`SigningKey(seed).verify_key`), fake-server attribute shadowing, double JSON parse.

## Deviations / decisions

- **Close = `OperationState.CLOSED`** on success (matches paper broker); open =
  `FILLED`. Rejection/failed → `REJECTED` with the public numeric error code as a
  string (`"5006"` = `TRADING_RULE_INSUFFICIENT_MARGIN`, etc.).
- Pending-order endpoints deliberately deferred to Phase 6 (contract verification
  first) — the strategy in Phase 4 only needs market orders.
- Host discovery (`exn_use_host_discovery`) intentionally **not** implemented: the
  official docs mark the discovery endpoint "pending clarification"; defaulting to
  `EXN_API_BASE_URL` (`https://api.exness.com`) is the documented flow.

## Next steps — Phase 4 (Strategy & Signal Engine)

1. Indicators on the 5s/10s/60s bars from `engine/market.py` — plus optional
   **broker-native S5 candles** via `get_candles` (5 days of history available) for
   session context/warm-start.
2. Strategy module: trend/momentum/MA-cross/RSI/ATR-volatility/spread/session filters
   over EURUSD, GBPUSD, XAUUSD → `Signal` objects with full audit fields
   (instrument, timeframe, side, strength, reasons[], indicators snapshot, ts).
3. Signal cadence ~5–10s; every decision logged to `signals` table (already in schema).
4. Optional advisory LLM hook (OpenAI-compatible, `LLM_BASE_URL`/`KEY`/`MODEL`) —
   read-only advice appended to signal audit trail; engine fully functional without it.
5. Unit tests with deterministic synthetic bar sets; no trade authority introduced.
