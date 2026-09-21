"""REST client tests against an in-process fake Exness server (httpx.MockTransport).

The fake re-verifies the Ed25519 signature on every request (decoding EXN-DATA
and checking EXN-SIGN with the public key), so these tests exercise the real
wire contract, not just our own parsing.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx
import pytest
from nacl.signing import SigningKey, VerifyKey

from tradingbot.broker.exness.rest import (
    ExnessApiError,
    ExnessRateLimitError,
    ExnessRestClient,
)
from tradingbot.broker.exness.signing import EMPTY_BODY_HASH, RequestSigner

API_KEY = "EXNTESTKEY0000000001"
SEED = base64.b64encode(bytes(range(32)))
ACCOUNT_ID = "152706877"

ACCOUNT_DETAIL = {"account": {"id": ACCOUNT_ID, "currency": "USD", "leverage": "500"}}
LIMITS = {
    "limits": {
        "rest": {
            "global_account_rate": {"limit": 100, "window_seconds": 1},
            "methods": [
                {"api_operation_id": "getTradingStateSnapshot",
                 "rate_limit": {"limit": 10, "window_seconds": 1}},
            ],
        }
    }
}
CONDITIONS = {
    "instrument": "EURUSD",
    "point_digits": 5,
    "contract_size": "100000",
    "margin_currency": "USD",
    "volume_min": "0.01",
    "volume_max": "100",
    "volume_step": "0.01",
    "trade_mode": "enabled",
}

captured: list[dict[str, Any]] = []


def _verify_signature(request: httpx.Request, verifying: VerifyKey) -> None:
    headers = {k.upper(): v for k, v in request.headers.items()}
    for h in ("EXN-API-KEY", "EXN-TIMESTAMP", "EXN-SIGN-VERSION", "EXN-DATA", "EXN-SIGN"):
        assert h in headers, f"missing header {h}"
    assert headers["EXN-API-KEY"] == API_KEY
    data_b64 = headers["EXN-DATA"]
    raw = base64.urlsafe_b64decode(data_b64 + "=" * (-len(data_b64) % 4))
    sign_b64 = headers["EXN-SIGN"]
    sig = base64.urlsafe_b64decode(sign_b64 + "=" * (-len(sign_b64) % 4))
    verifying.verify(raw, sig)
    data = json.loads(raw)
    # path (incl. query) must be exactly as transmitted
    query = request.url.query.decode()
    assert data["path"] == request.url.path + (f"?{query}" if query else "")
    assert data["method"] == request.method
    expected_hash = b64u(hashlib.sha256(request.content).digest())
    assert data["body_hash"] == expected_hash
    if request.method == "GET":
        assert expected_hash == EMPTY_BODY_HASH
    assert str(data["timestamp"]) == headers["EXN-TIMESTAMP"]
    assert data["idempotency_key"] == headers["EXN-IDEMPOTENCY-KEY"]
    if request.method in ("GET",):
        assert data["idempotency_key"] == ""


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_handler(verifying: VerifyKey, state: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        _verify_signature(request, verifying)
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "body": request.content,
                "idempotency": request.headers.get("EXN-IDEMPOTENCY-KEY", ""),
            }
        )
        path, query = request.url.path, dict(request.url.params)
        if path == f"/v1/configuration/accounts/{ACCOUNT_ID}/account":
            return httpx.Response(200, json=ACCOUNT_DETAIL)
        if path == f"/v1/configuration/accounts/{ACCOUNT_ID}/limits":
            return httpx.Response(200, json=LIMITS)
        if path == f"/v1/configuration/accounts/{ACCOUNT_ID}/instruments":
            return httpx.Response(200, json={"instruments": ["EURUSD", "GBPUSD", "XAUUSD"]})
        if path == f"/v1/configuration/accounts/{ACCOUNT_ID}/instruments/EURUSD/conditions":
            return httpx.Response(200, json=CONDITIONS)
        if path == f"/v1/market-data/accounts/{ACCOUNT_ID}/candles":
            assert query["instrument"] == "EURUSD"
            assert query["timeframe"] == "S5"
            return httpx.Response(
                200,
                json={"candles": [{"open_time": "2026-09-20T00:00:00Z", "open": "1.08",
                                   "high": "1.081", "low": "1.079", "close": "1.0805"}]},
            )
        if path == f"/v1/history/accounts/{ACCOUNT_ID}/deals":
            return httpx.Response(200, json={
                "items": [{
                    "event_time": "2026-09-20T00:00:01Z",
                    "deal": {
                        "deal_id": "9001", "type": "open", "direction": "buy",
                        "instrument": "EURUSD", "volume": "0.01", "price": "1.08340",
                        "position_id": "7001", "create_time": "2026-09-20T00:00:01Z",
                    },
                }],
                "has_more": False,
            })
        if path == f"/v1/trading/accounts/{ACCOUNT_ID}/snapshot":
            return httpx.Response(200, json=state["snapshot"])
        if path.startswith(f"/v1/trading/accounts/{ACCOUNT_ID}/operations/"):
            op_id = path.rsplit("/", 1)[1]
            op = state["operations"].get(op_id)
            if op is None:
                return httpx.Response(404, json={"code": 3014, "error_message": "REQUEST_OPERATION_NOT_FOUND"})
            return httpx.Response(200, json=op)
        if path == f"/v1/trading/accounts/{ACCOUNT_ID}/positions" and request.method == "POST":
            body = json.loads(request.content)
            op_id = state["next_op"]
            state["next_op"] += 1
            state["opens"].append(body)
            assert request.headers.get("EXN-IDEMPOTENCY-KEY", "") != ""
            return httpx.Response(
                202,
                json={"operation_id": str(op_id), "client_request_id": request.headers["EXN-IDEMPOTENCY-KEY"],
                      "status": "accepted"},
            )
        if path.startswith(f"/v1/trading/accounts/{ACCOUNT_ID}/positions/") and request.method == "DELETE":
            pid = path.rsplit("/", 1)[1]
            if pid not in state["open_positions"]:
                return httpx.Response(400, json={"code": 3013, "error_message": "REQUEST_POSITION_NOT_FOUND"})
            op_id = state["next_op"]
            state["next_op"] += 1
            state["closes"].append({"position_id": pid, "query": query})
            return httpx.Response(
                202,
                json={"operation_id": str(op_id), "client_request_id": request.headers["EXN-IDEMPOTENCY-KEY"],
                      "status": "accepted"},
            )
        if path == "/__rate_limit":
            return httpx.Response(429, json={"code": 3015, "error_message": "REQUEST_RATE_LIMIT"})
        return httpx.Response(500, json={"code": 3, "error_message": "SYSTEM_INTERNAL_ERROR"})

    return httpx.MockTransport(handler)


@pytest.fixture
def state() -> dict[str, Any]:
    return {
        "next_op": 9000,
        "snapshot": {"orders": [], "positions": [], "account_state":
                     {"balance": "10000", "equity": "10000", "used_margin": "0"}},
        "operations": {},
        "opens": [],
        "closes": [],
        "open_positions": {"7001"},
    }


@pytest.fixture
def rest(state) -> ExnessRestClient:
    http = make_handler(SigningKey(base64.b64decode(SEED)).verify_key, state)
    return ExnessRestClient(
        RequestSigner(API_KEY, SEED.decode()), "http://api.exness.test", ACCOUNT_ID, http=http
    )


async def test_get_account_and_limits_configure_rate_limiter(rest: ExnessRestClient) -> None:
    account = await rest.get_account()
    assert account["account"]["currency"] == "USD"
    limits = await rest.get_limits()
    assert limits["limits"]["rest"]["global_account_rate"]["limit"] == 100
    # after /limits, the snapshot bucket is the per-operation one
    assert rest._limiter.bucket("getTradingStateSnapshot").limit == 10.0
    assert rest._limiter.bucket("unknownOp").limit == 100.0  # global fallback


async def test_open_position_sends_canonical_body_and_idempotency_key(rest: ExnessRestClient, state: dict) -> None:
    ack = await rest.open_position(
        "EURUSD", "buy", "0.10", price="1.08340", stop_loss_price="1.07900",
        client_request_id="crid-open-1",
    )
    assert ack == {"operation_id": "9000", "client_request_id": "crid-open-1", "status": "accepted"}
    body = state["opens"][-1]  # already parsed by the fake handler
    assert body == {"instrument": "EURUSD", "side": "buy", "volume": "0.10",
                    "price": "1.08340", "stop_loss_price": "1.07900"}
    assert captured[-1]["idempotency"] == "crid-open-1"


async def test_close_position_uses_delete_with_signed_query(rest: ExnessRestClient, state: dict) -> None:
    ack = await rest.close_position("7001", "crid-close-1", volume="0.10")
    assert ack["status"] == "accepted"
    entry = state["closes"][-1]
    assert entry["position_id"] == "7001"
    assert entry["query"] == {"volume": "0.10"}
    # signed path must have included the query (checked in _verify_signature)


async def test_close_position_full_omits_query(rest: ExnessRestClient, state: dict) -> None:
    await rest.close_position("7001", "crid-close-2")
    assert state["closes"][-1]["query"] == {}


async def test_error_codes_are_extracted(rest: ExnessRestClient, state: dict) -> None:
    with pytest.raises(ExnessApiError) as e:
        await rest.close_position("9999", "crid-bad")
    assert e.value.code == 3013
    assert e.value.http_status == 400

    with pytest.raises(ExnessApiError) as e2:
        await rest.get_operation_status("nope")
    assert e2.value.code == 3014  # REQUEST_OPERATION_NOT_FOUND


async def test_429_raises_rate_limit_error() -> None:
    http = httpx.MockTransport(
        lambda r: httpx.Response(429, json={"code": 3015, "error_message": "REQUEST_RATE_LIMIT"})
    )
    client = ExnessRestClient(RequestSigner(API_KEY, SEED.decode()), "http://x.test", "1", http=http)
    with pytest.raises(ExnessRateLimitError) as e:
        await client.get_account()
    assert e.value.code == 3015
    assert e.value.http_status == 429
