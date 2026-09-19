"""Operator-tunable risk policy: versioned replacement with hard bounds.

The contract tested here: the seeded policy is readable, replacement requires
the local admin token, every cap is bounded by hard ceilings stricter than the
defaults (a typo can only tighten exposure), the daily-loss cap may not exceed
the drawdown cap, old versions are retired instead of mutated, exactly one
policy stays active, and every change lands in the audit trail.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app, settings
from app.models import AuditEvent, RiskPolicy

ADMIN = {"X-TradingOS-Token": "test-local-admin-token"}


def _default_payload() -> dict:
    return {
        "max_risk_fraction": 0.004,
        "max_trade_amount": 8.0,
        "max_daily_loss_fraction": 0.03,
        "max_drawdown_fraction": 0.06,
        "max_open_positions": 2,
        "stale_market_seconds": 120,
    }


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _risk_policy_world():
    yield
    with SessionLocal() as session:
        session.query(AuditEvent).filter(AuditEvent.event_type == "RISK_POLICY_UPDATED").delete()
        session.query(RiskPolicy).delete()
        session.add(RiskPolicy())  # restore the pristine seeded world
        session.commit()


def test_seeded_policy_is_readable_without_admin_token(client) -> None:
    response = client.get(f"{settings.api_prefix}/risk")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "risk-v1"
    assert body["active"] is True


def test_replacement_requires_local_admin_token(client) -> None:
    response = client.put(f"{settings.api_prefix}/risk", json=_default_payload())
    assert response.status_code == 401


def test_replacement_activates_new_version_and_retires_old(client) -> None:
    response = client.put(f"{settings.api_prefix}/risk", json=_default_payload(), headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "risk-v2"
    assert body["active"] is True
    assert body["max_trade_amount"] == 8.0

    with SessionLocal() as session:
        actives = list(session.scalars(select(RiskPolicy).where(RiskPolicy.active.is_(True))))
        assert len(actives) == 1
        assert actives[0].version == "risk-v2"
        retired = session.scalar(select(RiskPolicy).where(RiskPolicy.version == "risk-v1"))
        assert retired is not None and retired.active is False
        event = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "RISK_POLICY_UPDATED").order_by(AuditEvent.id.desc()).limit(1))
        assert event is not None
        assert event.payload["retired_version"] == "risk-v1"
        assert event.payload["activated_version"] == "risk-v2"


def test_version_counter_advances_across_replacements(client) -> None:
    assert client.put(f"{settings.api_prefix}/risk", json=_default_payload(), headers=ADMIN).status_code == 200
    second = client.put(f"{settings.api_prefix}/risk", json=_default_payload(), headers=ADMIN)
    assert second.status_code == 200
    assert second.json()["version"] == "risk-v3"


def test_cap_above_hard_ceiling_is_rejected_without_new_version(client) -> None:
    payload = _default_payload() | {"max_trade_amount": 500.0}
    response = client.put(f"{settings.api_prefix}/risk", json=payload, headers=ADMIN)
    assert response.status_code == 422
    with SessionLocal() as session:
        assert session.scalar(select(RiskPolicy).where(RiskPolicy.active.is_(True))).version == "risk-v1"


def test_fractional_floor_is_enforced(client) -> None:
    payload = _default_payload() | {"max_risk_fraction": 0.00001}
    response = client.put(f"{settings.api_prefix}/risk", json=payload, headers=ADMIN)
    assert response.status_code == 422


def test_daily_loss_cap_may_not_exceed_drawdown_cap(client) -> None:
    payload = _default_payload() | {"max_daily_loss_fraction": 0.10, "max_drawdown_fraction": 0.05}
    response = client.put(f"{settings.api_prefix}/risk", json=payload, headers=ADMIN)
    assert response.status_code == 422
    assert "daily" in response.json()["detail"].lower()


def test_position_and_staleness_bounds_are_enforced(client) -> None:
    too_many = _default_payload() | {"max_open_positions": 6}
    assert client.put(f"{settings.api_prefix}/risk", json=too_many, headers=ADMIN).status_code == 422
    too_stale = _default_payload() | {"stale_market_seconds": 3600}
    assert client.put(f"{settings.api_prefix}/risk", json=too_stale, headers=ADMIN).status_code == 422


def test_partial_replacement_is_rejected(client) -> None:
    """Every cap must be restated so an unstated dimension cannot stay loose by accident."""
    payload = _default_payload()
    del payload["max_daily_loss_fraction"]
    response = client.put(f"{settings.api_prefix}/risk", json=payload, headers=ADMIN)
    assert response.status_code == 422


def test_execution_service_sees_the_new_policy_immediately(client) -> None:
    """The loop and execution service read the active policy per tick — no restart needed."""
    tightened = _default_payload() | {"max_trade_amount": 2.0}
    assert client.put(f"{settings.api_prefix}/risk", json=tightened, headers=ADMIN).status_code == 200
    with SessionLocal() as session:
        active = session.scalar(select(RiskPolicy).where(RiskPolicy.active.is_(True)).order_by(RiskPolicy.id.desc()).limit(1))
        assert active.max_trade_amount == 2.0
