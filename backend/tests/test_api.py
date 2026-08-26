"""Regression checks for TradingOS's practice-first backend boundary."""

from fastapi.testclient import TestClient

from app.main import app


def test_health_is_operational_and_live_execution_is_off() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["live_execution"] is False
    assert response.json()["broker_connection"] == "DISCONNECTED"


def test_system_starts_practice_first_and_paused() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/state")
    assert response.status_code == 200
    assert response.json()["account_mode"] == "PRACTICE"
    assert response.json()["system_state"] == "PAUSED"


def test_real_mode_is_hard_disabled() -> None:
    with TestClient(app) as client:
        response = client.post("/api/v1/mode/enable-real")
    assert response.status_code == 403
    assert "hard-disabled" in response.json()["detail"]


def test_local_broker_controls_require_a_configured_admin_token() -> None:
    with TestClient(app) as client:
        response = client.post("/api/v1/broker/connect")
    assert response.status_code == 503


def test_watchlist_replacement_preserves_auditability() -> None:
    with TestClient(app) as client:
        response = client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        events = client.get("/api/v1/events")
    assert response.status_code == 200
    assert response.json()[0]["symbol"] == "EURUSD"
    assert any(event["event_type"] == "WATCHLIST_REPLACED" for event in events.json())
