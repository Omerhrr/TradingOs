"""Trade-outcome analytics and strategy status governance tests.

The analytics endpoint is a read-only projection: these tests seed known
intent -> order -> outcome chains and assert the exact aggregates, the equity
curve geometry, and the group breakdowns. The strategy status endpoint is
governance: only DRAFT/RETIRED are reachable; VALIDATED stays earned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AuditEvent, OrderIntent, OrderRecord, StrategyEvaluation, StrategyVersion, TradeOutcome

from app.main import app

AUTH = {"X-TradingOS-Token": "test-local-admin-token"}


def _seed_trade(
    *,
    idempotency_key: str,
    strategy_id: int | None,
    symbol: str,
    side: str,
    amount: float,
    broker_order_id: str,
    pnl: float,
    outcome: str,
    settled_at: datetime,
) -> None:
    with SessionLocal() as session:
        intent = OrderIntent(
            idempotency_key=idempotency_key,
            strategy_version_id=strategy_id,
            symbol=symbol,
            mode="PRACTICE",
            side=side,
            requested_amount=amount,
            rationale={},
            status="SETTLED",
        )
        session.add(intent)
        session.flush()
        record = OrderRecord(
            order_intent_id=intent.id,
            broker_order_id=broker_order_id,
            status="SETTLED",
            request_payload={},
            broker_response={},
            submitted_at=settled_at,
            reconciled_at=settled_at,
        )
        session.add(record)
        session.flush()
        session.add(TradeOutcome(order_record_id=record.id, realized_pnl=pnl, outcome=outcome, settled_at=settled_at, learning_tags={}))
        session.add(AuditEvent(event_type="TEST_SEEDED", severity="INFO", message="test fixture", payload={}))
        session.commit()


def _seed_validated_strategy(session, key: str = "ema_test", version: str = "v1") -> StrategyVersion:
    strategy = StrategyVersion(strategy_key=key, version=version, status="VALIDATED", definition={"kind": "ema_cross", "fast_window": 5, "slow_window": 10}, validation_summary={})
    session.add(strategy)
    session.commit()
    session.refresh(strategy)
    return strategy


def test_empty_analytics_is_all_zeros_and_honest_nones() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/trades")
    assert response.status_code == 200
    body = response.json()
    assert body["total_trades"] == 0
    assert body["wins"] == 0
    assert body["losses"] == 0
    assert body["net_pnl"] == 0
    assert body["win_rate"] is None
    assert body["profit_factor"] is None
    assert body["max_drawdown"] == 0
    assert body["equity_curve"] == []
    assert body["by_symbol"] == []
    assert body["recent"] == []


def test_analytics_aggregates_match_a_known_trade_sequence() -> None:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    plan = [
        # (offset minutes, symbol, side, pnl, outcome)
        (0, "EURUSD", "CALL", 5.0, "WIN"),
        (1, "GBPUSD", "PUT", -3.0, "LOSS"),
        (2, "GBPUSD", "CALL", 8.0, "WIN"),
        (3, "EURUSD", "PUT", -2.0, "LOSS"),
    ]
    for index, (offset, symbol, side, pnl, outcome) in enumerate(plan):
        _seed_trade(
            idempotency_key=f"analytics-{index}",
            strategy_id=None,
            symbol=symbol,
            side=side,
            amount=5.0,
            broker_order_id=f"analytics-broker-{index}",
            pnl=pnl,
            outcome=outcome,
            settled_at=start + timedelta(minutes=offset),
        )

    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/trades")
    assert response.status_code == 200
    body = response.json()

    assert body["total_trades"] == 4
    assert body["wins"] == 2
    assert body["losses"] == 2
    assert body["flat"] == 0
    assert body["win_rate"] == 0.5
    assert body["net_pnl"] == 8.0
    assert body["avg_pnl"] == 2.0
    assert body["avg_win"] == 6.5
    assert body["avg_loss"] == -2.5
    assert body["profit_factor"] == 2.6  # gross 13 / gross loss 5
    assert body["best_pnl"] == 8.0
    assert body["worst_pnl"] == -3.0
    # equity walk: 5 -> 2 -> 10 -> 8. Dips below running peak: 5-2 = 3, then 10-8 = 2.
    assert [point["equity"] for point in body["equity_curve"]] == [5.0, 2.0, 10.0, 8.0]
    assert body["max_drawdown"] == 3.0
    # newest first in the recent ledger
    assert [row["realized_pnl"] for row in body["recent"]] == [-2.0, 8.0, -3.0, 5.0]
    assert all(row["strategy_key"] is None and row["strategy_version_id"] is None for row in body["recent"])

    symbols = {row["group"]: row for row in body["by_symbol"]}
    assert set(symbols) == {"EURUSD", "GBPUSD"}
    assert symbols["EURUSD"]["net_pnl"] == 3.0
    assert symbols["EURUSD"]["win_rate"] == 0.5
    assert symbols["GBPUSD"]["net_pnl"] == 5.0

    sides = {row["group"]: row for row in body["by_side"]}
    assert sides["CALL"]["net_pnl"] == 13.0
    assert sides["CALL"]["trades"] == 2
    assert sides["PUT"]["net_pnl"] == -5.0

    strategies = {row["group"]: row for row in body["by_strategy"]}
    assert set(strategies) == {"manual"}
    assert strategies["manual"]["trades"] == 4


def test_analytics_groups_strategy_versions_by_key_not_manual() -> None:
    with SessionLocal() as session:
        strategy = _seed_validated_strategy(session)
        strategy_id = strategy.id
    start = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    _seed_trade(idempotency_key="analytics-strat-0", strategy_id=strategy_id, symbol="EURUSD", side="CALL", amount=5.0, broker_order_id="analytics-strat-b0", pnl=4.0, outcome="WIN", settled_at=start)
    _seed_trade(idempotency_key="analytics-strat-1", strategy_id=strategy_id, symbol="EURUSD", side="CALL", amount=5.0, broker_order_id="analytics-strat-b1", pnl=-1.0, outcome="LOSS", settled_at=start + timedelta(minutes=1))

    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/trades")
    body = response.json()
    by_strategy = {row["group"]: row for row in body["by_strategy"]}
    assert set(by_strategy) == {"ema_test"}
    assert by_strategy["ema_test"]["wins"] == 1
    assert by_strategy["ema_test"]["losses"] == 1
    assert by_strategy["ema_test"]["net_pnl"] == 3.0
    recent = body["recent"][0]
    assert recent["strategy_key"] == "ema_test"
    assert recent["strategy_version_id"] == strategy_id


# --------------------------------------------------------------------- status


def test_strategy_status_update_round_trip_and_governance() -> None:
    with TestClient(app) as client:
        created = client.post("/api/v1/strategies", headers=AUTH, json={"strategy_key": "gov-strat", "version": "v1"})
        assert created.status_code == 200
        strategy_id = created.json()["id"]

        # VALIDATED cannot be set through the status endpoint: it is earned.
        assert client.put(f"/api/v1/strategies/{strategy_id}/status", headers=AUTH, json={"status": "VALIDATED"}).status_code == 422
        # Unknown strategy 404s.
        assert client.put("/api/v1/strategies/99999/status", headers=AUTH, json={"status": "RETIRED"}).status_code == 404
        # Missing token 401s.
        assert client.put(f"/api/v1/strategies/{strategy_id}/status", json={"status": "RETIRED"}).status_code == 401

        retired = client.put(f"/api/v1/strategies/{strategy_id}/status", headers=AUTH, json={"status": "RETIRED"})
        assert retired.status_code == 200
        assert retired.json()["status"] == "RETIRED"

        redrafted = client.put(f"/api/v1/strategies/{strategy_id}/status", headers=AUTH, json={"status": "DRAFT"})
        assert redrafted.status_code == 200
        assert redrafted.json()["status"] == "DRAFT"

        events = client.get("/api/v1/events").json()
        changes = [event for event in events if event["event_type"] == "STRATEGY_STATUS_CHANGED"]
        assert len(changes) == 2
        assert changes[0]["payload"]["to_status"] == "DRAFT"
        assert changes[1]["payload"]["from_status"] == "DRAFT"


def test_strategy_evaluations_listing() -> None:
    with SessionLocal() as session:
        strategy = _seed_validated_strategy(session, key="eval-history", version="v2")
        strategy_id = strategy.id
        session.add(StrategyEvaluation(strategy_version_id=strategy_id, dataset_start=datetime(2026, 8, 1, tzinfo=UTC), dataset_end=datetime(2026, 8, 31, tzinfo=UTC), censor_gap_seconds=60, metrics={"trades": 37, "win_rate": 0.6, "total_return": 0.11, "max_drawdown": 0.017}, accepted=True))
        session.commit()

    with TestClient(app) as client:
        assert client.get("/api/v1/strategies/99999/evaluations").status_code == 404
        response = client.get(f"/api/v1/strategies/{strategy_id}/evaluations")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["strategy_version_id"] == strategy_id
    assert rows[0]["metrics"]["trades"] == 37
    assert rows[0]["accepted"] is True


# ----------------------------------------------------------------- drill-down


def _seed_multi_symbol_plan() -> None:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    plan = [
        (0, "EURUSD", "CALL", 5.0, "WIN"),
        (1, "GBPUSD", "PUT", -3.0, "LOSS"),
        (2, "GBPUSD", "CALL", 8.0, "WIN"),
        (3, "EURUSD", "PUT", -2.0, "LOSS"),
    ]
    for index, (offset, symbol, side, pnl, outcome) in enumerate(plan):
        _seed_trade(
            idempotency_key=f"drill-{index}",
            strategy_id=None,
            symbol=symbol,
            side=side,
            amount=5.0,
            broker_order_id=f"drill-broker-{index}",
            pnl=pnl,
            outcome=outcome,
            settled_at=start + timedelta(minutes=offset),
        )


def test_symbol_drilldown_scopes_every_number_to_the_symbol() -> None:
    _seed_multi_symbol_plan()
    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/symbols/EURUSD")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "EURUSD"
    assert body["total_trades"] == 2
    assert body["wins"] == 1 and body["losses"] == 1
    assert body["net_pnl"] == 3.0
    assert body["win_rate"] == 0.5
    assert body["profit_factor"] == 2.5  # gross 5 / gross loss 2
    # Own-slice equity: 5 -> 3, drawdown 2.0 — never GBP's 3.0 hole.
    assert [point["equity"] for point in body["equity_curve"]] == [5.0, 3.0]
    assert body["max_drawdown"] == 2.0
    sides = {row["group"]: row for row in body["by_side"]}
    assert set(sides) == {"CALL", "PUT"}
    assert sides["CALL"]["net_pnl"] == 5.0
    assert sides["PUT"]["net_pnl"] == -2.0
    strategies = {row["group"]: row for row in body["by_strategy"]}
    assert set(strategies) == {"manual"}
    assert [row["realized_pnl"] for row in body["recent"]] == [-2.0, 5.0]
    # A lowercase query resolves to the same ledger.
    with TestClient(app) as client:
        assert client.get("/api/v1/analytics/symbols/eurusd").json()["symbol"] == "EURUSD"


def test_symbol_drilldown_drawdown_is_isolated_per_symbol() -> None:
    """A symbol's curve never inherits another instrument's hole."""
    _seed_multi_symbol_plan()
    with TestClient(app) as client:
        gbp = client.get("/api/v1/analytics/symbols/GBPUSD").json()
        headline = client.get("/api/v1/analytics/trades").json()
    # GBP's own equity walk is -3 then +5: peak(0) - equity(-3) = 3, then the
    # curve recovers above water, so its own-slice max drawdown is 3.0.
    assert [point["equity"] for point in gbp["equity_curve"]] == [-3.0, 5.0]
    assert gbp["max_drawdown"] == 3.0
    # The headline drawdown over all four trades happens to be 3.0 too, but it
    # is computed over its own curve — each view stands on its own numbers.
    assert headline["max_drawdown"] == 3.0


def test_symbol_drilldown_unknown_symbol_is_404() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/symbols/NOSUCH")
    assert response.status_code == 404
    assert "NOSUCH" in response.json()["detail"]
