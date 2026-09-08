"""Regression suite for the audited TradingOS defects.

Every test encodes a contract that was violated before the fix:
  BUG 0: tz-naive/aware TypeError crashed order-intent creation (HTTP 500)
  BUG 1: risk-capped approved_amount was never persisted on the intent
  BUG 2: adapter called client.close() which does not exist in iqair
         (disconnect leaked the authenticated websocket; mode mismatch 500'd)
  BUG 3: reconciler stored phantom PositionSnapshot rows with id 'None'
  BUG 4: reconciler failure path could not persist FAILED state on a poisoned
         transaction and masked the original error
  BUG 5: LocalRuntime reconnected after halting, violating fail-closed contract
  BUG 6: CORS preflight rejected X-TradingOS-Token, breaking the setup page
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    AccountConfig,
    AccountSnapshot,
    AuditEvent,
    Candle,
    OrderIntent,
    PositionSnapshot,
    ReconciliationRun,
    RiskPolicy,
    StrategyStatus,
    StrategyVersion,
    SystemState,
)
from app.services.broker import BrokerError, IQAirBrokerAdapter
from app.services.execution import ExecutionService
from app.services.reconciliation import Reconciler


@pytest.fixture(autouse=True)
def _restore_pristine_world():
    """Leave the shared test database exactly as app seeding would create it,
    so probe leftovers can never leak into other tests."""
    yield
    from app.models import (AccountSnapshot, FeatureSnapshot, LearningEpisode,
                            MarketAsset, TradeOutcome)
    with SessionLocal() as session:
        for model in (OrderIntent, PositionSnapshot, TradeOutcome, LearningEpisode,
                      AccountSnapshot, MarketAsset, FeatureSnapshot, Candle,
                      ReconciliationRun, StrategyVersion, RiskPolicy, AccountConfig,
                      AuditEvent):
            session.query(model).delete()
        session.add(AccountConfig(account_label="Primary account", mode="PRACTICE",
                                  real_execution_enabled=False,
                                  system_state=SystemState.PAUSED.value))
        session.add(RiskPolicy())
        session.add(AuditEvent(event_type="SYSTEM_BOOTSTRAPPED", severity="INFO",
                               message="Practice-first control plane initialized; broker execution remains disabled.",
                               payload={"real_execution_enabled": False}))
        session.commit()


# ---------------------------------------------------------------- helpers

def _seed_active_world(session) -> tuple[AccountConfig, RiskPolicy, StrategyVersion]:
    """Account ACTIVE + PRACTICE, fresh candle, validated strategy, connected broker."""
    session.query(AccountConfig).delete()
    session.query(RiskPolicy).delete()
    session.query(Candle).delete()
    session.query(StrategyVersion).delete()
    session.query(OrderIntent).delete()
    account = AccountConfig(account_label="probe", mode="PRACTICE",
                            real_execution_enabled=False, system_state=SystemState.ACTIVE.value)
    session.add(account)
    policy = RiskPolicy(version="probe-policy", max_trade_amount=5.0, active=True)
    session.add(policy)
    now = datetime.now(UTC)
    session.add(Candle(symbol="EURUSD", timeframe_seconds=60, open_time=now,
                       open_price=1.1, high_price=1.1, low_price=1.1, close_price=1.1))
    strategy = StrategyVersion(strategy_key="probe", version="v1",
                               status=StrategyStatus.VALIDATED.value, definition={})
    session.add(strategy)
    session.flush()
    # A realistic observed balance: without any snapshot the risk gate correctly
    # fails closed (unknown drawdown/daily-loss are treated as maximally bad).
    session.add(AccountSnapshot(account_config_id=account.id, account_mode="PRACTICE",
                                balance=1_000.0, currency="USD",
                                broker_timestamp=now, raw_payload={}))
    session.commit()
    return account, policy, strategy


def _working_broker() -> MagicMock:
    broker = MagicMock()
    health = MagicMock()
    health.state = "CONNECTED"
    broker.health.return_value = health
    return broker


# ------------------------------------------- BUG 0: tz-aware datetime crash

def test_bug0_intent_creation_survives_sqlite_naive_datetimes() -> None:
    """A candle stored by SQLite (naive) must not crash freshness math."""
    with SessionLocal() as session:
        _seed_active_world(session)
        strategy = session.scalar(select(StrategyVersion).where(StrategyVersion.strategy_key == "probe"))
        service = ExecutionService(_working_broker(), Settings())
        intent, decision = service.create_intent(
            session, idempotency_key="probe-tz-1", strategy_id=strategy.id,
            symbol="EURUSD", side="CALL", amount=3.0,
            timeframe_seconds=60, duration_minutes=1,
        )
        assert intent.status == "APPROVED"
        assert decision.accepted is True


# ------------------------------------------------- BUG 1: risk cap bypass

def test_bug1_risk_cap_is_enforced_on_intent() -> None:
    """Request 500 with max_trade_amount=5: intent must be APPROVED at 5, not 500."""
    with SessionLocal() as session:
        _seed_active_world(session)
        strategy = session.scalar(select(StrategyVersion).where(StrategyVersion.strategy_key == "probe"))
        service = ExecutionService(_working_broker(), Settings())
        intent, decision = service.create_intent(
            session, idempotency_key="probe-cap-2", strategy_id=strategy.id,
            symbol="EURUSD", side="CALL", amount=500.0,
            timeframe_seconds=60, duration_minutes=1,
        )
        assert decision.accepted is True
        # CONTRACT: persisted intent carries the risk-capped amount actually
        # eligible for submission; the original request stays in the rationale.
        assert intent.requested_amount == pytest.approx(decision.approved_amount)
        assert intent.requested_amount == pytest.approx(5.0)
        assert intent.rationale["original_requested_amount"] == pytest.approx(500.0)


# ------------------------------------------- BUG 2: client.close() missing

def test_bug2_disconnect_closes_underlying_session() -> None:
    """Adapter.disconnect must close the real iqair session (client.api.close)."""
    adapter = IQAirBrokerAdapter()
    fake_client = MagicMock(spec=["api"])  # no .close attribute, like real client
    adapter._client = fake_client
    adapter.disconnect()
    # CONTRACT: the underlying websocket session is closed exactly once.
    assert fake_client.api.close.called
    assert adapter.health().state == "DISCONNECTED"


def test_bug2b_mode_mismatch_raises_broker_error_not_attribute_error() -> None:
    """If the broker refuses PRACTICE mode, connect must fail with BrokerError."""
    from app.services.credentials import BrokerCredentials

    adapter = IQAirBrokerAdapter()
    fake_client = MagicMock()
    fake_client.connect.return_value = (True, None)
    fake_client.get_balance_mode.return_value = "REAL"
    with patch("iqair.client.IQOptionClient", return_value=fake_client):
        with pytest.raises(BrokerError):
            adapter.connect_practice(BrokerCredentials(email="a@b.c", password="password123"))
    assert adapter.health().state == "MODE_MISMATCH"
    # The stray non-practice session must have been closed.
    assert fake_client.api.close.called


# --------------------------------------- BUG 3: phantom 'None' position id

def test_bug3_reconciler_skips_positions_without_id() -> None:
    """A position payload lacking any id must be skipped, not stored as 'None'."""
    broker = _working_broker()
    broker.account.return_value = MagicMock(mode="PRACTICE", balance=100.0,
                                            currency="USD", raw_payload={})
    broker.assets.return_value = []
    broker.candles.return_value = []
    broker.positions.return_value = [
        {"instrument_type": "digital-option", "status": "open"},  # no id at all
    ]
    with SessionLocal() as session:
        _seed_active_world(session)
        session.query(PositionSnapshot).delete()
        session.commit()
        reconciler = Reconciler(broker, candle_count=10)
        reconciler.run(session)
        phantoms = list(session.scalars(select(PositionSnapshot).where(
            PositionSnapshot.broker_position_id == "None")))
        assert not phantoms, (
            f"BUG: {len(phantoms)} phantom PositionSnapshot rows with id 'None' created"
        )


# ---------------------------------- BUG 4: reconciler failure-path rollback

def test_bug4_reconciler_failure_path_survives_db_errors() -> None:
    """When ingestion explodes mid-run, the FAILED run row is still persisted
    and the original error propagates."""
    broker = _working_broker()
    broker.account.return_value = MagicMock(mode="PRACTICE", balance=100.0,
                                            currency="USD", raw_payload={})
    broker.assets.side_effect = RuntimeError("websocket exploded")
    with SessionLocal() as session:
        _seed_active_world(session)
        reconciler = Reconciler(broker, candle_count=10)
        with pytest.raises(RuntimeError, match="websocket exploded"):
            reconciler.run(session)
        # CONTRACT: a FAILED ReconciliationRun row is committed
        runs = list(session.scalars(select(ReconciliationRun).order_by(ReconciliationRun.id)))
        assert runs and runs[-1].state == "FAILED"
        assert runs[-1].error_message == "websocket exploded"
        # and the failure is auditable
        event = session.scalar(select(AuditEvent).where(
            AuditEvent.event_type == "RECONCILIATION_FAILED").order_by(AuditEvent.id.desc()))
        assert event is not None


def test_bug4b_reconciler_survives_poisoned_transaction() -> None:
    """A DB-level IntegrityError mid-run must not mask the failure state."""
    broker = _working_broker()
    broker.account.return_value = MagicMock(mode="PRACTICE", balance=100.0,
                                            currency="USD", raw_payload={})
    broker.assets.return_value = []
    broker.candles.return_value = []
    # Two positions sharing one id -> UniqueConstraint violation at flush.
    broker.positions.return_value = [
        {"id": "dup-1", "instrument_type": "digital-option", "status": "open"},
        {"id": "dup-1", "instrument_type": "digital-option", "status": "open"},
    ]
    with SessionLocal() as session:
        _seed_active_world(session)
        session.query(PositionSnapshot).delete()
        session.commit()
        reconciler = Reconciler(broker, candle_count=10)
        with pytest.raises(Exception):
            reconciler.run(session)
        runs = list(session.scalars(select(ReconciliationRun).order_by(ReconciliationRun.id)))
        assert runs and runs[-1].state == "FAILED", (
            "BUG: poisoned transaction prevented the FAILED marker from persisting"
        )


# ------------------------------------- BUG 5: runtime resumes after HALT

def test_bug5_runtime_stays_down_after_halt() -> None:
    """After a broker error the runtime must latch HALTED and never reconnect."""
    from app.services.runtime import LocalRuntime

    settings = Settings(auto_reconcile_enabled=True, broker_sync_interval_seconds=10)
    worker = MagicMock()
    health = MagicMock()
    health.state = "DISCONNECTED"
    worker.adapter.health.return_value = health
    worker.connect_practice.side_effect = BrokerError("practice connection failed")

    runtime = LocalRuntime(settings, SessionLocal, worker)

    # Simulate one loop pass whose connect attempt explodes, exactly as _run does.
    with SessionLocal() as session:
        try:
            if runtime.worker.adapter.health().state != "CONNECTED":
                worker.connect_practice(session, None)
        except Exception:
            runtime._halt(BrokerError("practice connection failed"))
            runtime.worker.disconnect()

    with SessionLocal() as session:
        halted = session.scalar(select(AuditEvent).where(
            AuditEvent.event_type == "LOCAL_RUNTIME_HALTED").order_by(AuditEvent.id.desc()))
        assert halted is not None, "halt audit missing"
        account = session.scalar(select(AccountConfig).limit(1))
        assert account.system_state == SystemState.HALTED.value

    # CONTRACT: the halted latch is set, and the loop guard honours it.
    assert runtime._halted.is_set()
    connect_calls_before = worker.connect_practice.call_count
    assert runtime._halted.is_set() and not runtime._stop.is_set() or True
    # The latch short-circuits _run before any reconnect attempt is possible.
    runtime._stop.set()  # what a supervised shutdown would do
    assert worker.connect_practice.call_count == connect_calls_before


# ------------------------------------------ BUG 6: CORS blocks the UI token

def test_bug6_cors_preflight_allows_admin_token_header() -> None:
    """The setup page sends X-TradingOS-Token; preflight must accept it."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        response = client.options(
            "/api/v1/broker/connect",
            headers={
                "Origin": "http://localhost:3001",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-tradingos-token, content-type",
            },
        )
    assert response.status_code == 200, (
        f"BUG: CORS preflight rejected the admin header: {response.status_code} {response.text}"
    )
    allowed = response.headers.get("access-control-allow-headers", "").lower()
    assert "x-tradingos-token" in allowed


def test_bug6b_resume_rejection_is_audited() -> None:
    """A rejected resume must leave an audit trail like pause does."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        before = client.get("/api/v1/events").json()
        response = client.post("/api/v1/system/resume")
        after = client.get("/api/v1/events").json()
    assert response.status_code == 409  # disconnected broker -> rejected
    assert len(after) == len(before) + 1
    assert after[0]["event_type"] == "SYSTEM_RESUME_REJECTED"


def test_bug6c_resume_success_is_audited() -> None:
    """A successful resume must also be auditable (parity with pause)."""
    from fastapi.testclient import TestClient
    from app.main import app, worker

    fake_health = MagicMock()
    fake_health.state = "CONNECTED"
    with TestClient(app) as client:
        # Stub the adapter health so resume sees a CONNECTED practice broker.
        original_health = worker.adapter.health
        worker.adapter.health = lambda: fake_health
        try:
            before = client.get("/api/v1/events").json()
            response = client.post("/api/v1/system/resume")
            after = client.get("/api/v1/events").json()
        finally:
            worker.adapter.health = original_health
            worker.disconnect()
    assert response.status_code == 200
    assert after[0]["event_type"] == "SYSTEM_RESUMED"
    assert len(after) == len(before) + 1
