"""Isolated runtime for the TradingOS test suite.

Every test run gets a throwaway database, a configured admin token, and a
fresh Fernet key, regardless of any developer ``.env`` file or leftover local
``data/tradingos.db``. Environment is pinned here BEFORE any ``app`` module is
imported: pydantic-settings gives os.environ higher precedence than .env
files, so these values deterministically win.
"""

from __future__ import annotations

import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="tradingos-tests-")

os.environ["TRADINGOS_DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"
os.environ["TRADINGOS_LOCAL_ADMIN_TOKEN"] = "test-local-admin-token"
os.environ["TRADINGOS_AUTO_RECONCILE_ENABLED"] = "false"
os.environ["TRADINGOS_PRACTICE_EXECUTION_ENABLED"] = "false"
os.environ["TRADINGOS_REAL_EXECUTION_ENABLED"] = "false"

from cryptography.fernet import Fernet  # noqa: E402

os.environ["TRADINGOS_CREDENTIAL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_pristine_world():
    """Every test ends with the shared database exactly as app seeding leaves it.

    Service-level tests never open a TestClient, so the schema must be created
    up front; the post-test wipe plus reseed keeps probe leftovers from leaking
    between tests in any order.
    """
    yield
    from sqlalchemy import select

    from app.database import Base, SessionLocal, engine
    from app.models import (
        AccountConfig,
        AccountSnapshot,
        AuditEvent,
        Candle,
        EncryptedBrokerCredential,
        FeatureSnapshot,
        LearningEpisode,
        LoopRun,
        MarketAsset,
        OrderIntent,
        OrderRecord,
        PositionSnapshot,
        ReconciliationRun,
        RiskPolicy,
        StrategyEvaluation,
        StrategyVersion,
        SystemState,
        TradeOutcome,
    )

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        for model in (OrderIntent, OrderRecord, PositionSnapshot, TradeOutcome,
                      LearningEpisode, AccountSnapshot, MarketAsset, FeatureSnapshot,
                      Candle, ReconciliationRun, LoopRun, StrategyEvaluation, StrategyVersion, RiskPolicy,
                      AccountConfig, EncryptedBrokerCredential, AuditEvent):
            session.query(model).delete()
        if session.scalar(select(AccountConfig.id).limit(1)) is None:
            session.add(AccountConfig(account_label="Primary account", mode="PRACTICE",
                                      real_execution_enabled=False,
                                      system_state=SystemState.PAUSED.value))
        if session.scalar(select(RiskPolicy.id).where(RiskPolicy.active.is_(True)).limit(1)) is None:
            session.add(RiskPolicy())
        if session.scalar(select(AuditEvent.id).limit(1)) is None:
            session.add(AuditEvent(event_type="SYSTEM_BOOTSTRAPPED", severity="INFO",
                                   message="Practice-first control plane initialized; broker execution remains disabled.",
                                   payload={"real_execution_enabled": False}))
        session.commit()
