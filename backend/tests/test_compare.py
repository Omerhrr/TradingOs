"""Strategy-comparison analytics: side-by-side evidence per strategy version.

The comparison merges three read-only layers per strategy — settled live
outcomes, gate activity, and the latest walk-forward evaluation. These probes
pin the arithmetic (win rate, per-strategy drawdown isolation, profit factor)
and the exclusion rule: manual traffic never masquerades as a strategy row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Candle, OrderIntent, OrderRecord, StrategyEvaluation, StrategyVersion, TradeOutcome


def _ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)  # helpers may run before any client lifespan


def _seed_strategy(key: str, version: str = "v1", status: str = "VALIDATED", **definition) -> StrategyVersion:
    _ensure_schema()
    with SessionLocal() as session:
        strategy = StrategyVersion(strategy_key=key, version=version, status=status,
                                   definition={"kind": "ema_cross", "fast_window": 12, "slow_window": 26, **definition})
        session.add(strategy)
        session.commit()
        session.refresh(strategy)
        return strategy


def _settle(session: Session, *, strategy_id: int, key: str, symbol: str, side: str,
            pnl: float, outcome: str, status: str = "SETTLED") -> None:
    intent = OrderIntent(idempotency_key=key, strategy_version_id=strategy_id, symbol=symbol,
                         side=side, requested_amount=5.0, status=status, rationale={}, mode="PRACTICE")
    session.add(intent)
    session.flush()
    record = OrderRecord(order_intent_id=intent.id, status="SETTLED", request_payload={}, broker_response={})
    session.add(record)
    session.flush()
    session.add(TradeOutcome(order_record_id=record.id, realized_pnl=pnl, outcome=outcome, settled_at=datetime.now(UTC)))


def test_empty_world_returns_empty_comparison() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/strategies/compare")
    assert response.status_code == 200
    assert response.json()["strategies"] == []


def test_live_aggregates_are_per_strategy_isolated() -> None:
    strong = _seed_strategy("ema-strong", definition={"fast_window": 8})
    weak = _seed_strategy("ema-weak", definition={"fast_window": 20})
    with SessionLocal() as session:
        # strong: +2, +3, -1 -> net +4, 2 wins / 3 trades
        # equity 2, 5, 4 with peak 2, 5, 5 -> drawdown 1.0 at the end
        _settle(session, strategy_id=strong.id, key="k1", symbol="EURUSD", side="CALL", pnl=2.0, outcome="WIN")
        _settle(session, strategy_id=strong.id, key="k2", symbol="EURUSD", side="CALL", pnl=3.0, outcome="WIN")
        _settle(session, strategy_id=strong.id, key="k3", symbol="EURUSD", side="PUT", pnl=-1.0, outcome="LOSS")
        # weak: -2, -3 -> net -5, dd: equity 0,-2,-5 with peak 0 -> dd 5
        _settle(session, strategy_id=weak.id, key="k4", symbol="GBPUSD", side="PUT", pnl=-2.0, outcome="LOSS")
        _settle(session, strategy_id=weak.id, key="k5", symbol="GBPUSD", side="CALL", pnl=-3.0, outcome="LOSS")
        # manual traffic must not appear as a strategy
        _settle(session, strategy_id=None, key="manual-1", symbol="EURUSD", side="CALL", pnl=9.0, outcome="WIN")
        # a rejected intent counts as gate activity, not a live trade
        session.add(OrderIntent(idempotency_key="k6", strategy_version_id=weak.id, symbol="EURUSD",
                                side="PUT", requested_amount=5.0, status="REJECTED", rationale={}, mode="PRACTICE"))
        session.commit()
    with TestClient(app) as client:
        rows = {row["strategy_key"]: row for row in client.get("/api/v1/analytics/strategies/compare").json()["strategies"]}
    assert set(rows) == {"ema-strong", "ema-weak"}
    strong_row = rows["ema-strong"]["live"]
    assert strong_row["trades"] == 3 and strong_row["wins"] == 2 and strong_row["losses"] == 1
    assert abs(strong_row["net_pnl"] - 4.0) < 1e-9
    assert abs(strong_row["win_rate"] - 2 / 3) < 1e-6
    assert abs(strong_row["profit_factor"] - 5.0 / 1.0) < 1e-6
    assert abs(strong_row["max_drawdown"] - 1.0) < 1e-9, "peak 5, final equity 4 -> dd 1"
    weak_row = rows["ema-weak"]["live"]
    assert abs(weak_row["net_pnl"] - (-5.0)) < 1e-9
    assert abs(weak_row["max_drawdown"] - 5.0) < 1e-9, "a strategy cannot inherit another's hole"
    assert rows["ema-weak"]["activity"]["rejected"] == 1
    assert rows["ema-weak"]["activity"]["intents"] == 3
    assert rows["ema-strong"]["activity"]["submitted"] == 3


def test_latest_evaluation_is_surfaced() -> None:
    strategy = _seed_strategy("ema-evaluated")
    with SessionLocal() as session:
        session.add_all([
            StrategyEvaluation(strategy_version_id=strategy.id, dataset_start=datetime.now(UTC) - timedelta(hours=2),
                               dataset_end=datetime.now(UTC) - timedelta(hours=1), censor_gap_seconds=60,
                               metrics={"trades": 40, "win_rate": 0.55, "total_return": 0.08, "method": "ema_cross_walk_forward"},
                               accepted=False),
            StrategyEvaluation(strategy_version_id=strategy.id, dataset_start=datetime.now(UTC) - timedelta(hours=1),
                               dataset_end=datetime.now(UTC), censor_gap_seconds=60,
                               metrics={"trades": 37, "win_rate": 0.62, "total_return": 0.11, "method": "ema_cross_walk_forward"},
                               accepted=True),
        ])
        session.commit()
    with TestClient(app) as client:
        rows = client.get("/api/v1/analytics/strategies/compare").json()["strategies"]
    assert len(rows) == 1
    evaluation = rows[0]["evaluation"]
    assert evaluation["evaluated"] is True
    assert evaluation["accepted"] is True, "only the newest evaluation is reported"
    assert evaluation["metrics"]["trades"] == 37


def test_unvalidated_strategies_are_still_listed() -> None:
    _seed_strategy("ema-draft", status="DRAFT")
    with TestClient(app) as client:
        rows = client.get("/api/v1/analytics/strategies/compare").json()["strategies"]
    assert len(rows) == 1
    assert rows[0]["status"] == "DRAFT"
    assert rows[0]["live"]["trades"] == 0
    assert rows[0]["live"]["net_pnl"] == 0.0
    assert rows[0]["evaluation"]["evaluated"] is False
