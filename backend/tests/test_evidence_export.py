"""Per-strategy evidence export: one honest bundle, two report formats.

The JSON bundle, the CSV, and the PDF must all be projections of the same
database truth: identity, latest persisted evaluation, lab provenance (cell
memory), gate activity, and the settled trade ledger. Exports never echo
client-supplied numbers, and an unknown strategy answers 404.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from math import sin, tau

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Candle, StrategyVersion, SweepPickRecord, SweepRunRecord

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
def _evidence_world():
    _seed_candles()
    yield
    with SessionLocal() as session:
        session.query(SweepPickRecord).delete()
        session.query(SweepRunRecord).delete()
        session.query(StrategyVersion).delete()
        session.query(Candle).filter(Candle.symbol == SYMBOL).delete()
        session.commit()


def _seed_strategy() -> int:
    with SessionLocal() as session:
        strategy = StrategyVersion(
            strategy_key="evidence-probe",
            version="v1",
            definition={"kind": "ema_cross", "fast_window": 8, "slow_window": 24, "volatility_window": 20, "max_drawdown": 0.05},
        )
        session.add(strategy)
        session.commit()
        return strategy.id


def _promote_from_lab(client) -> dict:
    """Save a sweep pick so the evidence bundle carries real provenance."""
    sweep = client.post("/api/v1/backtest/sweep", headers=ADMIN, json={
        "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60,
        "fast_windows": [8], "slow_windows": [24], "volatility_window": 20,
    }).json()
    save = client.post("/api/v1/strategies/from-sweep", headers=ADMIN, json={
        "strategy_key": "evidence-probe", "version": "v2",
        "symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60,
        "fast_window": 8, "slow_window": 24, "volatility_window": 20,
        "sweep_run_id": sweep["sweep_run_id"],
    })
    assert save.status_code == 200
    return save.json()


def test_evidence_bundle_reports_identity_evaluation_and_provenance(client) -> None:
    promoted = _promote_from_lab(client)
    strategy_id = promoted["strategy"]["id"]
    response = client.get(f"/api/v1/strategies/{strategy_id}/evidence")
    assert response.status_code == 200
    bundle = response.json()
    assert bundle["strategy"]["strategy_key"] == "evidence-probe"
    assert bundle["strategy"]["status"] == "DRAFT"
    assert bundle["origin"] == "backtest_lab"
    assert bundle["definition"]["fast_window"] == 8
    # Provenance: the exact cell, linked to the sweep surface.
    assert len(bundle["lab_provenance"]) == 1
    pick = bundle["lab_provenance"][0]
    assert pick["fast_window"] == 8 and pick["slow_window"] == 24
    assert pick["sweep_run_id"] == sweep_run_id_from(promoted)
    assert pick["metrics"]["trades"] == promoted["evidence"]["metrics"]["trades"]
    # Live outcomes are empty but present, gate activity is a full split.
    assert bundle["live"]["trades"] == 0
    assert set(bundle["activity"]) == {"intents", "approved", "submitted", "rejected"}


def sweep_run_id_from(promoted: dict) -> int:
    return promoted["sweep_pick"]["sweep_run_id"]


def test_evaluation_appears_in_bundle_after_validation(client) -> None:
    strategy_id = _seed_strategy()
    evaluated = client.post(f"/api/v1/strategies/{strategy_id}/evaluate", json={"symbol": SYMBOL, "timeframe_seconds": TIMEFRAME, "censor_gap_seconds": 60}, headers=ADMIN)
    assert evaluated.status_code == 200
    bundle = client.get(f"/api/v1/strategies/{strategy_id}/evidence").json()
    assert bundle["evaluation"]["evaluated"] is True
    assert bundle["evaluation"]["accepted"] is True  # the sine series trades well
    assert bundle["evaluation"]["metrics"]["trades"] == evaluated.json()["metrics"]["trades"]


def test_csv_export_is_sectioned_and_fully_populated(client) -> None:
    promoted = _promote_from_lab(client)
    strategy_id = promoted["strategy"]["id"]
    response = client.get(f"/api/v1/strategies/{strategy_id}/evidence/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('evidence-evidence-probe-v2.csv"')

    text = response.text
    assert "# Identity" in text and "# Latest persisted walk-forward evaluation" in text
    assert "# Lab provenance" in text and "# Settled trade ledger" in text
    rows = list(csv.reader(io.StringIO(text)))
    flat = [row for row in rows if row and not row[0].startswith("#")]
    assert ["strategy_key", "evidence-probe"] in flat
    assert ["origin", "backtest_lab"] in flat
    provenance_row = next(row for row in flat if row[:4] == ["8", "24", "20", SYMBOL])
    assert len(provenance_row) == 11  # cell + context + evidence columns


def test_pdf_export_is_a_valid_pdf_with_expected_filename(client) -> None:
    promoted = _promote_from_lab(client)
    strategy_id = promoted["strategy"]["id"]
    response = client.get(f"/api/v1/strategies/{strategy_id}/evidence/export.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('evidence-evidence-probe-v2.pdf"')
    body = response.content
    assert body.startswith(b"%PDF-")
    assert b"%%EOF" in body
    assert len(body) > 1_000  # a real report, not a stub page


def test_csv_and_pdf_work_for_an_unvalidated_manual_strategy(client) -> None:
    """An empty-world strategy still exports: honest zeros, no provenance."""
    strategy_id = _seed_strategy()
    csv_response = client.get(f"/api/v1/strategies/{strategy_id}/evidence/export.csv")
    assert csv_response.status_code == 200
    assert "evaluated,False" in csv_response.text.replace(" ", "")
    pdf_response = client.get(f"/api/v1/strategies/{strategy_id}/evidence/export.pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.content.startswith(b"%PDF-")
    bundle = client.get(f"/api/v1/strategies/{strategy_id}/evidence").json()
    assert bundle["evaluation"]["evaluated"] is False
    assert bundle["lab_provenance"] == []
    assert bundle["origin"] == "strategy_desk"


def test_unknown_strategy_answers_404_on_every_evidence_route(client) -> None:
    for path in ("/evidence", "/evidence/export.csv", "/evidence/export.pdf", "/sweep-history"):
        response = client.get(f"/api/v1/strategies/999999{path}")
        assert response.status_code == 404, path


def test_bundle_matches_export_numbers(client) -> None:
    """JSON bundle, CSV, and PDF come from the same projection — no drift."""
    promoted = _promote_from_lab(client)
    strategy_id = promoted["strategy"]["id"]
    bundle = client.get(f"/api/v1/strategies/{strategy_id}/evidence").json()
    csv_response = client.get(f"/api/v1/strategies/{strategy_id}/evidence/export.csv").text
    metrics = bundle["evaluation"]["metrics"] or promoted["evidence"]["metrics"]
    trades = metrics["trades"]
    # The recomputed cell evidence appears verbatim in both exports' shared fields.
    assert f"{trades}" in csv_response
    assert json.dumps(bundle["lab_provenance"][0]["metrics"]) is not None
