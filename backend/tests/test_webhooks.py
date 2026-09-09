"""Webhook retry policy: persisted deliveries, exponential backoff, exhaustion.

The old path was a single fire-and-forget POST — one network hiccup and the
operator silently never heard about a guard trip. The contract tested here:
every notification is a persisted delivery enqueued atomically with its alert,
a dispatcher retries with exponential backoff under a bounded budget,
exhaustion is marked and audited, an operator retry reopens with a fresh
budget, and payloads are signed when a secret is configured.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app, settings
from app.models import Alert, AlertRule, AuditEvent, WebhookDelivery
from app.services import alerts as alerting
from app.services import webhooks
from app.services.webhooks import WebhookDispatcher, sign_body

ADMIN = {"X-TradingOS-Token": "test-local-admin-token"}
TARGET = "http://127.0.0.1:9/disabled-endpoint"  # never contacted: transport is stubbed


@pytest.fixture()
def client():
    # No lifespan: the background dispatcher must stay OFF so rows are only
    # moved by the explicit run_once() calls in each test.
    return TestClient(app)


@pytest.fixture(autouse=True)
def _webhook_world(monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", TARGET)
    monkeypatch.setattr(settings, "webhook_max_attempts", 3)
    monkeypatch.setattr(settings, "webhook_backoff_base_seconds", 2)
    monkeypatch.setattr(settings, "webhook_backoff_max_seconds", 300)
    monkeypatch.setattr(settings, "webhook_timeout_seconds", 5)
    monkeypatch.setattr(settings, "webhook_signing_secret", "test-signing-secret")
    webhooks._dispatcher = None  # tests drive run_once() synchronously
    yield
    with SessionLocal() as session:
        session.query(Alert).delete()
        session.query(WebhookDelivery).delete()
        session.query(AlertRule).delete()
        session.query(AuditEvent).filter(AuditEvent.event_type.in_(["ALERT_RAISED", "WEBHOOK_DELIVERED", "WEBHOOK_EXHAUSTED", "WEBHOOK_RETRY_QUEUED", "WEBHOOK_TEST_QUEUED"])).delete()
        session.commit()


def _as_aware(value):
    """SQLite drops tzinfo on readback; normalize before datetime math."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class _TransportSpy:
    """Mimics the ``_http_post`` signature and records every call."""

    def __init__(self, behavior=None):
        self.calls: list[dict] = []
        self.behavior = behavior or (lambda *args: 200)

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout_seconds: int) -> int:
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout_seconds})
        return self.behavior(url, body, headers, timeout_seconds)


def _raise_guard_alert() -> int:
    with SessionLocal() as session:
        alert = alerting.raise_alert(session, settings, code=alerting.GUARD_TRIPPED, severity="WARNING", message="System state is PAUSED; ACTIVE is required.", payload={"skipped": True})
        assert alert is not None
        return alert.id


def _only_delivery() -> WebhookDelivery:
    with SessionLocal() as session:
        row = session.scalar(select(WebhookDelivery).order_by(WebhookDelivery.id.desc()).limit(1))
        assert row is not None
        session.expunge(row)
        return row


def _force_due(delivery_id: int) -> None:
    with SessionLocal() as session:
        row = session.get(WebhookDelivery, delivery_id)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def test_alert_raise_enqueues_a_pending_delivery_atomically() -> None:
    alert_id = _raise_guard_alert()
    row = _only_delivery()
    assert row.status == "PENDING"
    assert row.attempts == 0
    assert row.alert_id == alert_id
    assert row.target_url == TARGET
    assert row.max_attempts == 3
    assert row.body["code"] == alerting.GUARD_TRIPPED
    assert row.body["severity"] == "WARNING"
    assert row.body["event"] == "alert"


def test_no_target_configured_means_no_delivery_rows(monkeypatch) -> None:
    monkeypatch.setattr(settings, "alert_webhook_url", None)
    _raise_guard_alert()
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(WebhookDelivery)) == 0


def test_successful_delivery_is_marked_delivered_signed_and_audited() -> None:
    spy = _TransportSpy()
    original = webhooks._http_post
    webhooks._http_post = spy
    try:
        _raise_guard_alert()
        attempted = WebhookDispatcher(SessionLocal, settings).run_once()
        assert attempted == 1
    finally:
        webhooks._http_post = original

    row = _only_delivery()
    assert row.status == "DELIVERED"
    assert row.attempts == 1
    assert row.delivered_at is not None
    assert row.last_http_status == 200
    assert row.next_attempt_at is None

    call = spy.calls[0]
    assert call["url"] == TARGET
    assert call["timeout"] == 5
    signature = call["headers"]["X-TradingOS-Signature"]
    expected = "sha256=" + hmac.new(b"test-signing-secret", call["body"], hashlib.sha256).hexdigest()
    assert signature == expected  # receiver can verify authenticity

    with SessionLocal() as session:
        audit = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "WEBHOOK_DELIVERED"))
        assert audit is not None
        assert audit.payload["delivery_id"] == row.id


def test_transport_failure_schedules_exponential_backoff() -> None:
    def always_fail(url, body, headers, timeout_seconds):
        raise urllib.error.URLError("connection refused")

    spy = _TransportSpy(always_fail)
    original = webhooks._http_post
    webhooks._http_post = spy
    try:
        _raise_guard_alert()
        dispatcher = WebhookDispatcher(SessionLocal, settings)
        dispatcher.run_once()
    finally:
        webhooks._http_post = original

    row = _only_delivery()
    assert row.status == "PENDING"
    assert row.attempts == 1
    assert row.last_error and "URLError" in row.last_error
    delay = (_as_aware(row.next_attempt_at) - datetime.now(UTC)).total_seconds()
    assert 0 < delay <= 2.5  # base * 2**0 = 2s (allow CI scheduling slack)

    # Not due yet: an immediate second pass must not re-attempt.
    original = webhooks._http_post
    webhooks._http_post = _TransportSpy(always_fail)
    try:
        assert WebhookDispatcher(SessionLocal, settings).run_once() == 0
    finally:
        webhooks._http_post = original

    _force_due(row.id)
    webhooks._http_post = _TransportSpy(always_fail)
    try:
        WebhookDispatcher(SessionLocal, settings).run_once()
    finally:
        webhooks._http_post = original
    row = _only_delivery()
    assert row.attempts == 2
    delay = (_as_aware(row.next_attempt_at) - datetime.now(UTC)).total_seconds()
    assert 0 < delay <= 4.5  # base * 2**1 = 4s


def test_retry_budget_exhaustion_marks_row_and_audits() -> None:
    def always_fail(url, body, headers, timeout_seconds):
        raise urllib.error.URLError("still down")

    original = webhooks._http_post
    try:
        _raise_guard_alert()
        dispatcher = WebhookDispatcher(SessionLocal, settings)
        for _ in range(3):  # max_attempts == 3
            webhooks._http_post = _TransportSpy(always_fail)
            _force_due(_only_delivery().id)
            dispatcher.run_once()
    finally:
        webhooks._http_post = original

    row = _only_delivery()
    assert row.status == "EXHAUSTED"
    assert row.attempts == 3
    assert row.next_attempt_at is None
    with SessionLocal() as session:
        audit = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "WEBHOOK_EXHAUSTED"))
        assert audit is not None
        assert audit.payload["attempts"] == 3

    # Exhausted rows are out of the queue: further passes do nothing.
    webhooks._http_post = _TransportSpy(always_fail)
    try:
        assert WebhookDispatcher(SessionLocal, settings).run_once() == 0
    finally:
        webhooks._http_post = original


def test_manual_retry_reopens_with_a_fresh_budget() -> None:
    failures = {"count": 0}

    def fail_three_times_then_recover(url, body, headers, timeout_seconds) -> int:
        failures["count"] += 1
        if failures["count"] <= 3:
            raise urllib.error.URLError("down")
        return 200

    original = webhooks._http_post
    try:
        _raise_guard_alert()
        delivery_id = _only_delivery().id
        for _ in range(3):
            webhooks._http_post = fail_three_times_then_recover
            _force_due(delivery_id)
            WebhookDispatcher(SessionLocal, settings).run_once()
        assert _only_delivery().status == "EXHAUSTED"

        with SessionLocal() as session:
            reopened = webhooks.retry_delivery(session, settings, delivery_id)
            assert reopened.status == "PENDING"
            assert reopened.attempts == 0  # fresh budget, documented semantics
        WebhookDispatcher(SessionLocal, settings).run_once()  # transport recovered
    finally:
        webhooks._http_post = original

    row = _only_delivery()
    assert row.status == "DELIVERED"
    with SessionLocal() as session:
        audit = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "WEBHOOK_RETRY_QUEUED"))
        assert audit is not None


def test_backoff_is_capped_at_the_configured_maximum(monkeypatch) -> None:
    monkeypatch.setattr(settings, "webhook_backoff_base_seconds", 2)
    monkeypatch.setattr(settings, "webhook_backoff_max_seconds", 3)
    assert webhooks.backoff_seconds(settings, 1) == 2
    assert webhooks.backoff_seconds(settings, 2) == 3  # raw 4, capped at 3
    assert webhooks.backoff_seconds(settings, 9) == 3


def test_test_webhook_endpoint_contract(client) -> None:
    denied = client.post("/api/v1/alerts/webhook/test")
    assert denied.status_code == 401

    settings.alert_webhook_url = None
    unconfigured = client.post("/api/v1/alerts/webhook/test", headers=ADMIN)
    assert unconfigured.status_code == 409
    settings.alert_webhook_url = TARGET

    ok = client.post("/api/v1/alerts/webhook/test", headers=ADMIN)
    assert ok.status_code == 200
    body = ok.json()
    assert body["target_url"] == TARGET
    row = _only_delivery()
    assert row.event == "test"
    assert row.code == "WEBHOOK_TEST"
    assert row.alert_id is None
    assert row.body["event"] == "test"
    with SessionLocal() as session:
        audit = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "WEBHOOK_TEST_QUEUED"))
        assert audit is not None


def test_delivery_listing_and_retry_endpoint(client) -> None:
    _raise_guard_alert()
    listing = client.get("/api/v1/alerts/deliveries")
    assert listing.status_code == 200
    rows = listing.json()["deliveries"]
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"

    denied = client.post(f"/api/v1/alerts/deliveries/{rows[0]['id']}/retry")
    assert denied.status_code == 401

    retried = client.post(f"/api/v1/alerts/deliveries/{rows[0]['id']}/retry", headers=ADMIN)
    assert retried.status_code == 200
    assert retried.json()["attempts"] == 0

    unknown = client.post("/api/v1/alerts/deliveries/999999/retry", headers=ADMIN)
    assert unknown.status_code == 404


def test_policy_endpoint_reflects_environment(client) -> None:
    response = client.get("/api/v1/alerts/webhook/policy")
    assert response.status_code == 200
    body = response.json()
    assert body["target_configured"] is True
    assert body["max_attempts"] == 3
    assert body["backoff_base_seconds"] == 2
    assert body["signing_enabled"] is True


def test_dispatcher_thread_starts_stops_and_drains() -> None:
    spy = _TransportSpy()
    original = webhooks._http_post
    webhooks._http_post = spy
    try:
        dispatcher = WebhookDispatcher(SessionLocal, settings, poll_interval_seconds=0.05)
        dispatcher.start()
        try:
            _raise_guard_alert()
            deadline = datetime.now(UTC) + timedelta(seconds=5)
            while datetime.now(UTC) < deadline:
                row = _only_delivery()
                if row.status == "DELIVERED":
                    break
                time.sleep(0.05)
            assert _only_delivery().status == "DELIVERED"
        finally:
            dispatcher.stop(timeout=5)
        assert dispatcher._thread is None
    finally:
        webhooks._http_post = original


def test_signature_helper_is_deterministic() -> None:
    body = b'{"event":"test"}'
    first = sign_body("secret-a", body)
    second = sign_body("secret-a", body)
    other = sign_body("secret-b", body)
    assert first == second
    assert first != other
    assert first.startswith("sha256=")
