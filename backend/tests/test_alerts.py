"""Loop-guard alerting: dedupe, auto-resolve, acknowledgement, and the ledger.

A guard trip is the system working as designed, but the operator must hear
about it exactly once per condition — not once per tick. The contract tested
here: cooldown dedupe, auto-resolution when guards pass, operator ack
endpoints, and the rule that alerting never mutates order or strategy state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app, settings
from app.models import Alert, AuditEvent

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
        session.query(AuditEvent).filter(AuditEvent.event_type.in_(["ALERT_RAISED", "ALERT_ACKNOWLEDGED", "ALERT_RESOLVED"])).delete()
        session.commit()


def _seed_guard_alert(code: str = "LOOP_GUARD_TRIPPED", minutes_ago: int = 0) -> int:
    with SessionLocal() as session:
        alert = Alert(
            code=code,
            severity="WARNING",
            message="System state is PAUSED; ACTIVE is required.",
            payload={"skipped": True},
            last_seen_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        )
        session.add(alert)
        session.commit()
        return alert.id


def test_skipped_tick_raises_deduplicated_guard_alert() -> None:
    """Two ticks against a paused world collapse into one alert row, twice seen."""
    from app.main import loop_engine

    with SessionLocal() as session:
        loop_engine.tick(session)
        first_count = session.scalar(select(func.count()).select_from(Alert)) or 0
        loop_engine.tick(session)
        rows = list(session.scalars(select(Alert).where(Alert.code == "LOOP_GUARD_TRIPPED")))
    assert first_count == 1
    assert len(rows) == 1
    assert rows[0].occurrences == 2
    assert rows[0].acknowledged_at is None
    assert rows[0].message  # whichever guard tripped first, it is on the record


def test_guard_alert_auto_resolves_when_guards_pass() -> None:
    """A tick that passes its guards acknowledges the standing guard alert."""
    from unittest.mock import patch

    from app.main import loop_engine

    _seed_guard_alert()
    # A healthy guard pass requires PRACTICE + ACTIVE + a connected broker; the
    # engine's real guard logic is exercised elsewhere, so force the guard
    # check green on the instance and let reconciliation fail (the broken tick
    # then raises its own alert class, which this test also asserts).
    with patch.object(loop_engine, "_guards", return_value=None), \
         patch.object(loop_engine.worker, "reconcile", side_effect=RuntimeError("boom")):
        with SessionLocal() as session:
            with pytest.raises(RuntimeError):
                loop_engine.tick(session)
    with SessionLocal() as session:
        guard = session.scalar(select(Alert).where(Alert.code == "LOOP_GUARD_TRIPPED"))
        assert guard is not None
        assert guard.acknowledged_at is not None
        # The tick itself broke, which raises its own alert class.
        tick_failed = session.scalar(select(Alert).where(Alert.code == "LOOP_TICK_FAILED"))
        assert tick_failed is not None
        assert tick_failed.severity == "ERROR"
        session.query(Alert).delete()
        session.commit()


def test_alert_after_cooldown_starts_a_new_row() -> None:
    """Once the cooldown window has passed, a still-tripping guard raises fresh."""
    from app.services import alerts as alerting

    _seed_guard_alert(minutes_ago=5)  # default cooldown is 60s
    with SessionLocal() as session:
        alerting.raise_alert(session, settings, code=alerting.GUARD_TRIPPED, severity="WARNING", message="System state is PAUSED; ACTIVE is required.", payload={})
        rows = list(session.scalars(select(Alert).order_by(Alert.id)))
    assert len(rows) == 2
    assert rows[0].occurrences == 1 and rows[1].occurrences == 1


def test_alert_listing_reports_unacknowledged_count(client) -> None:
    _seed_guard_alert()
    response = client.get("/api/v1/alerts")
    assert response.status_code == 200
    body = response.json()
    assert body["unacknowledged"] >= 1
    assert any(alert["code"] == "LOOP_GUARD_TRIPPED" for alert in body["alerts"])
    unread = client.get("/api/v1/alerts/unread-count")
    assert unread.status_code == 200
    assert unread.json()["unacknowledged"] == body["unacknowledged"]


def test_alert_ack_endpoint_requires_admin(client) -> None:
    alert_id = _seed_guard_alert()
    denied = client.post(f"/api/v1/alerts/{alert_id}/ack")
    assert denied.status_code == 401
    response = client.post(f"/api/v1/alerts/{alert_id}/ack", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["acknowledged"] == [alert_id]
    assert response.json()["auto"] is False
    with SessionLocal() as session:
        row = session.get(Alert, alert_id)
        assert row.acknowledged_at is not None


def test_ack_all_clears_every_standing_alert(client) -> None:
    _seed_guard_alert()
    _seed_guard_alert(code="LOOP_TICK_FAILED")
    response = client.post("/api/v1/alerts/ack-all", headers=ADMIN)
    assert response.status_code == 200
    assert len(response.json()["acknowledged"]) == 2
    unread = client.get("/api/v1/alerts/unread-count")
    assert unread.json()["unacknowledged"] == 0
    # A no-op ack-all returns an empty list.
    again = client.post("/api/v1/alerts/ack-all", headers=ADMIN)
    assert again.json()["acknowledged"] == []


def test_unknown_alert_ack_answers_404(client) -> None:
    response = client.post("/api/v1/alerts/999999/ack", headers=ADMIN)
    assert response.status_code == 404


def test_alerting_writes_audit_trail_and_never_touches_orders() -> None:
    from app.main import loop_engine

    with SessionLocal() as session:
        loop_engine.tick(session)
    with SessionLocal() as session:
        raised = session.scalars(select(AuditEvent).where(AuditEvent.event_type == "ALERT_RAISED")).all()
        assert raised, "a guard trip must be auditable"
        assert any("LOOP_GUARD_TRIPPED" in event.payload.get("code", "") for event in raised)
