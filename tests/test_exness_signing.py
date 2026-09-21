"""Unit tests for Exness Ed25519 request signing (official contract)."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from nacl.signing import SigningKey

from tradingbot.broker.exness.signing import (
    EMPTY_BODY_HASH,
    RequestSigner,
    b64url,
    decode_exn_data,
)

API_KEY = "EXNDEMO1234567890"
SEED = base64.b64encode(bytes(range(32)))


@pytest.fixture
def signer() -> RequestSigner:
    return RequestSigner(API_KEY, SEED.decode())


def test_empty_body_hash_matches_official_vector() -> None:
    assert EMPTY_BODY_HASH == "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"
    assert b64url(hashlib.sha256(b"").digest()) == EMPTY_BODY_HASH


def test_b64url_has_no_padding() -> None:
    for payload in (b"a", b"ab", b"abc", b"abcd", b"\x00\xff" * 17):
        assert "=" not in b64url(payload)


def test_sign_produces_all_required_headers(signer: RequestSigner) -> None:
    headers = signer.sign("GET", "/v1/configuration/accounts/123/limits", None, "", 1_700_000_000_000)
    assert set(headers) == {
        "EXN-API-KEY", "EXN-IDEMPOTENCY-KEY", "EXN-TIMESTAMP",
        "EXN-SIGN-VERSION", "EXN-DATA", "EXN-SIGN",
    }
    assert headers["EXN-API-KEY"] == API_KEY
    assert headers["EXN-SIGN-VERSION"] == "1"
    assert headers["EXN-TIMESTAMP"] == "1700000000000"
    assert headers["EXN-IDEMPOTENCY-KEY"] == ""  # empty for signed GETs


def test_exn_data_fields_and_empty_body_hash(signer: RequestSigner) -> None:
    ts = 1_700_000_123_456
    headers = signer.sign("GET", "/v1/trading/accounts/123/snapshot", None, "", ts)
    data = decode_exn_data(headers["EXN-DATA"])
    assert data == {
        "api_key": API_KEY,
        "idempotency_key": "",
        "timestamp": ts,
        "sign_version": 1,
        "method": "GET",
        "path": "/v1/trading/accounts/123/snapshot",
        "body_hash": EMPTY_BODY_HASH,
    }


def test_signature_verifies_over_decoded_data_bytes(signer: RequestSigner) -> None:
    path = "/v1/configuration/accounts/123/limits"
    headers = signer.sign("GET", path, None, "", 1_700_000_000_000)
    raw = base64.urlsafe_b64decode(headers["EXN-DATA"] + "=" * (-len(headers["EXN-DATA"]) % 4))
    verifying = SigningKey(base64.b64decode(SEED)).verify_key
    verifying.verify(raw, base64.urlsafe_b64decode(headers["EXN-SIGN"] + "=" * (-len(headers["EXN-SIGN"]) % 4)))


def test_fixed_timestamp_is_deterministic() -> None:
    s1 = RequestSigner(API_KEY, SEED.decode())
    s2 = RequestSigner(API_KEY, SEED.decode())
    h1 = s1.sign("GET", "/v1/x", None, "", 555)
    h2 = s2.sign("GET", "/v1/x", None, "", 555)
    assert h1 == h2  # same inputs -> identical headers


def test_body_hash_covers_exact_body_bytes(signer: RequestSigner) -> None:
    body = json.dumps({"instrument": "EURUSD", "side": "buy"}, separators=(",", ":")).encode()
    headers = signer.sign("POST", "/v1/trading/accounts/123/positions", body, "crid-1", 1)
    data = decode_exn_data(headers["EXN-DATA"])
    assert data["body_hash"] == b64url(hashlib.sha256(body).digest())
    assert data["idempotency_key"] == "crid-1"
    assert data["method"] == "POST"


def test_query_string_is_signed_verbatim(signer: RequestSigner) -> None:
    path = "/v1/market-data/accounts/123/candles?count=10&from=2026-09-20T00:00:00Z&instrument=EURUSD&price_type=bid&timeframe=S5"
    headers = signer.sign("GET", path, None, "", 7)
    assert decode_exn_data(headers["EXN-DATA"])["path"] == path


def test_path_must_be_absolute(signer: RequestSigner) -> None:
    with pytest.raises(ValueError, match="absolute"):
        signer.sign("GET", "v1/x", None, "", 1)


def test_private_key_must_be_32_bytes() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        RequestSigner(API_KEY, base64.b64encode(b"short").decode())
