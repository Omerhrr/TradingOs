"""Tests for the wired strategy -> intent -> practice-execution loop.

Covers the manual /loop/run API, guard behaviour (fail-closed skips),
per-candle intent idempotency, manual-intent isolation, failure paths,
and the background runtime driving the loop when enabled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    AccountConfig,
    AuditEvent,
    LoopRun,
    OrderIntent,
    OrderRecord,
    ReconciliationRun,
    SystemState,
)
from app.services.runtime import LocalRuntime
from app.services.worker import BrokerWorker

from tests.fake_broker import FakeBrokerAdapter, sine_candles, trend_tail_candles

AUTH = {"X-TradingOS-Token": "test-local-admin-token"}


@pytest.fixture()
def fake_broker() -> FakeBrokerAdapter:
    return FakeBrokerAdapter(candles_by_symbol={"EURUSD": trend_tail_candles(200)})


@pytest.fixture()
def install_broker(fake_broker: FakeBrokerAdapter, monkeypatch) -> FakeBrokerAdapter:
    from app.main import worker as app_worker

    monkeypatch.setattr(app_worker, "adapter", fake_broker)
    monkeypatch.setattr(app_worker.reconciler, "broker", fake_broker)
    return fake_broker


def _connect(fake_broker: FakeBrokerAdapter) -> None:
    from app.services.credentials import BrokerCredentials

    fake_broker.connect_practice(BrokerCredentials(email="e@example.com", password="practice-pass-123"))


def _validated_strategy(client, monkeypatch) -> int:
    created = client.post("/api/v1/strategies", headers=AUTH, json={"strategy_key": "loop-ema", "version": "v1", "definition": {}}).json()
    evaluation = client.post(f"/api/v1/strategies/{created['id']}/evaluate", headers=AUTH,
                             json={"symbol": "EURUSD", "timeframe_seconds": 60, "censor_gap_seconds": 60}).json()
    assert evaluation["accepted"] is True, evaluation
    return created["id"]


# ------------------------------------------------------------------- happy path


def test_manual_loop_run_creates_and_submits_intent(install_broker, monkeypatch) -> None:
    """Full autonomous pass: signal -> risk-gated intent -> practice submission."""
    from app.main import settings as app_settings

    monkeypatch.setattr(app_settings, "practice_execution_enabled", True)
    broker = install_broker
    _connect(broker)

    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        client.post("/api/v1/reconciliation/run", headers=AUTH)  # ingest candles
        _validated_strategy(client, monkeypatch)
        client.post("/api/v1/system/resume", headers=AUTH)

        response = client.post("/api/v1/loop/run", headers=AUTH)
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["state"] == "SUCCEEDED"
        assert run["summary"]["skipped"] is False
        assert len(run["summary"]["signals"]) == 1
        assert run["summary"]["signals"][0]["signal"] == "CALL"  # trend tail forces a decisive uptrend
        assert len(run["summary"]["intents_created"]) == 1

        intent = run["summary"]["intents_created"][0]
        assert intent["status"] == "APPROVED"
        assert run["summary"]["intents_submitted"] >= 1
        assert len(broker.submitted) == 1

        # The intent carries the deterministic loop idempotency key.
        with SessionLocal() as session:
            row = session.get(OrderIntent, intent["intent_id"])
            assert row.idempotency_key.startswith("loop:")
            assert row.idempotency_key.split(":")[1] == str(row.strategy_version_id)
            assert session.scalar(select(OrderRecord).where(OrderRecord.order_intent_id == row.id)) is not None

        # Second tick within the same candle: one signal, zero new intents.
        second = client.post("/api/v1/loop/run", headers=AUTH).json()
        assert second["state"] == "SUCCEEDED"
        assert second["summary"]["intents_created"] == []
        assert len(broker.submitted) == 1

        # Audit trail contains the tick events.
        with SessionLocal() as session:
            ticks = list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == "LOOP_TICK")))
            assert len(ticks) >= 2


def test_loop_uses_risk_cap_for_signal_amount(install_broker, monkeypatch) -> None:
    """Signal intents request the policy cap and are capped by the gate anyway."""
    from app.main import settings as app_settings

    monkeypatch.setattr(app_settings, "practice_execution_enabled", True)
    broker = install_broker
    _connect(broker)
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        client.post("/api/v1/reconciliation/run", headers=AUTH)
        _validated_strategy(client, monkeypatch)
        client.post("/api/v1/system/resume", headers=AUTH)
        run = client.post("/api/v1/loop/run", headers=AUTH).json()
    with SessionLocal() as session:
        intent = session.get(OrderIntent, run["summary"]["intents_created"][0]["intent_id"])
        assert intent.requested_amount <= 5.0  # default policy cap
        assert intent.rationale["original_requested_amount"] <= 5.0


# ------------------------------------------------------------------- guards


def test_loop_skips_when_system_paused(install_broker) -> None:
    """PAUSED system must produce a clean skipped tick, not an error."""
    broker = install_broker
    _connect(broker)
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        response = client.post("/api/v1/loop/run", headers=AUTH)
    assert response.status_code == 200
    run = response.json()
    assert run["state"] == "SUCCEEDED"
    assert run["summary"]["skipped"] is True
    assert "PAUSED" in run["summary"]["reason"]
    with SessionLocal() as session:
        assert session.scalar(select(OrderIntent).limit(1)) is None


def test_loop_skips_when_broker_disconnected() -> None:
    """ACTIVE system with a disconnected broker must skip cleanly."""
    with SessionLocal() as session:
        account = session.query(AccountConfig).first()
        account.system_state = SystemState.ACTIVE.value
        session.commit()
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        response = client.post("/api/v1/loop/run", headers=AUTH)
    run = response.json()
    assert run["state"] == "SUCCEEDED"
    assert run["summary"]["skipped"] is True
    assert "broker" in run["summary"]["reason"].lower()


def test_loop_requires_validated_strategy(install_broker, monkeypatch) -> None:
    """A DRAFT strategy must never produce loop signals."""
    broker = install_broker
    _connect(broker)
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        client.post("/api/v1/reconciliation/run", headers=AUTH)
        client.post("/api/v1/strategies", headers=AUTH, json={"strategy_key": "draft-ema", "version": "v1", "definition": {}})
        client.post("/api/v1/system/resume", headers=AUTH)
        run = client.post("/api/v1/loop/run", headers=AUTH).json()
    assert run["summary"]["signals"] == []
    assert run["summary"]["intents_created"] == []


# ------------------------------------------------------------------- failure paths


def test_loop_failure_marks_run_failed_and_returns_502(install_broker) -> None:
    """A broker error mid-tick (reconcile phase) fails the run observably."""
    broker = install_broker
    _connect(broker)
    broker.fail_account = True
    with SessionLocal() as session:
        account = session.query(AccountConfig).first()
        account.system_state = SystemState.ACTIVE.value  # pass the guards first
        session.commit()
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        response = client.post("/api/v1/loop/run", headers=AUTH)
        assert response.status_code == 502
        runs = client.get("/api/v1/loop/runs", headers=AUTH).json()
    assert runs[0]["state"] == "FAILED"
    assert "timed out" in runs[0]["error_message"]


def test_loop_does_not_submit_manual_intents(install_broker, monkeypatch) -> None:
    """Only loop-originated intents are auto-submitted; manual ones stay queued."""
    from app.main import settings as app_settings

    monkeypatch.setattr(app_settings, "practice_execution_enabled", True)
    broker = install_broker
    _connect(broker)
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        client.post("/api/v1/reconciliation/run", headers=AUTH)
        strategy_id = _validated_strategy(client, monkeypatch)
        client.post("/api/v1/system/resume", headers=AUTH)
        # A manual APPROVED intent (submitted path is a separate user action).
        manual = client.post("/api/v1/order-intents", headers=AUTH, json={
            "strategy_version_id": strategy_id, "symbol": "EURUSD", "side": "PUT", "amount": 5.0,
            "timeframe_seconds": 60, "duration_minutes": 1, "idempotency_key": "manual-1"}).json()
        assert manual["status"] == "APPROVED"
        run = client.post("/api/v1/loop/run", headers=AUTH).json()
        with SessionLocal() as session:
            manual_row = session.get(OrderIntent, manual["id"])
            assert manual_row.status == "APPROVED", "loop must not submit manual intents"


def test_loop_status_endpoint_reflects_configuration(install_broker) -> None:
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.post("/api/v1/loop/run", headers=AUTH)
        status = client.get("/api/v1/loop/status").json()
    assert status["loop_enabled"] is False  # conftest disables background loop
    assert status["practice_execution_enabled"] is False
    assert status["system_state"] in {"PAUSED", "ACTIVE"}
    assert status["last_run"]["state"] in {"SUCCEEDED", "FAILED"}


# ------------------------------------------------------------------- runtime wiring


def test_runtime_drives_loop_when_enabled() -> None:
    """With auto_reconcile + loop enabled, the background thread ticks the loop."""
    broker = FakeBrokerAdapter(candles_by_symbol={"EURUSD": trend_tail_candles(200)})
    settings = Settings(_env_file=None, auto_reconcile_enabled=True, loop_enabled=True, broker_sync_interval_seconds=10)
    settings.broker_sync_interval_seconds = 0
    worker = BrokerWorker(broker, 200)
    from app.services.loop import LoopEngine

    loop = LoopEngine(worker, settings)
    runtime = LocalRuntime(settings, SessionLocal, worker, loop)
    _connect(broker)
    runtime.start()
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        last_run_state = None
        while datetime.now(UTC) < deadline:
            with SessionLocal() as session:
                run = session.scalar(select(LoopRun).order_by(LoopRun.id.desc()).limit(1))
                last_run_state = run.state if run else None
            if last_run_state in {"SUCCEEDED", "FAILED"}:
                break
            runtime._stop.wait(0.05)
        assert last_run_state == "SUCCEEDED", f"loop never ticked successfully: {last_run_state}"
    finally:
        runtime.stop()


def test_runtime_without_loop_still_reconciles() -> None:
    """Backward compatibility: runtime with no loop engine keeps reconciling."""
    broker = FakeBrokerAdapter(candles_by_symbol={"EURUSD": sine_candles(30)})
    settings = Settings(_env_file=None, auto_reconcile_enabled=True, broker_sync_interval_seconds=10)
    settings.broker_sync_interval_seconds = 0
    worker = BrokerWorker(broker, 200)
    runtime = LocalRuntime(settings, SessionLocal, worker)
    _connect(broker)
    runtime.start()
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        while datetime.now(UTC) < deadline:
            with SessionLocal() as session:
                rec = session.scalar(select(ReconciliationRun).where(ReconciliationRun.state == "SUCCEEDED").limit(1))
            if rec is not None:
                break
            runtime._stop.wait(0.05)
        assert rec is not None, "runtime never reconciled"
        runtime._stop.wait(0.5)
        with SessionLocal() as session:
            assert session.scalar(select(LoopRun)) is None, "loop must not tick without an engine"
    finally:
        runtime.stop()
