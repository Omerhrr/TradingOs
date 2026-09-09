"""Alert-rule configuration: seeding, silencing, severity/cooldown overrides.

Rules make alert behavior an operator decision instead of a code constant.
The contract tested here: the manifest seeds idempotently, a disabled rule
silences a code completely (no row, no audit, no webhook), severity and
cooldown overrides are honored, missing rules fall back to the built-in
defaults, and the rule endpoints are admin-gated with validation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app, settings
from app.models import Alert, AlertRule, AuditEvent, WebhookDelivery
from app.services import alerts as alerting

ADMIN = {"X-TradingOS-Token": "test-local-admin-token"}


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _alert_world():
    yield
    with SessionLocal() as session:
        session.query(Alert).delete()
        session.query(WebhookDelivery).delete()
        session.query(AlertRule).delete()
        session.query(AuditEvent).filter(AuditEvent.event_type.in_(["ALERT_RAISED", "ALERT_RULE_UPDATED"])).delete()
        session.commit()


def _seed_rules() -> None:
    with SessionLocal() as session:
        alerting.ensure_alert_rules(session)


def test_rules_seed_from_manifest_with_natural_severities() -> None:
    _seed_rules()
    with SessionLocal() as session:
        rules = {rule.code: rule for rule in session.scalars(select(AlertRule))}
    assert set(rules) == {alerting.GUARD_TRIPPED, alerting.TICK_FAILED, alerting.SUBMIT_ERROR}
    assert rules[alerting.GUARD_TRIPPED].severity == "WARNING"
    assert rules[alerting.TICK_FAILED].severity == "ERROR"
    assert rules[alerting.SUBMIT_ERROR].severity == "ERROR"
    for rule in rules.values():
        assert rule.enabled is True
        assert rule.notify_webhook is True
        assert rule.cooldown_seconds is None  # falls back to the settings default
        assert rule.description


def test_rule_seeding_is_idempotent_and_never_rewrites() -> None:
    _seed_rules()
    with SessionLocal() as session:
        rule = session.scalar(select(AlertRule).where(AlertRule.code == alerting.GUARD_TRIPPED))
        rule.severity = "ERROR"
        session.commit()
    _seed_rules()
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AlertRule)) == 3
        # Reseeding must not clobber operator edits.
        assert session.scalar(select(AlertRule).where(AlertRule.code == alerting.GUARD_TRIPPED)).severity == "ERROR"


def test_disabled_rule_silences_the_code_completely() -> None:
    _seed_rules()
    with SessionLocal() as session:
        alerting.update_alert_rule(session, settings, alerting.GUARD_TRIPPED, enabled=False)
        result = alerting.raise_alert(session, settings, code=alerting.GUARD_TRIPPED, severity="WARNING", message="paused")
        assert result is None
        assert session.scalar(select(func.count()).select_from(Alert)) == 0
        assert session.scalar(select(func.count()).select_from(WebhookDelivery)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "ALERT_RAISED")) == 0


def test_rule_severity_override_wins_over_caller() -> None:
    _seed_rules()
    with SessionLocal() as session:
        alerting.update_alert_rule(session, settings, alerting.GUARD_TRIPPED, severity="ERROR")
        alert = alerting.raise_alert(session, settings, code=alerting.GUARD_TRIPPED, severity="WARNING", message="paused")
        assert alert is not None
        assert alert.severity == "ERROR"
        session.refresh(alert)
        assert alert.severity == "ERROR"


def test_rule_cooldown_override_extends_dedupe_window() -> None:
    _seed_rules()
    with SessionLocal() as session:
        alerting.update_alert_rule(session, settings, alerting.GUARD_TRIPPED, cooldown_seconds=86_400)
        session.add(Alert(code=alerting.GUARD_TRIPPED, severity="WARNING", message="paused", last_seen_at=datetime.now(UTC) - timedelta(minutes=10)))
        session.commit()
        # 10 minutes is far outside the default 60s window but inside the rule's.
        alert = alerting.raise_alert(session, settings, code=alerting.GUARD_TRIPPED, severity="WARNING", message="paused")
        assert alert is not None
        assert alert.occurrences == 2


def test_missing_rules_fall_back_to_builtin_defaults() -> None:
    # Deliberately NO ensure_alert_rules(): raise_alert must keep working.
    with SessionLocal() as session:
        alert = alerting.raise_alert(session, settings, code=alerting.GUARD_TRIPPED, severity="WARNING", message="paused")
        assert alert is not None
        assert alert.severity == "WARNING"
        assert alert.occurrences == 1


def test_rule_update_endpoint_auth_validation_and_audit(client) -> None:
    _seed_rules()
    denied = client.put(f"/api/v1/alerts/rules/{alerting.GUARD_TRIPPED}", json={"enabled": False})
    assert denied.status_code == 401

    invalid = client.put(f"/api/v1/alerts/rules/{alerting.GUARD_TRIPPED}", headers=ADMIN, json={"severity": "CRITICAL"})
    assert invalid.status_code == 422

    unknown = client.put("/api/v1/alerts/rules/NOT_A_CODE", headers=ADMIN, json={"enabled": False})
    assert unknown.status_code == 404

    ok = client.put(f"/api/v1/alerts/rules/{alerting.GUARD_TRIPPED}", headers=ADMIN, json={"severity": "ERROR", "cooldown_seconds": 300})
    assert ok.status_code == 200
    body = ok.json()
    assert body["severity"] == "ERROR"
    assert body["cooldown_seconds"] == 300
    assert body["enabled"] is True  # untouched fields keep their value

    listing = client.get("/api/v1/alerts/rules")
    assert listing.status_code == 200
    codes = {rule["code"] for rule in listing.json()["rules"]}
    assert codes == {alerting.GUARD_TRIPPED, alerting.TICK_FAILED, alerting.SUBMIT_ERROR}

    with SessionLocal() as session:
        audit = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "ALERT_RULE_UPDATED"))
        assert audit is not None
        assert audit.payload["code"] == alerting.GUARD_TRIPPED
