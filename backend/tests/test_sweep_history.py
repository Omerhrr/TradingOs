"""Sweep-parameter history: surfaces are remembered, picks remember their cell.

The runner service stays a pure projection (zero persistence, tested in
test_backtest); the API route records the surface afterwards. A saved pick
pins the exact (fast, slow) cell it was promoted from, optionally linking the
sweep surface it came from, and the heatmap can mark cells that already became
drafts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import sin, tau

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Candle, StrategyVersion, SweepPickRecord, SweepRunRecord

ADMIN = {"X-TradingOS-Token": "test-local-admin-token"}
SYMBOL = "EURBTCTST"
TIMEFRAME = 60


def _seed_candles(count: int = 240) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with SessionLocal() as session:
        for index in range(count):
            price = 1.10 + 0.0004 * index + 0.004 * sin(tau * index / 40)
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


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _lab_world():
    _seed_candles()
    yield
    with SessionLocal() as session:
        session.query(SweepPickRecord).delete()
        session.query(SweepRunRecord).delete()
        session.query(AuditEvent).filter(AuditEvent.event_type == "STRATEGY_DRAFTED_FROM_LAB").delete()
        session.query(StrategyVersion).delete()
        session.query(Candle).filter(Candle.symbol == SYMBOL).delete()
        session.commit()


def _run_sweep(client) -> dict:
    response = client.post("/api/v1/backtest/sweep", headers=ADMIN, json={
        "symbol": SYMBOL,
        "timeframe_seconds": TIMEFRAME,
        "censor_gap_seconds": 60,
        "fast_windows": [4, 8],
        "slow_windows": [24, 34],
        "volatility_window": 20,
    })
    assert response.status_code == 200
    return response.json()


def test_sweep_route_remembers_the_surface(client) -> None:
    """A sweep API call records a replayable surface; the service stays pure."""
    body = _run_sweep(client)
    assert body["sweep_run_id"] is not None
    listing = client.get("/api/v1/backtest/sweeps")
    assert listing.status_code == 200
    runs = listing.json()
    assert len(runs) == 1
    run = runs[0]
    assert run["id"] == body["sweep_run_id"]
    assert run["symbol"] == SYMBOL
    assert run["fast_windows"] == [4, 8]
    assert run["slow_windows"] == [24, 34]
    assert len(run["cells"]) == 4
    assert run["cells"][0]["fast_window"] == 4


def test_saved_pick_remembers_its_cell(client) -> None:
    """A promoted pick carries its cell memory, linked to the sweep surface."""
    sweep = _run_sweep(client)
    save = client.post("/api/v1/strategies/from-sweep", headers=ADMIN, json={
        "strategy_key": "ema-cell-memory",
        "version": "v1",
        "symbol": SYMBOL,
        "timeframe_seconds": TIMEFRAME,
        "censor_gap_seconds": 60,
        "fast_window": 8,
        "slow_window": 34,
        "volatility_window": 20,
        "sweep_run_id": sweep["sweep_run_id"],
    })
    assert save.status_code == 200
    body = save.json()
    pick = body["sweep_pick"]
    assert pick is not None
    strategy_id = body["strategy"]["id"]
    assert pick["strategy_version_id"] == strategy_id
    assert pick["sweep_run_id"] == sweep["sweep_run_id"]
    assert pick["fast_window"] == 8 and pick["slow_window"] == 34
    assert pick["metrics"]["trades"] == body["evidence"]["metrics"]["trades"]

    history = client.get(f"/api/v1/strategies/{strategy_id}/sweep-history")
    assert history.status_code == 200
    rows = history.json()
    assert len(rows) == 1
    assert rows[0]["id"] == pick["id"]
    assert rows[0]["symbol"] == SYMBOL


def test_pick_without_sweep_link_still_remembers_its_cell(client) -> None:
    """A save without a sweep run id still pins the cell, just unlinked."""
    save = client.post("/api/v1/strategies/from-sweep", headers=ADMIN, json={
        "strategy_key": "ema-free-cell",
        "version": "v1",
        "symbol": SYMBOL,
        "timeframe_seconds": TIMEFRAME,
        "censor_gap_seconds": 60,
        "fast_window": 4,
        "slow_window": 24,
        "volatility_window": 20,
    })
    assert save.status_code == 200
    pick = save.json()["sweep_pick"]
    assert pick["sweep_run_id"] is None
    assert pick["fast_window"] == 4 and pick["slow_window"] == 24


def test_pick_with_unknown_sweep_run_is_rejected(client) -> None:
    response = client.post("/api/v1/strategies/from-sweep", headers=ADMIN, json={
        "strategy_key": "ema-ghost-sweep",
        "version": "v1",
        "symbol": SYMBOL,
        "timeframe_seconds": TIMEFRAME,
        "censor_gap_seconds": 60,
        "fast_window": 4,
        "slow_window": 24,
        "volatility_window": 20,
        "sweep_run_id": 999_999,
    })
    assert response.status_code == 422
    assert "sweep run" in response.json()["detail"]


def test_saved_cells_are_reported_per_signature(client) -> None:
    """The heatmap marker endpoint reports exactly the saved cells for a surface."""
    _run_sweep(client)
    client.post("/api/v1/strategies/from-sweep", headers=ADMIN, json={
        "strategy_key": "ema-marked-cell",
        "version": "v1",
        "symbol": SYMBOL,
        "timeframe_seconds": TIMEFRAME,
        "censor_gap_seconds": 60,
        "fast_window": 8,
        "slow_window": 24,
        "volatility_window": 20,
    })
    marked = client.get(f"/api/v1/backtest/picks?symbol={SYMBOL}&timeframe_seconds={TIMEFRAME}&censor_gap_seconds=60")
    assert marked.status_code == 200
    cells = marked.json()
    assert len(cells) == 1
    assert cells[0]["fast_window"] == 8 and cells[0]["slow_window"] == 24
    assert cells[0]["strategy_key"] == "ema-marked-cell"
    # A different signature is clean.
    other = client.get(f"/api/v1/backtest/picks?symbol=GBPBTCTST&timeframe_seconds={TIMEFRAME}&censor_gap_seconds=60")
    assert other.json() == []


def test_sweep_history_is_bounded(client) -> None:
    """The surface ring prunes: only the newest MAX_SWEEP_RUN_RECORDS survive."""
    from app.services.backtest import MAX_SWEEP_RUN_RECORDS

    for _ in range(MAX_SWEEP_RUN_RECORDS + 3):
        _run_sweep(client)
    with SessionLocal() as session:
        stored = session.scalar(select(func.count()).select_from(SweepRunRecord))
    assert stored == MAX_SWEEP_RUN_RECORDS
    listing = client.get("/api/v1/backtest/sweeps").json()
    assert listing[0]["id"] > listing[-1]["id"]  # newest first


def test_sweep_history_listing_requires_nothing_and_stays_ordered(client) -> None:
    _run_sweep(client)
    _run_sweep(client)
    runs = client.get("/api/v1/backtest/sweeps").json()
    assert len(runs) == 2
    assert all(run["fast_windows"] == [4, 8] for run in runs)
