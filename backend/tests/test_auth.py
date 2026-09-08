"""Auth-gated remote access: sessions, lockout, the HTTP gate, and the WS gate.

With ``TRADINGOS_REMOTE_ACCESS_ENABLED`` off, the app keeps its local-first
behavior (reads open, admin actions need the header token). With it on, every
request needs the admin token or a login session — including the live
WebSocket — and failed sign-ins are rate limited per source and audited.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app, login_gate
from app.models import AuditEvent
from app.services.auth import SessionManager

settings = get_settings()
ADMIN = "test-local-admin-token"


@pytest.fixture(autouse=True)
def _reset_login_gate():
    """The in-memory lockout must never leak between tests (one shared client IP)."""
    with login_gate._lock:
        login_gate._failures.clear()
    yield
    with login_gate._lock:
        login_gate._failures.clear()


@pytest.fixture()
def remote_on(monkeypatch):
    monkeypatch.setattr(settings, "remote_access_enabled", True)
    return settings


def _login(client: TestClient, token: str = ADMIN):
    return client.post("/api/v1/auth/login", json={"token": token})


# ------------------------------------------------------------------ sessions


def test_session_roundtrip() -> None:
    manager = SessionManager(settings)
    token, expires_at = manager.issue()
    assert manager.verify(token) is True
    assert expires_at.timestamp() > time.time()


def test_tampered_session_is_rejected() -> None:
    manager = SessionManager(settings)
    token, _ = manager.issue()
    head, sig = token.rsplit(".", 1)
    forged = f"{head}.{sig[:-2]}aa"
    assert manager.verify(forged) is False
    assert manager.verify(token.replace("v1", "v9", 1)) is False
    assert manager.verify(None) is False


def test_expired_session_is_rejected() -> None:
    key = hashlib.sha256(f"tradingos-session-v1:{ADMIN}".encode()).digest()
    message = f"v1.{int(time.time()) - 3600}.deadbeef"
    signature = base64.urlsafe_b64encode(hmac.new(key, message.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    expired = f"{message}.{signature}"
    assert SessionManager(settings).verify(expired) is False


# --------------------------------------------------------------- login flow


def test_login_success_sets_session_cookie_and_audits() -> None:
    with TestClient(app) as client:
        response = _login(client)
    assert response.status_code == 200
    body = response.json()
    assert body["cookie_name"] == "tradingos_session"
    assert SessionManager(settings).verify(body["session_token"]) is True
    assert "tradingos_session=" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()
    with SessionLocal() as session:
        from sqlalchemy import select

        events = session.scalars(select(AuditEvent).where(AuditEvent.event_type == "AUTH_LOGIN_SUCCESS")).all()
    assert len(events) == 1


def test_login_failure_is_audited_without_leaking_the_attempt() -> None:
    with TestClient(app) as client:
        response = _login(client, token="wrong-secret")
    assert response.status_code == 401
    with SessionLocal() as session:
        from sqlalchemy import select

        events = session.scalars(select(AuditEvent).where(AuditEvent.event_type == "AUTH_LOGIN_FAILED")).all()
    assert len(events) == 1
    assert "wrong-secret" not in events[0].payload


def test_lockout_after_five_failures_even_with_valid_token() -> None:
    with TestClient(app) as client:
        for _ in range(5):
            assert _login(client, token="wrong-secret").status_code == 401
        locked = _login(client, token="wrong-secret")
        assert locked.status_code == 429
        assert "Retry-After" in locked.headers
        # Lockout ignores correctness: the real token is refused too.
        assert _login(client).status_code == 429


def test_successful_login_clears_failure_memory() -> None:
    with TestClient(app) as client:
        for _ in range(4):
            assert _login(client, token="wrong-secret").status_code == 401
        assert _login(client).status_code == 200
        assert _login(client, token="wrong-secret").status_code == 401, "a fresh window started after success"


# ------------------------------------------------------------- HTTP gate


def test_gate_off_keeps_local_first_behavior() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/state").status_code == 200
        assert client.get("/api/v1/health").status_code == 200


def test_gate_on_blocks_anonymous_reads_but_not_health_or_auth(remote_on) -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/state").status_code == 401
        assert client.get("/api/v1/analytics/strategies/compare").status_code == 401
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/auth/session").status_code == 200
        assert client.get("/docs").status_code == 401, "docs leak the API surface and must be gated too"


def test_session_cookie_satisfies_gate_and_admin_endpoints(remote_on) -> None:
    with TestClient(app) as client:
        assert _login(client).status_code == 200
        # httpx keeps the session cookie; the browser flow relies on the same mechanism.
        assert client.get("/api/v1/state").status_code == 200
        paused = client.post("/api/v1/system/pause")
        assert paused.status_code == 200, "a signed-in operator drives admin controls without re-pasting the token"
        session_view = client.get("/api/v1/auth/session").json()
        assert session_view == {"authenticated": True, "remote_access": True, "expires_at": session_view["expires_at"]}
        assert session_view["expires_at"] is not None


def test_admin_token_header_still_works_when_gate_is_on(remote_on) -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/state", headers={"X-TradingOS-Token": ADMIN}).status_code == 200
        assert client.post("/api/v1/system/pause", headers={"X-TradingOS-Token": ADMIN}).status_code == 200


def test_bearer_session_satisfies_gate_and_admin_endpoints(remote_on) -> None:
    with TestClient(app) as client:
        token = _login(client).json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        assert client.get("/api/v1/state", headers=headers).status_code == 200
        assert client.post("/api/v1/system/pause", headers=headers).status_code == 200
        assert client.get("/api/v1/state").status_code == 401


def test_logout_clears_the_cookie(remote_on) -> None:
    with TestClient(app) as client:
        assert _login(client).status_code == 200
        assert client.get("/api/v1/state").status_code == 200
        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.get("/api/v1/state").status_code == 401


# -------------------------------------------------------------- WS gate


def test_ws_requires_credentials_when_remote_access_is_on(remote_on) -> None:
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws/loop") as ws:
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["payload"]["code"] == "unauthorized"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_ws_accepts_query_token_when_remote_access_is_on(remote_on) -> None:
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/v1/ws/loop?access_token={ADMIN}") as ws:
            message = ws.receive_json()
    assert message["type"] == "hello"
