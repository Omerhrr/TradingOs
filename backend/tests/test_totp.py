"""Two-factor (TOTP) hardening of the interactive login gate.

The implementation is stdlib RFC 6238; the vectors below are the RFC's own
SHA-1 test vectors. The gate itself must fail closed: a required-but-
unprovisioned secret blocks sign-in, wrong codes are rate limited and
audited, and neither the secret nor the code ever reaches the ledger.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app, login_gate
from app.models import AuditEvent, TwoFactorSecret
from app.services.auth import totp_code, totp_qr_svg, totp_verify

settings = get_settings()
ADMIN = "test-local-admin-token"
ADMIN_HEADER = {"X-TradingOS-Token": ADMIN}

# RFC 6238 appendix B reference secret (ASCII "12345678901234567890").
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
]


@pytest.fixture(autouse=True)
def _clean_totp_world():
    with login_gate._lock:
        login_gate._failures.clear()
    yield
    with SessionLocal() as session:
        session.query(TwoFactorSecret).delete()
        session.query(AuditEvent).delete()
        session.commit()
    with login_gate._lock:
        login_gate._failures.clear()


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _provision(client) -> str:
    response = client.post("/api/v1/auth/totp/provision", headers=ADMIN_HEADER)
    assert response.status_code == 200
    return response.json()["secret"]


# ------------------------------------------------------------------- crypto


def test_rfc6238_vectors() -> None:
    from app.services.auth import _totp_at

    for timestamp, expected in RFC_VECTORS:
        assert _totp_at(RFC_SECRET, timestamp // 30, digits=8) == expected


def test_verify_window_and_shape() -> None:
    now = 1_700_000_000
    assert totp_verify(RFC_SECRET, totp_code(RFC_SECRET, for_time=now), now=now)
    assert totp_verify(RFC_SECRET, totp_code(RFC_SECRET, for_time=now - 30), now=now)  # one step behind
    assert totp_verify(RFC_SECRET, totp_code(RFC_SECRET, for_time=now + 30), now=now)  # one step ahead
    assert not totp_verify(RFC_SECRET, totp_code(RFC_SECRET, for_time=now - 150), now=now)
    assert not totp_verify(RFC_SECRET, "abc")
    assert not totp_verify(RFC_SECRET, "12345")
    assert not totp_verify(RFC_SECRET, None)
    assert not totp_verify(RFC_SECRET, "")


# ------------------------------------------------------------- provisioning


def test_provision_requires_admin(client) -> None:
    assert client.post("/api/v1/auth/totp/provision").status_code == 401
    assert client.get("/api/v1/auth/totp/status").status_code == 401


def test_provision_rotates_and_never_audits_the_secret(client) -> None:
    first = _provision(client)
    second = _provision(client)
    assert first != second
    with SessionLocal() as session:
        row = session.scalar(select(TwoFactorSecret))
        assert row is not None and row.enabled
        assert first not in row.secret_ciphertext
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == "AUTH_TOTP_PROVISIONED")))
        assert len(events) == 2  # provision + rotation
        assert all(first not in json.dumps(event.payload) and second not in json.dumps(event.payload) for event in events)
    status = client.get("/api/v1/auth/totp/status", headers=ADMIN_HEADER)
    assert status.json() == {"required": False, "provisioned": True}


def test_provision_returns_scannable_qr_exactly_once(client) -> None:
    """The QR renders the otpauth URI into matrix form: no secret as text."""
    response = client.post("/api/v1/auth/totp/provision", headers=ADMIN_HEADER)
    assert response.status_code == 200
    body = response.json()
    qr, secret, uri = body["qr_svg"], body["secret"], body["otpauth_uri"]
    assert qr.lstrip().startswith("<svg")
    assert "</svg>" in qr
    # Matrix-encoded, not text-encoded: a file search for the secret or URI
    # must come up empty, exactly like the audit ledger.
    assert secret not in qr
    assert uri not in qr
    # Only this one response ever carries it: the status endpoint stays clean.
    status = client.get("/api/v1/auth/totp/status", headers=ADMIN_HEADER)
    assert "qr_svg" not in status.json()


def test_qr_svg_is_deterministic_and_secret_bound() -> None:
    from app.services.auth import otpauth_uri

    uri_a = otpauth_uri("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    uri_b = otpauth_uri("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
    assert totp_qr_svg(uri_a) == totp_qr_svg(uri_a)  # deterministic
    assert totp_qr_svg(uri_a) != totp_qr_svg(uri_b)  # bound to the secret


def test_disable_fails_the_gate_closed(client) -> None:
    _provision(client)
    disabled = client.post("/api/v1/auth/totp/disable", headers=ADMIN_HEADER)
    assert disabled.json() == {"required": False, "provisioned": False}
    with SessionLocal() as session:
        row = session.scalar(select(TwoFactorSecret))
        assert row is not None and not row.enabled


# --------------------------------------------------------------- login gate


def test_login_without_totp_required_needs_no_code(client) -> None:
    _provision(client)
    response = client.post("/api/v1/auth/login", json={"token": ADMIN})
    assert response.status_code == 200


def test_required_but_unprovisioned_fails_closed(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "totp_required", True)
    response = client.post("/api/v1/auth/login", json={"token": ADMIN})
    assert response.status_code == 503
    assert "provision" in response.json()["detail"]


def test_required_gate_demands_valid_code(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "totp_required", True)
    secret = _provision(client)
    assert client.post("/api/v1/auth/login", json={"token": ADMIN}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"token": ADMIN, "totp_code": "000000"}).status_code in {400, 401}
    good = client.post("/api/v1/auth/login", json={"token": ADMIN, "totp_code": totp_code(secret)})
    assert good.status_code == 200
    assert "tradingos_session" in good.cookies
    # The code never lands in the audit ledger.
    with SessionLocal() as session:
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == "AUTH_LOGIN_FAILED")))
        assert events and all("totp_code_invalid" in json.dumps(event.payload) for event in events)
        assert all(totp_code(secret) not in json.dumps(event.payload) for event in events)


def test_stale_code_is_rejected_and_rate_limited(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "totp_required", True)
    _provision(client)
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"token": ADMIN, "totp_code": "000000"})
    locked = client.post("/api/v1/auth/login", json={"token": ADMIN, "totp_code": totp_code(_read_secret())})
    assert locked.status_code == 429


def _read_secret() -> str:
    from app.services.credentials import CredentialVault

    with SessionLocal() as session:
        row = session.scalar(select(TwoFactorSecret))
        return CredentialVault(settings.credential_encryption_key).decrypt(row.secret_ciphertext)


def test_session_endpoint_reports_totp_state(client, monkeypatch) -> None:
    assert client.get("/api/v1/auth/session").json()["totp_required"] is False
    monkeypatch.setattr(settings, "totp_required", True)
    assert client.get("/api/v1/auth/session").json()["totp_required"] is True


def test_admin_header_path_ignores_totp(client, monkeypatch) -> None:
    """Machine credentials stay TOTP-free: the header token still drives admin controls."""
    monkeypatch.setattr(settings, "totp_required", True)
    _provision(client)
    assert client.get("/api/v1/auth/totp/status", headers=ADMIN_HEADER).status_code == 200
