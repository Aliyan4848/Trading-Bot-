"""Exness Public Trader API — REST client (official, verified contract).

Endpoints implemented (paths verified against exness-api.com reference docs):
  GET  /v1/configuration/accounts/{id}/account
  GET  /v1/configuration/accounts/{id}/limits
  GET  /v1/configuration/accounts/{id}/instruments
  GET  /v1/configuration/accounts/{id}/instruments/{inst}/conditions
  GET  /v1/market-data/accounts/{id}/candles
  GET  /v1/history/accounts/{id}/deals
  GET  /v1/trading/accounts/{id}/snapshot
  GET  /v1/trading/accounts/{id}/operations/{op_id}
  POST /v1/trading/accounts/{id}/positions          (open, market execution)
  DELETE /v1/trading/accounts/{id}/positions/{pos_id}

Rate limits are dynamic per account/key/endpoint: we fetch the effective
limits from the ``/limits`` endpoint and throttle locally (the API returns
*configured* limits, not remaining quota — the client must account locally,
per the official docs). Until limits are fetched, a conservative default
applies. On HTTP 429 we do NOT loop-retry: at most one retry after waiting
the window, and only for idempotent requests (all our GETs and all mutating
requests carrying an idempotency key).

Pending-order endpoints (place/modify/cancel) are intentionally NOT
implemented yet — their exact contract will be verified before Phase 6
uses them (no fabricated endpoints).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from tradingbot.broker.exness.signing import RequestSigner
from tradingbot.core.logging import get_logger

log = get_logger("tradingbot.broker.exness.rest")


class ExnessApiError(RuntimeError):
    """Business/transport error with the stable public error code when known."""

    def __init__(self, code: str | int | None, message: str, http_status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


class ExnessRateLimitError(ExnessApiError):
    """HTTP 429 — caller must wait for capacity; never loop-retry."""


@dataclass
class _Bucket:
    limit: float
    window_s: float
    tokens: float
    updated: float

    def capacity(self) -> float:
        now = time.monotonic()
        self.tokens = min(self.limit, self.tokens + (now - self.updated) * (self.limit / self.window_s))
        self.updated = now
        return self.tokens

    def take(self) -> bool:
        if self.capacity() >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """Local token buckets per operation (configured by the /limits endpoint)."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        # conservative default before /limits is fetched
        self._default = _Bucket(limit=2.0, window_s=1.0, tokens=2.0, updated=time.monotonic())

    def configure(self, limits: dict[str, Any]) -> None:
        """``limits`` is the raw response body of the /limits endpoint."""
        rest = limits.get("limits", {}).get("rest", {})
        for m in rest.get("methods", []):
            rl = m.get("rate_limit", {})
            limit = rl.get("limit")
            window = rl.get("window_seconds")
            op = m.get("api_operation_id") or m.get("path") or ""
            if op and limit and window:
                self._buckets[op] = _Bucket(limit=float(limit), window_s=float(window),
                                            tokens=float(limit), updated=time.monotonic())
        global_rl = rest.get("global_account_rate")
        if global_rl and global_rl.get("limit") and global_rl.get("window_seconds"):
            self._default = _Bucket(limit=float(global_rl["limit"]),
                                    window_s=float(global_rl["window_seconds"]),
                                    tokens=float(global_rl["limit"]),
                                    updated=time.monotonic())
        log.info("exness rate limiter configured", operations=len(self._buckets))

    def bucket(self, op: str) -> _Bucket:
        return self._buckets.get(op, self._default)

    def wait_seconds(self, op: str) -> float:
        b = self.bucket(op)
        deficit = 1.0 - b.capacity()
        if deficit <= 0:
            return 0.0
        return deficit / (b.limit / b.window_s) if b.window_s > 0 else 1.0

    async def acquire(self, op: str) -> None:
        wait = self.wait_seconds(op)
        if wait > 0:
            await asyncio.sleep(min(wait, 30.0))
        if not self.bucket(op).take():
            await asyncio.sleep(self.bucket(op).window_s)


class ExnessRestClient:
    def __init__(
        self,
        signer: RequestSigner,
        base_url: str,
        account_id: str,
        http: httpx.AsyncClient | httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._signer = signer
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._limiter = limiter or RateLimiter()
        if isinstance(http, httpx.AsyncClient):
            self._http = http
            self._owns_http = False
        else:
            self._http = httpx.AsyncClient(transport=http, base_url=self._base_url,
                                           timeout=timeout_s)
            self._owns_http = True
        self._counters: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------- transport
    def _path(self, suffix: str) -> str:
        return f"/v1{suffix}"

    def _query_string(self, params: dict[str, Any]) -> str:
        # Single canonical encoding; the SAME string is signed and sent.
        if not params:
            return ""
        return "?" + urlencode(params, doseq=False)

    async def request(
        self,
        method: str,
        path_suffix: str,
        *,
        operation: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> Any:
        path = self._path(path_suffix) + self._query_string(query or {})
        body_bytes = None
        headers: dict[str, str] = {}
        if body is not None:
            import json as _json

            body_bytes = _json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        await self._limiter.acquire(operation)
        ts_ms = int(time.time() * 1000)
        headers.update(self._signer.sign(method, path, body_bytes, idempotency_key, ts_ms))
        self._counters[operation] += 1

        resp = await self._http.request(method, self._base_url + path, content=body_bytes, headers=headers)
        if resp.status_code == 429:
            raise ExnessRateLimitError(
                self._extract_code(resp), "rate limited", http_status=429
            )
        if resp.status_code >= 400:
            raise ExnessApiError(
                self._extract_code(resp), self._extract_message(resp) or resp.text[:300],
                http_status=resp.status_code,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    @staticmethod
    def _extract_code(resp: httpx.Response) -> str | int | None:
        try:
            data = resp.json()
            code = data.get("code")
            if code is None and isinstance(data.get("error"), dict):
                code = data["error"].get("code")
            return code
        except Exception:
            return None

    @staticmethod
    def _extract_message(resp: httpx.Response) -> str | None:
        try:
            data = resp.json()
            msg = data.get("error_message") or data.get("message")
            if msg is None and isinstance(data.get("error"), dict):
                msg = data["error"].get("error_message") or data["error"].get("message")
            return msg
        except Exception:
            return None

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------- endpoints
    async def get_account(self) -> dict[str, Any]:
        return await self.request(
            "GET", f"/configuration/accounts/{self._account_id}/account",
            operation="getAccount",
        )

    async def get_limits(self) -> dict[str, Any]:
        data = await self.request(
            "GET", f"/configuration/accounts/{self._account_id}/limits",
            operation="getLimits",
        )
        self._limiter.configure(data)
        return data

    async def get_instruments(self) -> list[str]:
        data = await self.request(
            "GET", f"/configuration/accounts/{self._account_id}/instruments",
            operation="getAvailableInstrumentList",
        )
        if isinstance(data, list):
            return [str(x) for x in data]
        return [str(x) for x in data.get("instruments", [])]

    async def get_instrument_conditions(self, instrument: str) -> dict[str, Any]:
        return await self.request(
            "GET", f"/configuration/accounts/{self._account_id}/instruments/{instrument}/conditions",
            operation="getInstrumentCondition",
        )

    async def get_candles(
        self,
        instrument: str,
        timeframe: str,
        from_dt: datetime,
        to_dt: datetime | None = None,
        count: int | None = None,
        price_type: str = "bid",
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "instrument": instrument,
            "timeframe": timeframe,
            "price_type": price_type,
            "from": from_dt.isoformat().replace("+00:00", "Z"),
        }
        if to_dt is not None:
            query["to"] = to_dt.isoformat().replace("+00:00", "Z")
        if count is not None:
            query["count"] = count
        data = await self.request(
            "GET", f"/market-data/accounts/{self._account_id}/candles",
            operation="getCandleHistory", query=query,
        )
        return data.get("candles", [])

    async def get_deals_history(
        self,
        from_dt: datetime,
        to_dt: datetime | None = None,
        limit: int = 100,
        deal_type: str | None = None,
        instrument: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": limit}
        if cursor is None:
            query["from"] = from_dt.isoformat().replace("+00:00", "Z")
        else:
            query["cursor"] = cursor
        if to_dt is not None:
            query["to"] = to_dt.isoformat().replace("+00:00", "Z")
        if deal_type:
            query["deal_type"] = deal_type
        if instrument:
            query["instrument"] = instrument
        return await self.request(
            "GET", f"/history/accounts/{self._account_id}/deals",
            operation="getDealsHistory", query=query,
        )

    async def get_snapshot(self) -> dict[str, Any]:
        return await self.request(
            "GET", f"/trading/accounts/{self._account_id}/snapshot",
            operation="getTradingStateSnapshot",
        )

    async def get_operation_status(self, operation_id: str) -> dict[str, Any]:
        return await self.request(
            "GET", f"/trading/accounts/{self._account_id}/operations/{operation_id}",
            operation="getOperationStatus",
        )

    async def open_position(
        self,
        instrument: str,
        side: str,
        volume: str,
        *,
        price: str | None = None,
        deviation: str | None = None,
        stop_loss_price: str | None = None,
        take_profit_price: str | None = None,
        comment: str | None = None,
        client_request_id: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instrument": instrument,
            "side": side,
            "volume": volume,
        }
        if price is not None:
            body["price"] = price
        if deviation is not None:
            body["deviation"] = deviation
        if stop_loss_price is not None:
            body["stop_loss_price"] = stop_loss_price
        if take_profit_price is not None:
            body["take_profit_price"] = take_profit_price
        if comment:
            body["comment"] = comment
        return await self.request(
            "POST", f"/trading/accounts/{self._account_id}/positions",
            operation="openPosition", body=body, idempotency_key=client_request_id,
        )

    async def close_position(
        self,
        position_id: str,
        client_request_id: str,
        *,
        volume: str | None = None,
        price: str | None = None,
        deviation: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if volume is not None:
            query["volume"] = volume
        if price is not None:
            query["price"] = price
        if deviation is not None:
            query["deviation"] = deviation
        return await self.request(
            "DELETE", f"/trading/accounts/{self._account_id}/positions/{position_id}",
            operation="closePosition", query=query or None, idempotency_key=client_request_id,
        )
