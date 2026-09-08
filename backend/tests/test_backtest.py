"""Backtest runner: read-only walk-forward previews and bounded parameter sweeps.

The runner must never mutate state (no evaluations, no audit rows, no feature
snapshots) and must share its math with the persisted evaluation path — both
derive from ``walk_forward_detail``, so a backtest preview and a persisted
evaluation over the same candles and parameters produce identical metrics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import sin, tau

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Candle, FeatureSnapshot, LearningEpisode, StrategyEvaluation, StrategyVersion

ADMIN = {"X-TradingOS-Token": "test-local-admin-token"}
SYMBOL = "EURBTCTST"
TIMEFRAME = 60


def _seed_candles(count: int = 240, base: float = 1.10, drift: float = 0.0004, amplitude: float = 0.004) -> None:
    """Deterministic sine-plus-drift series the EMA cross can actually trade."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with SessionLocal() as session:
        for index in range(count):
            price = base + drift * index + amplitude * sin(tau * index / 40)
            session.add(Candle(
                symbol=SYMBOL,
                timeframe_seconds=TIMEFRAME,
                open_time=start + timedelta(seconds=TIMEFRAME * index),
                close_time=start + timedelta(seconds=TIMEFRAME * (index + 1)),
                open_price=price,
                high_price=price * 1.0005,
                low_price=price * 0.9995,
                close_price=price,
                volume=1000.0,
            ))
        session.commit()


def _seed_strategy(definition: dict | None = None) -> int:
    with SessionLocal() as session:
        strategy = StrategyVersion(strategy_key="backtest-probe", version="v1", definition=definition or {"kind": "ema_cross", "fast_window": 8, "slow_window": 24, "volatility_window": 20, "max_drawdown": 0.05})
        session.add(strategy)
        session.commit()
        return strategy.id


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _market_world():
    _seed_candles()
    yield
    with SessionLocal() as session:
        session.query(Candle).filter(Candle.symbol == SYMBOL).delete()
        session.query(StrategyVersion).filter(StrategyVersion.strategy_key == "backtest-probe").delete()
        session.query(StrategyEvaluation).delete()
        session.query(LearningEpisode).delete()
        session.commit()


def test_backtest_matches_persisted_evaluation(client) -> None:
    """The runner and the evaluation path share one walker: metrics must agree."""
    strategy_id = _seed_strategy()
    evaluated = client.post(f"/api/v1/strategies/{strategy_id}/evaluate", json={"symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert evaluated.status_code == 200
    preview = client.post("/api/v1/backtest/run", json={"strategy_version_id": strategy_id, "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert preview.status_code == 200
    body = preview.json()
    assert body["metrics"] == evaluated.json()["metrics"]
    assert body["params"]["fast_window"] == 8
    assert body["equity_curve"][0]["equity"] == 1.0
    assert body["equity_curve"][-1]["equity"] == pytest.approx(1 + body["metrics"]["total_return"], rel=1e-9)
    assert len(body["trades"]) == body["metrics"]["trades"]


def test_backtest_writes_nothing(client) -> None:
    strategy_id = _seed_strategy()
    with SessionLocal() as session:
        before = (
            session.scalar(select(StrategyEvaluation.id).limit(1)),
            session.scalar(select(LearningEpisode.id).limit(1)),
            session.scalar(select(FeatureSnapshot.id).limit(1)),
            len(list(session.scalars(select(AuditEvent.id)))),
        )
    response = client.post("/api/v1/backtest/run", json={"strategy_version_id": strategy_id, "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert response.status_code == 200
    with SessionLocal() as session:
        after = (
            session.scalar(select(StrategyEvaluation.id).limit(1)),
            session.scalar(select(LearningEpisode.id).limit(1)),
            session.scalar(select(FeatureSnapshot.id).limit(1)),
            len(list(session.scalars(select(AuditEvent.id)))),
        )
    assert before == after


def test_backtest_with_inline_definition_needs_no_strategy(client) -> None:
    response = client.post("/api/v1/backtest/run", json={"definition": {"fast_window": 5, "slow_window": 30, "volatility_window": 20}, "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["params"]["fast_window"] == 5
    assert body["metrics"]["method"] == "ema_cross_walk_forward"


def test_backtest_honors_censor_gap(client) -> None:
    """A censor gap wider than the dataset must produce zero trades, not an error."""
    response = client.post("/api/v1/backtest/run", json={"definition": {"fast_window": 5, "slow_window": 30}, "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 86_400}, headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["metrics"]["trades"] == 0


def test_backtest_unknown_strategy_is_404(client) -> None:
    response = client.post("/api/v1/backtest/run", json={"strategy_version_id": 999_999, "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert response.status_code == 404


def test_backtest_rejects_inverted_windows(client) -> None:
    response = client.post("/api/v1/backtest/run", json={"definition": {"fast_window": 30, "slow_window": 10}, "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert response.status_code == 422


def test_backtest_requires_candles(client) -> None:
    response = client.post("/api/v1/backtest/run", json={"definition": {"fast_window": 5, "slow_window": 30}, "symbol": "NOCANDLES", "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert response.status_code == 422


def test_backtest_requires_admin(client) -> None:
    response = client.post("/api/v1/backtest/run", json={"symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60})
    assert response.status_code == 401


def test_sweep_reports_grid_and_skips_invalid_pairs(client) -> None:
    response = client.post("/api/v1/backtest/sweep", json={"symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60, "fast_windows": [4, 30, 8], "slow_windows": [20, 4, 40]}, headers=ADMIN)
    assert response.status_code == 200
    cells = {(cell["fast_window"], cell["slow_window"]): cell for cell in response.json()["cells"]}
    assert (4, 20) in cells and (8, 40) in cells
    assert cells[(4, 20)]["metrics"]["trades"] >= 0
    assert cells[(30, 4)]["metrics"] is None
    assert "fast_window" in cells[(30, 4)]["error"]
    assert cells[(4, 4)]["metrics"] is None
    # Determinism: identical requests produce identical cells.
    second = client.post("/api/v1/backtest/sweep", json={"symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60, "fast_windows": [4, 30, 8], "slow_windows": [20, 4, 40]}, headers=ADMIN)
    repeat_cells = {(cell["fast_window"], cell["slow_window"]): cell for cell in second.json()["cells"]}
    assert repeat_cells[(4, 20)]["metrics"] == cells[(4, 20)]["metrics"]


def test_sweep_caps_grid_size(client) -> None:
    response = client.post("/api/v1/backtest/sweep", json={"symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60, "fast_windows": [4, 6, 8, 10, 12], "slow_windows": [20, 30, 40, 50, 60]}, headers=ADMIN)
    assert response.status_code == 422
    assert "capped" in response.json()["detail"]


# ------------------------------------------------------------- sweep pick save


def _save_pick(client, **overrides) -> dict:
    payload = {
        "strategy_key": "ema-lab-pick",
        "version": "v1",
        "symbol": SYMBOL,
        "timeframe_seconds": TIMEFRAME,
        "censor_gap_seconds": 60,
        "fast_window": 8,
        "slow_window": 34,
        "volatility_window": 20,
    }
    payload.update(overrides)
    return client.post("/api/v1/strategies/from-sweep", json=payload, headers=ADMIN)


def test_save_sweep_pick_creates_draft_with_recomputed_evidence(client) -> None:
    saved = _save_pick(client)
    assert saved.status_code == 200
    body = saved.json()
    strategy, evidence = body["strategy"], body["evidence"]
    assert strategy["status"] == "DRAFT"
    assert strategy["strategy_key"] == "ema-lab-pick"
    # The pick keeps the standard draft gate, never the sweep's reporting sentinel.
    assert strategy["definition"]["max_drawdown"] == 0.05
    assert strategy["definition"]["fast_window"] == 8
    assert strategy["definition"]["slow_window"] == 34
    assert evidence["origin"] == "backtest_lab"
    assert evidence["symbol"] == SYMBOL

    # Evidence is recomputed server-side: it must equal the read-only runner
    # over the same candles, not whatever a client might have echoed.
    preview = client.post("/api/v1/backtest/run", json={"definition": {"fast_window": 8, "slow_window": 34, "volatility_window": 20}, "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert preview.status_code == 200
    assert evidence["metrics"] == preview.json()["metrics"]

    # The draft is a normal strategy: listed, audited, and evaluatable.
    listed = client.get("/api/v1/strategies")
    assert any(row["id"] == strategy["id"] for row in listed.json())
    with SessionLocal() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "STRATEGY_DRAFTED_FROM_LAB").order_by(AuditEvent.id.desc()))
        assert event is not None
        assert event.payload["strategy_key"] == "ema-lab-pick"
        assert event.payload["fast_window"] == 8
    evaluated = client.post(f"/api/v1/strategies/{strategy['id']}/evaluate", json={"symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert evaluated.status_code == 200
    assert evaluated.json()["metrics"]["trades"] == evidence["metrics"]["trades"]


def test_save_sweep_pick_rejects_duplicate_key_version(client) -> None:
    assert _save_pick(client).status_code == 200
    duplicate = _save_pick(client)
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]


def test_save_sweep_pick_rejects_inverted_windows(client) -> None:
    response = _save_pick(client, fast_window=40, slow_window=8)
    assert response.status_code == 422
    assert "smaller" in response.json()["detail"]


def test_save_sweep_pick_needs_candles_for_the_symbol(client) -> None:
    response = _save_pick(client, symbol="NOCANDLES")
    assert response.status_code == 422
    assert "30 candles" in response.json()["detail"]


def test_save_sweep_pick_requires_admin(client) -> None:
    response = client.post("/api/v1/strategies/from-sweep", json={"strategy_key": "ema-lab-pick", "version": "v1", "symbol": SYMBOL, "fast_window": 8, "slow_window": 34})
    assert response.status_code == 401
