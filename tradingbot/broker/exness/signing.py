"""Ed25519 request signing for the official Exness Public Trader API.

Contract (verified against the official docs, exness-api.com, 2026-09-19):

  Headers
    EXN-API-KEY          public API key (must equal api_key inside EXN-DATA)
    EXN-IDEMPOTENCY-KEY  empty for signed GET requests; non-empty for mutating
    EXN-TIMESTAMP        unix milliseconds (must equal timestamp inside EXN-DATA)
    EXN-SIGN-VERSION     "1"
    EXN-DATA             base64url (NO padding) of the JSON payload:
                         {api_key, idempotency_key, timestamp, sign_version,
                          method, path, body_hash}
    EXN-SIGN             base64url (NO padding) Ed25519 signature of the
                         DECODED EXN-DATA payload bytes (not the b64 string)

  Rules
    * body_hash = SHA-256 of the exact request body bytes, base64url no padding.
      Empty body => "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU" (doc constant).
    * path is signed with the query string EXACTLY as transmitted — never
      reorder/normalize/re-encode after signing.
    * fresh timestamp + signature per request.

The server validates the signature over the exact EXN-DATA bytes we transmit,
so the JSON serialization is ours to choose; we use compact sorted-keys JSON
for determinism.
"""

from __future__ import annotations

import base64
import hashlib
import json

from nacl.signing import SigningKey

#: SHA-256 of the empty byte sequence, base64url no padding (official doc value).
EMPTY_BODY_HASH = "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"


def b64url(data: bytes) -> str:
    """base64url without '=' padding (API requirement)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class RequestSigner:
    def __init__(self, api_key: str, private_key: str) -> None:
        """private_key: the Ed25519 secret (seed) as standard base64, from Key
        Management. Never logged, never serialized by us beyond signing."""
        if not api_key:
            raise ValueError("exness api key is empty")
        seed = base64.b64decode(private_key, validate=True)
        if len(seed) != 32:
            raise ValueError("exness private key must be a 32-byte Ed25519 seed (base64)")
        self._api_key = api_key
        self._sk = SigningKey(seed)

    @property
    def api_key(self) -> str:
        return self._api_key

    def sign(
        self,
        method: str,
        path: str,
        body: bytes | None,
        idempotency_key: str,
        timestamp_ms: int,
    ) -> dict[str, str]:
        """Return the EXN-* headers for one request.

        ``path`` must include the query string exactly as it will be sent.
        ``method`` is normalized to uppercase (as transmitted).
        """
        if not path.startswith("/"):
            raise ValueError("path must be absolute, e.g. /v1/...")
        body_bytes = body if body is not None else b""
        body_hash = b64url(hashlib.sha256(body_bytes).digest())
        payload = {
            "api_key": self._api_key,
            "idempotency_key": idempotency_key,
            "timestamp": timestamp_ms,
            "sign_version": 1,
            "method": method.upper(),
            "path": path,
            "body_hash": body_hash,
        }
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        data = b64url(payload_bytes)
        signature = self._sk.sign(payload_bytes).signature
        return {
            "EXN-API-KEY": self._api_key,
            "EXN-IDEMPOTENCY-KEY": idempotency_key,
            "EXN-TIMESTAMP": str(timestamp_ms),
            "EXN-SIGN-VERSION": "1",
            "EXN-DATA": data,
            "EXN-SIGN": b64url(signature),
        }


def decode_exn_data(data_b64url: str) -> dict:
    """Test/introspection helper: decode EXN-DATA back to its dict."""
    pad = "=" * (-len(data_b64url) % 4)
    raw = base64.urlsafe_b64decode(data_b64url + pad)
    return json.loads(raw)
