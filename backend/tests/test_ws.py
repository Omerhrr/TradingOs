"""Live loop-channel tests: the /ws/loop WebSocket endpoint.

Covers the hello snapshot, fan-out to multiple clients, ping/pong keepalive,
the loop engine's tick events arriving on the socket, execution submit events,
and clean subscriber cleanup on disconnect.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app, loop_engine
from app.services.events import event_bus
from app.services.execution import ExecutionService

from tests.fake_broker import FakeBrokerAdapter

AUTH = {"X-TradingOS-Token": "test-local-admin-token"}


def test_hello_snapshot_arrives_on_connect() -> None:
    with TestClient(app) as client, client.websocket_connect("/api/v1/ws/loop") as ws:
        message = ws.receive_json()
    assert message["type"] == "hello"
    payload = message["payload"]
    assert set(payload) >= {"loop_enabled", "practice_execution_enabled", "broker_connection", "system_state", "last_run"}


def test_events_fan_out_to_every_connected_client() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws/loop") as first, client.websocket_connect("/api/v1/ws/loop") as second:
            assert first.receive_json()["type"] == "hello"
            assert second.receive_json()["type"] == "hello"
            event_bus.publish("loop.tick.completed", {"run_id": 1, "state": "SUCCEEDED", "summary": {"skipped": True, "reason": "unit test"}})
            assert first.receive_json()["type"] == "loop.tick.completed"
            assert second.receive_json()["type"] == "loop.tick.completed"


def test_ping_answers_with_pong() -> None:
    with TestClient(app) as client, client.websocket_connect("/api/v1/ws/loop") as ws:
        ws.receive_json()  # hello
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"


def test_loop_engine_tick_publishes_started_and_completed() -> None:
    with TestClient(app) as client, client.websocket_connect("/api/v1/ws/loop") as ws:
        assert ws.receive_json()["type"] == "hello"
        with SessionLocal() as session:
            loop_engine.tick(session)  # guards make this a skipped tick in a paused world
        started = ws.receive_json()
        completed = ws.receive_json()
    assert started["type"] == "loop.tick.started"
    assert completed["type"] == "loop.tick.completed"
    assert completed["payload"]["summary"]["skipped"] is True


def test_submission_publishes_execution_event() -> None:
    from app.config import get_settings
    from app.database import engine
    from app.models import Base, StrategyVersion

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        strategy = StrategyVersion(strategy_key="ws-submit-strat", version="v1", status="VALIDATED", definition={"kind": "ema_cross", "fast_window": 5, "slow_window": 10}, validation_summary={})
        session.add(strategy)
        session.commit()
        session.refresh(strategy)
        strategy_id = strategy.id

    fake_broker = FakeBrokerAdapter()
    from app.services.credentials import BrokerCredentials

    fake_broker.connect_practice(BrokerCredentials(email="ws@test.local", password="password123"))

    with TestClient(app) as client, client.websocket_connect("/api/v1/ws/loop") as ws:
        assert ws.receive_json()["type"] == "hello"
        settings = get_settings()
        service = ExecutionService(fake_broker, settings)
        with SessionLocal() as session:
            # A connected broker, ACTIVE system, and fresh candles let the gate approve.
            from datetime import UTC, datetime, timedelta

            from app.models import AccountConfig, AccountSnapshot, Candle, SystemState

            account = session.query(AccountConfig).first()
            account.system_state = SystemState.ACTIVE.value
            session.add(AccountSnapshot(account_config_id=account.id, account_mode="PRACTICE", balance=1_000.0, currency="USD", broker_timestamp=datetime.now(UTC), raw_payload={}))
            session.add(Candle(symbol="EURUSD", timeframe_seconds=60, open_time=datetime.now(UTC) - timedelta(seconds=10), close_time=datetime.now(UTC), open_price=1.1, high_price=1.1, low_price=1.1, close_price=1.1, volume=None, source="test"))
            session.commit()
            settings.practice_execution_enabled = True
            try:
                intent, decision = service.create_intent(session, idempotency_key="ws-submit-key-1", strategy_id=strategy_id, symbol="EURUSD", side="CALL", amount=5.0, timeframe_seconds=60, duration_minutes=1)
                assert decision.accepted, decision.reason
                service.submit_approved_intent(session, intent.id)
            finally:
                settings.practice_execution_enabled = False

        # Consume events until the submission arrives (skips unrelated ones).
        found = None
        for _ in range(5):
            event = ws.receive_json()
            if event["type"] == "execution.order.submitted":
                found = event
                break
    assert found is not None
    assert json.dumps(found["payload"]) is not None
    assert found["payload"]["symbol"] == "EURUSD"
    assert found["payload"]["intent_id"] == intent.id


def test_disconnect_releases_the_subscriber_queue() -> None:
    with TestClient(app) as client:
        baseline = event_bus.subscriber_count
        with client.websocket_connect("/api/v1/ws/loop"):
            during = event_bus.subscriber_count
    assert during == baseline + 1
    assert event_bus.subscriber_count == baseline
