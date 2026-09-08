"""Full-loop integration and failure-injection tests for TradingOS.

Layer 1 - integration: watchlist -> reconcile (candles) -> strategy -> evaluate
          -> resume -> intent -> practice submit -> settlement -> trade outcome.
Layer 2 - failure injection: a scripted broker that fails at every boundary
          (auth, mode mismatch, mode drift, candle stream, account read,
          order submit, mid-run crashes) proving the system stays fail-closed
          with persisted, auditable state.

The suite never touches the network: every test installs FakeBrokerAdapter
into the app worker, mirroring the exact IQAirBrokerAdapter contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    AccountConfig,
    AccountSnapshot,
    AuditEvent,
    Candle,
    EncryptedBrokerCredential,
    LearningEpisode,
    OrderIntent,
    OrderRecord,
    PositionSnapshot,
    ReconciliationRun,
    RiskPolicy,
    StrategyStatus,
    StrategyVersion,
    SystemState,
    TradeOutcome,
)
from app.services.broker import BrokerError
from app.services.credentials import CredentialVault
from app.services.execution import ExecutionService
from app.services.runtime import LocalRuntime
from app.services.worker import BrokerWorker

from tests.fake_broker import FakeBrokerAdapter, iqair_position, sine_candles, trend_tail_candles

AUTH = {"X-TradingOS-Token": "test-local-admin-token"}


# The shared pristine-world fixture lives in conftest.py (autouse).


@pytest.fixture()
def fake_broker() -> FakeBrokerAdapter:
    return FakeBrokerAdapter(candles_by_symbol={"EURUSD": sine_candles(200)})


@pytest.fixture()
def install_broker(fake_broker: FakeBrokerAdapter, monkeypatch) -> FakeBrokerAdapter:
    """Wire the fake into every component that holds an adapter reference."""
    from app.main import worker as app_worker

    monkeypatch.setattr(app_worker, "adapter", fake_broker)
    monkeypatch.setattr(app_worker.reconciler, "broker", fake_broker)
    return fake_broker


def _enable_practice_execution(monkeypatch) -> None:
    from app.main import settings as app_settings

    monkeypatch.setattr(app_settings, "practice_execution_enabled", True)


def _connect(fake_broker: FakeBrokerAdapter) -> None:
    from app.services.credentials import BrokerCredentials

    fake_broker.connect_practice(BrokerCredentials(email="e@example.com", password="p"))


def _latest_event_type(event_types: list[str]) -> str | None:
    with SessionLocal() as session:
        row = session.scalar(select(AuditEvent).where(AuditEvent.event_type.in_(event_types)).order_by(AuditEvent.id.desc()).limit(1))
        return row.event_type if row else None


# =============================================================== Layer 1: happy path


def test_full_cycle_strategy_to_settled_practice_trade(install_broker, monkeypatch) -> None:
    """The whole documented loop, end to end, through the real HTTP API.

    watchlist -> reconcile ingests candles -> strategy created -> evaluation
    accepted -> resume -> intent (risk-capped) -> practice submit -> broker
    position settles -> reconciler records SETTLED order, TradeOutcome and a
    LearningEpisode.
    """
    _enable_practice_execution(monkeypatch)
    broker = install_broker
    _connect(broker)

    with TestClient(app_context_client := __import__("app.main", fromlist=["app"]).app) as client:
        # 1. Watchlist drives candle ingestion.
        assert client.put("/api/v1/watchlist", json={"items": [
            {"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]}).status_code == 200

        # 2. Reconciliation ingests the 200-candle series.
        response = client.post("/api/v1/reconciliation/run", headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "SUCCEEDED"
        assert response.json()["summary"]["candles_ingested"] == 200
        with SessionLocal() as session:
            assert session.scalar(select(Candle).where(Candle.symbol == "EURUSD").limit(1)) is not None

        # 3. Strategy created then accepted by deterministic evaluation.
        created = client.post("/api/v1/strategies", headers=AUTH, json={"strategy_key": "ema", "version": "v1", "definition": {}}).json()
        evaluation = client.post(f"/api/v1/strategies/{created['id']}/evaluate", headers=AUTH,
                                 json={"symbol": "EURUSD", "timeframe_seconds": 60, "censor_gap_seconds": 60}).json()
        assert evaluation["accepted"] is True, evaluation
        with SessionLocal() as session:
            strategy = session.get(StrategyVersion, created["id"])
            assert strategy.status == StrategyStatus.VALIDATED.value

        # 4. System resumes only because the fake broker is CONNECTED.
        state = client.post("/api/v1/system/resume", headers=AUTH).json()
        assert state["system_state"] == "ACTIVE"

        # 5. Intent is risk-capped end to end: 1_000 requested, 5.0 approved.
        intent_response = client.post("/api/v1/order-intents", headers=AUTH, json={
            "strategy_version_id": created["id"], "symbol": "EURUSD", "side": "CALL",
            "amount": 1_000.0, "timeframe_seconds": 60, "duration_minutes": 1,
            "idempotency_key": "cycle-intent-1"})
        assert intent_response.status_code == 200, intent_response.text
        intent = intent_response.json()
        assert intent["status"] == "APPROVED"
        assert intent["requested_amount"] == 5.0
        assert intent["rationale"]["original_requested_amount"] == 1_000.0

        # 6. Approved intent is submitted to the practice broker.
        submitted = client.post(f"/api/v1/order-intents/{intent['id']}/submit", headers=AUTH).json()
        assert submitted["status"] == "SUBMITTED"
        broker_order_id = submitted["broker_order_id"]
        assert broker_order_id in {order["broker_order_id"] for order in broker.submitted}

        # 7. Broker position settles as a win.
        broker.positions_payload = [iqair_position(position_id="pos-1", order_id=broker_order_id, status="won", pnl=4.1)]

        # 8. Reconciliation turns the observation into persisted outcomes.
        response = client.post("/api/v1/reconciliation/run", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["summary"]["positions"] == 1

    with SessionLocal() as session:
        record = session.scalar(select(OrderRecord).where(OrderRecord.broker_order_id == broker_order_id))
        assert record is not None and record.status == "SETTLED"
        assert record.broker_position_id == "pos-1"
        outcome = session.scalar(select(TradeOutcome).where(TradeOutcome.order_record_id == record.id))
        assert outcome is not None
        assert outcome.outcome == "WIN" and outcome.realized_pnl == 4.1
        episode = session.scalar(select(LearningEpisode).where(LearningEpisode.order_record_id == record.id))
        assert episode is not None and episode.episode_type == "PRACTICE_TRADE_OUTCOME"

    # 9. The complete audit trail exists, in append-only order.
    with SessionLocal() as session:
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))
        event_types = [event.event_type for event in events]
        for expected in ("RECONCILIATION_SUCCEEDED", "STRATEGY_EVALUATED", "SYSTEM_RESUMED",
                         "ORDER_INTENT_AUTHORIZED", "PRACTICE_ORDER_SUBMITTED"):
            assert expected in event_types, f"missing {expected} in {event_types}"


def test_idempotent_intent_replay_returns_original_intent(install_broker, monkeypatch) -> None:
    """Replaying an idempotency key returns the first intent, not a duplicate."""
    _enable_practice_execution(monkeypatch)
    _connect(install_broker)
    payload = {"strategy_version_id": None, "symbol": "EURUSD", "side": "CALL", "amount": 5.0,
               "timeframe_seconds": 60, "duration_minutes": 1, "idempotency_key": "replay-1"}
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        client.post("/api/v1/reconciliation/run", headers=AUTH)
        # Strategy must be VALIDATED for acceptance; build one directly.
        with SessionLocal() as session:
            strategy = StrategyVersion(strategy_key="replay", version="v1", status=StrategyStatus.VALIDATED.value, definition={})
            session.add(strategy)
            session.commit()
            payload["strategy_version_id"] = strategy.id
        first = client.post("/api/v1/order-intents", headers=AUTH, json=payload).json()
        client.post("/api/v1/system/resume", headers=AUTH)
        second = client.post("/api/v1/order-intents", headers=AUTH, json=payload).json()
    assert first["id"] == second["id"]
    with SessionLocal() as session:
        rows = list(session.scalars(select(OrderIntent).where(OrderIntent.idempotency_key == "replay-1")))
    assert len(rows) == 1, "replay must not create a second intent row"


def test_double_submit_reuses_record_without_second_broker_call(install_broker, monkeypatch) -> None:
    """Submitting an already-submitted intent is a no-op, not a duplicate order."""
    _enable_practice_execution(monkeypatch)
    broker = install_broker
    _connect(broker)
    from app.main import settings as app_settings

    service = ExecutionService(broker, app_settings)
    with SessionLocal() as session:
        _seed_active_world(session)
        intent, decision = service.create_intent(session, idempotency_key="dupe-1", strategy_id=session.scalar(select(StrategyVersion.id).limit(1)),
                                                  symbol="EURUSD", side="CALL", amount=5.0, timeframe_seconds=60, duration_minutes=1)
        assert decision.accepted
        first = service.submit_approved_intent(session, intent.id)
        second = service.submit_approved_intent(session, intent.id)
    assert first.id == second.id
    assert len(broker.submitted) == 1


def test_reconciliation_run_is_queryable_via_api_after_failure(install_broker) -> None:
    """A FAILED reconciliation run must be observable through the API."""
    broker = install_broker
    _connect(broker)
    broker.fail_account = True
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        response = client.post("/api/v1/reconciliation/run", headers=AUTH)
        assert response.status_code == 502
        runs = client.get("/api/v1/reconciliation").json()
    assert runs[0]["state"] == "FAILED"
    assert "account request timed out" in runs[0]["error_message"]


# =============================================================== Layer 2: failure injection


def test_connect_auth_failure_fails_closed_and_audits(install_broker) -> None:
    broker = install_broker
    broker.fail_connect = True
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.post("/api/v1/broker/credentials", headers=AUTH, json={"email": "e@example.com", "password": "practice-pass-123"})
        response = client.post("/api/v1/broker/connect", headers=AUTH)
        assert response.status_code == 502
        assert broker.health().state == "AUTH_FAILED"
        health = client.get("/api/v1/health").json()
    assert health["broker_connection"] == "AUTH_FAILED"
    assert _latest_event_type(["BROKER_CONNECT_FAILED"]) == "BROKER_CONNECT_FAILED"
    with SessionLocal() as session:
        assert session.scalar(select(AccountSnapshot).limit(1)) is None


def test_connect_mode_mismatch_fails_closed_and_audits(install_broker) -> None:
    """A broker that refuses to confirm PRACTICE must never be used."""
    broker = install_broker
    broker.connect_mode = "REAL"
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.post("/api/v1/broker/credentials", headers=AUTH, json={"email": "e@example.com", "password": "practice-pass-123"})
        response = client.post("/api/v1/broker/connect", headers=AUTH)
        # Inspect inside the app context: lifespan shutdown legitimately
        # disconnects the adapter afterwards.
        assert broker.health().state == "MODE_MISMATCH"
    assert response.status_code == 502
    assert "PRACTICE balance mode" in response.json()["detail"]
    assert _latest_event_type(["BROKER_CONNECT_FAILED"]) == "BROKER_CONNECT_FAILED"


def test_mid_session_mode_drift_fails_reconciliation(install_broker) -> None:
    """Mode drift after connect must abort reconciliation with a FAILED run."""
    broker = install_broker
    _connect(broker)
    broker.live_mode = "REAL"
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        response = client.post("/api/v1/reconciliation/run", headers=AUTH)
    assert response.status_code == 502
    with SessionLocal() as session:
        run = session.scalar(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(1))
        assert run.state == "FAILED" and "drifted away from PRACTICE" in run.error_message
        assert session.scalar(select(AccountSnapshot).limit(1)) is None
        assert session.scalar(select(Candle).limit(1)) is None  # nothing half-committed
    assert _latest_event_type(["RECONCILIATION_FAILED"]) == "RECONCILIATION_FAILED"


def test_candle_ingestion_failure_mid_watchlist_rolls_back_run(install_broker) -> None:
    """A candle stream dying on the second symbol must not half-commit the first."""
    broker = install_broker
    broker.candles_by_symbol["GBPUSD"] = sine_candles(200)
    broker.fail_candles_on.add("GBPUSD")
    _connect(broker)
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [
            {"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True},
            {"symbol": "GBPUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        response = client.post("/api/v1/reconciliation/run", headers=AUTH)
    assert response.status_code == 502
    with SessionLocal() as session:
        assert session.scalar(select(Candle).limit(1)) is None, "EURUSD candles leaked past the rollback"
        run = session.scalar(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(1))
        assert run.state == "FAILED" and "GBPUSD" in run.error_message
        assert session.scalar(select(AccountSnapshot).limit(1)) is None


def test_submit_with_disconnected_broker_keeps_intent_approved(monkeypatch) -> None:
    """Broker drops between approval and submit: intent stays retryable."""
    _enable_practice_execution(monkeypatch)
    from app.services.credentials import BrokerCredentials

    broker = FakeBrokerAdapter()
    broker.connect_practice(BrokerCredentials(email="e@example.com", password="practice-pass-123"))
    service = ExecutionService(broker, __import__("app.main", fromlist=["settings"]).settings)
    with SessionLocal() as session:
        _seed_active_world(session)
        intent, decision = service.create_intent(session, idempotency_key=None, strategy_id=session.scalar(select(StrategyVersion.id).limit(1)),
                                                  symbol="EURUSD", side="CALL", amount=5.0, timeframe_seconds=60, duration_minutes=1)
        assert decision.accepted
        broker.disconnect()  # the drop happens after approval, before submit
        with pytest.raises(BrokerError):
            service.submit_approved_intent(session, intent.id)
        refreshed = session.get(OrderIntent, intent.id)
        assert refreshed.status == "APPROVED"
        assert session.scalar(select(OrderRecord).limit(1)) is None


def test_submit_disabled_by_config_fails_closed(monkeypatch) -> None:
    """practice_execution_enabled=False must hard-block broker submission."""
    service_settings = Settings(_env_file=None, practice_execution_enabled=False)
    service = ExecutionService(FakeBrokerAdapter(), service_settings)
    with SessionLocal() as session:
        _seed_active_world(session)
        intent, _ = service.create_intent(session, idempotency_key=None, strategy_id=session.scalar(select(StrategyVersion.id).limit(1)),
                                          symbol="EURUSD", side="CALL", amount=5.0, timeframe_seconds=60, duration_minutes=1)
        with pytest.raises(BrokerError, match="disabled"):
            service.submit_approved_intent(session, intent.id)


def test_rejected_intent_cannot_be_submitted(monkeypatch) -> None:
    """A REJECTED intent must be un-submittable even if the caller tries."""
    service = ExecutionService(FakeBrokerAdapter(), Settings(_env_file=None, practice_execution_enabled=True))
    with SessionLocal() as session:
        _seed_active_world(session)
        # System is ACTIVE in seed but broker is disconnected -> gate rejects.
        intent, decision = service.create_intent(session, idempotency_key=None, strategy_id=session.scalar(select(StrategyVersion.id).limit(1)),
                                                  symbol="EURUSD", side="CALL", amount=5.0, timeframe_seconds=60, duration_minutes=1)
        assert not decision.accepted
        with pytest.raises(BrokerError, match="Only APPROVED"):
            service.submit_approved_intent(session, intent.id)


def test_stale_market_rejects_intent(monkeypatch) -> None:
    service = ExecutionService(FakeBrokerAdapter(), Settings(_env_file=None, practice_execution_enabled=True))
    with SessionLocal() as session:
        _seed_active_world(session, candle_minutes_ago=15)
        broker = service.broker
        broker._connected = True
        from app.services.broker import BrokerHealth

        broker._health = BrokerHealth("CONNECTED", "test")
        intent, decision = service.create_intent(session, idempotency_key=None, strategy_id=session.scalar(select(StrategyVersion.id).limit(1)),
                                                 symbol="EURUSD", side="CALL", amount=5.0, timeframe_seconds=60, duration_minutes=1)
    assert not decision.accepted and "stale" in decision.reason.lower()
    assert intent.status == "REJECTED"


def test_daily_loss_limit_rejects_intent(monkeypatch) -> None:
    service = ExecutionService(FakeBrokerAdapter(), Settings(_env_file=None, practice_execution_enabled=True))
    with SessionLocal() as session:
        account, _, strategy = _seed_active_world(session)
        now = datetime.now(UTC)
        session.add(OrderIntent(idempotency_key="seed-loss", strategy_version_id=strategy.id, symbol="EURUSD",
                                mode="PRACTICE", side="CALL", requested_amount=5.0, rationale={}, status="SUBMITTED"))
        session.flush()
        record = OrderRecord(order_intent_id=session.scalar(select(OrderIntent.id).order_by(OrderIntent.id.desc()).limit(1)),
                             broker_order_id="seed-order-1", status="SETTLED", request_payload={}, broker_response={}, submitted_at=now)
        session.add(record)
        session.flush()
        session.add(TradeOutcome(order_record_id=record.id, realized_pnl=-30.0, outcome="LOSS", settled_at=now, learning_tags={}))
        session.commit()
        broker = service.broker
        broker._connected = True
        from app.services.broker import BrokerHealth

        broker._health = BrokerHealth("CONNECTED", "test")
        intent, decision = service.create_intent(session, idempotency_key="after-loss", strategy_id=strategy.id,
                                                 symbol="EURUSD", side="CALL", amount=5.0, timeframe_seconds=60, duration_minutes=1)
    assert not decision.accepted and "daily loss" in decision.reason.lower()


def test_open_position_cap_rejects_intent(monkeypatch) -> None:
    service = ExecutionService(FakeBrokerAdapter(), Settings(_env_file=None, practice_execution_enabled=True))
    with SessionLocal() as session:
        account, _, strategy = _seed_active_world(session)
        session.add(PositionSnapshot(broker_position_id="pos-live", instrument_type="digital-option",
                                     symbol="EURUSD", state="OPEN", raw_payload={}))
        session.commit()
        broker = service.broker
        broker._connected = True
        from app.services.broker import BrokerHealth

        broker._health = BrokerHealth("CONNECTED", "test")
        intent, decision = service.create_intent(session, idempotency_key="cap-test", strategy_id=strategy.id,
                                                 symbol="EURUSD", side="CALL", amount=5.0, timeframe_seconds=60, duration_minutes=1)
    assert not decision.accepted and "exposure" in decision.reason.lower()


def test_settlement_only_matches_known_broker_orders(install_broker) -> None:
    """Unknown broker positions must be observed, never turned into outcomes."""
    broker = install_broker
    _connect(broker)
    broker.positions_payload = [iqair_position(position_id="pos-unknown", order_id="order-unknown", status="won", pnl=9.9)]
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        client.put("/api/v1/watchlist", json={"items": [{"symbol": "EURUSD", "category": "forex", "timeframe_seconds": 60, "enabled": True}]})
        response = client.post("/api/v1/reconciliation/run", headers=AUTH)
        assert response.status_code == 200
    with SessionLocal() as session:
        assert session.scalar(select(TradeOutcome).limit(1)) is None
        position = session.scalar(select(PositionSnapshot).where(PositionSnapshot.broker_position_id == "pos-unknown"))
        assert position is not None and position.state == "WON"


# =============================================================== runtime loop injection


def test_runtime_halt_latches_after_connect_failure() -> None:
    """A failing broker inside the live runtime thread halts the system once."""
    broker = FakeBrokerAdapter(fail_connect=True)
    settings = Settings(_env_file=None, auto_reconcile_enabled=True, broker_sync_interval_seconds=10)
    settings.broker_sync_interval_seconds = 0  # bypass validation for a fast loop
    worker = BrokerWorker(broker, 200)
    vault = CredentialVault(__import__("app.main", fromlist=["settings"]).settings.credential_encryption_key)
    with SessionLocal() as session:
        session.add(EncryptedBrokerCredential(email_ciphertext=vault.encrypt("e@example.com"), password_ciphertext=vault.encrypt("p")))
        session.commit()

    runtime = LocalRuntime(settings, SessionLocal, worker)
    runtime.start()
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        while datetime.now(UTC) < deadline:
            with SessionLocal() as session:
                account = session.scalar(select(AccountConfig).limit(1))
                if account and account.system_state == SystemState.HALTED.value and broker.connect_calls >= 1:
                    break
            runtime._stop.wait(0.05)
        with SessionLocal() as session:
            account = session.scalar(select(AccountConfig).limit(1))
            assert account.system_state == SystemState.HALTED.value
            assert session.scalar(select(AuditEvent).where(AuditEvent.event_type == "LOCAL_RUNTIME_HALTED")) is not None
        calls_after_halt = broker.connect_calls
        runtime._stop.wait(1.0)
        assert broker.connect_calls == calls_after_halt, "runtime retried connect after halt latch"
    finally:
        runtime.stop()
    assert broker.disconnect_calls >= 1


def test_runtime_reconnects_cleanly_after_drop() -> None:
    """Positive control: a dropped session is re-established exactly once."""
    broker = FakeBrokerAdapter(candles_by_symbol={"EURUSD": sine_candles(30)})
    settings = Settings(_env_file=None, auto_reconcile_enabled=True, broker_sync_interval_seconds=10)
    settings.broker_sync_interval_seconds = 0
    worker = BrokerWorker(broker, 200)
    vault = CredentialVault(__import__("app.main", fromlist=["settings"]).settings.credential_encryption_key)
    with SessionLocal() as session:
        session.add(EncryptedBrokerCredential(email_ciphertext=vault.encrypt("e@example.com"), password_ciphertext=vault.encrypt("p")))
        session.commit()

    runtime = LocalRuntime(settings, SessionLocal, worker)
    runtime.start()
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        while datetime.now(UTC) < deadline:
            with SessionLocal() as session:
                run = session.scalar(select(ReconciliationRun).where(ReconciliationRun.state == "SUCCEEDED").order_by(ReconciliationRun.id.desc()).limit(1))
            if run is not None and broker.connect_calls >= 1:
                break
            runtime._stop.wait(0.05)
        assert broker.connect_calls == 1, "expected exactly one reconnect"
        calls_stable = broker.connect_calls
        runtime._stop.wait(1.0)
        assert broker.connect_calls == calls_stable, "reconnect loop flapped while healthy"
    finally:
        runtime.stop()
    assert broker.health().state == "DISCONNECTED"


# ---------------------------------------------------------------- shared helper


def _seed_active_world(session, *, candle_minutes_ago: int = 0):
    """ACTIVE PRACTICE account, policy, validated strategy, fresh candle, balance."""
    session.query(AccountConfig).delete()
    session.query(RiskPolicy).delete()
    session.query(Candle).delete()
    session.query(StrategyVersion).delete()
    session.query(OrderIntent).delete()
    account = AccountConfig(account_label="probe", mode="PRACTICE", real_execution_enabled=False,
                            system_state=SystemState.ACTIVE.value)
    session.add(account)
    session.add(RiskPolicy(version="probe-policy", max_trade_amount=5.0, active=True))
    now = datetime.now(UTC) - timedelta(minutes=candle_minutes_ago)
    session.add(Candle(symbol="EURUSD", timeframe_seconds=60, open_time=now, open_price=1.1,
                       high_price=1.1, low_price=1.1, close_price=1.1))
    strategy = StrategyVersion(strategy_key="probe", version="v1", status=StrategyStatus.VALIDATED.value, definition={})
    session.add(strategy)
    session.flush()
    session.add(AccountSnapshot(account_config_id=account.id, account_mode="PRACTICE", balance=1_000.0,
                                currency="USD", broker_timestamp=now, raw_payload={}))
    session.commit()
    return account, session.scalar(select(RiskPolicy).limit(1)), strategy
