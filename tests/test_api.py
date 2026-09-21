"""API tests: health, auth, account, kill switch persistence, websocket stream."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tradingbot.api.app import create_app


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_health_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["broker"] == "paper"
    assert body["mode"] == "simulation"
    assert body["instruments"] == ["EURUSD", "GBPUSD", "XAUUSD"]


def test_protected_routes_require_token(client):
    assert client.get("/api/v1/account").status_code == 401
    assert client.get("/api/v1/positions").status_code == 401
    assert client.get("/api/v1/risk").status_code == 401
    r = client.get("/api/v1/account", headers=_auth("wrong"))
    assert r.status_code == 401


def test_account_available_after_snapshot(client, settings):
    headers = _auth(settings.api_token)
    deadline = time.time() + 10
    body = None
    while time.time() < deadline:
        r = client.get("/api/v1/account", headers=headers)
        if r.status_code == 200:
            body = r.json()
            break
        time.sleep(0.1)
    assert body is not None, "account info never became available"
    assert body["currency"] == "USD"
    assert body["balance"] == pytest.approx(settings.paper_start_balance, abs=1.0)
    assert body["leverage"] == settings.paper_leverage


def test_positions_empty_initially(client, settings):
    r = client.get("/api/v1/positions", headers=_auth(settings.api_token))
    assert r.status_code == 200
    assert r.json() == []


def test_kill_switch_persists_and_reports(client, settings):
    headers = _auth(settings.api_token)
    r = client.post("/api/v1/risk/kill-switch", headers=headers, json={"enabled": True, "reason": "test"})
    assert r.status_code == 200
    assert r.json()["kill_switch"] is True

    risk = client.get("/api/v1/risk", headers=headers).json()
    assert risk["kill_switch"] is True
    assert risk["kill_reason"] == "test"

    r = client.post("/api/v1/risk/kill-switch", headers=headers, json={"enabled": False})
    assert r.json()["kill_switch"] is False
    risk = client.get("/api/v1/risk", headers=headers).json()
    assert risk["kill_switch"] is False


def test_pause_toggle(client, settings):
    headers = _auth(settings.api_token)
    r = client.post("/api/v1/risk/pause", headers=headers, json={"paused": True, "reason": "lunch"})
    assert r.status_code == 200
    risk = client.get("/api/v1/risk", headers=headers).json()
    assert risk["trading_paused"] is True
    assert risk["pause_reason"] == "lunch"


def test_market_endpoint_returns_ticks_and_bars(client, settings):
    headers = _auth(settings.api_token)
    # 1) wait for the first tick
    deadline = time.time() + 10
    body = None
    while time.time() < deadline:
        r = client.get("/api/v1/market/EURUSD?timeframe=5&limit=50", headers=headers)
        assert r.status_code == 200
        body = r.json()
        if body["last_tick"] is not None:
            break
        time.sleep(0.05)
    assert body is not None and body["last_tick"] is not None, "no ticks arrived"
    assert body["instrument"] == "EURUSD"
    assert body["last_tick"]["ask"] > body["last_tick"]["bid"]

    # 2) wait for a closed 5s bar
    deadline = time.time() + 15
    bars = []
    while time.time() < deadline:
        r = client.get("/api/v1/market/EURUSD?timeframe=5&limit=50", headers=headers)
        bars = r.json()["bars"]
        if bars:
            break
        time.sleep(0.5)
    assert bars, "no 5s bars produced within 15s"
    bar = bars[-1]
    assert bar["timeframe_s"] == 5
    assert bar["high"] >= max(bar["open"], bar["close"])
    assert bar["low"] <= min(bar["open"], bar["close"])
    assert bar["volume_ticks"] >= 1


def test_unknown_instrument_404(client, settings):
    r = client.get("/api/v1/market/NOPE", headers=_auth(settings.api_token))
    assert r.status_code == 404


def test_websocket_stream_with_token(client, settings):
    with client.websocket_connect(f"/ws?token={settings.api_token}") as ws:
        seen = set()
        deadline = time.time() + 15
        while time.time() < deadline and "tick" not in seen:
            msg = ws.receive_json()
            seen.add(msg["type"])
        assert "tick" in seen


def test_websocket_rejects_bad_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=bad") as ws:
        ws.receive_json()
