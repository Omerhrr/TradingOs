"""Loop-panel market chart: candles plus loop-signal markers.

The chart is a read-only projection, so these probes pin the two things the
UI relies on: candles come back oldest-first, and markers are derived from
loop-originated intents keyed by their signal candle — never from manual
traffic, never duplicated, never from another timeframe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Candle, OrderIntent

SYMBOL = "EURUSD"
TIMEFRAME = 60


def _seed_candles(count: int = 40, *, start: datetime | None = None) -> list[Candle]:
    Base.metadata.create_all(bind=engine)  # helpers may run before any client lifespan
    start = start or datetime.now(UTC) - timedelta(minutes=count + 5)
    candles = []
    price = 100.0
    with SessionLocal() as session:
        for index in range(count):
            open_time = start + timedelta(minutes=index)
            price += 0.25 if index % 3 else -0.15
            candles.append(Candle(symbol=SYMBOL, timeframe_seconds=TIMEFRAME, open_time=open_time,
                                  close_time=open_time + timedelta(seconds=TIMEFRAME),
                                  open_price=price, high_price=price + 0.4, low_price=price - 0.4,
                                  close_price=price + 0.1))
        session.add_all(candles)
        session.commit()
    return candles


def _seed_loop_intent(*, intent_id: str, side: str = "CALL", epoch: int, status: str = "APPROVED",
                      strategy_id: int | None = 7, timeframe: int = TIMEFRAME) -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        session.add(OrderIntent(idempotency_key=intent_id, strategy_version_id=strategy_id,
                                symbol=SYMBOL, side=side, requested_amount=5.0, status=status,
                                rationale={}, mode="PRACTICE"))
        if timeframe != TIMEFRAME:
            session.add(OrderIntent(idempotency_key=f"loop:9:{SYMBOL}:{timeframe}:{epoch}",
                                    strategy_version_id=9, symbol=SYMBOL, side="PUT",
                                    requested_amount=5.0, status="APPROVED", rationale={}, mode="PRACTICE"))
        session.commit()


def test_chart_returns_ascending_candles_without_markers() -> None:
    _seed_candles(30)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/market/chart?symbol={SYMBOL}&timeframe_seconds={TIMEFRAME}&limit=20")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == SYMBOL
    assert len(body["candles"]) == 20
    opens = [candle["open_time"] for candle in body["candles"]]
    assert opens == sorted(opens), "chart candles must arrive oldest -> newest"
    assert body["markers"] == []


def test_loop_signal_intents_become_unique_markers() -> None:
    _seed_candles(10)
    recent = datetime.now(UTC) - timedelta(minutes=3)
    epoch = int(recent.timestamp())
    # Same signal candle seen by two ticks -> the second intent would be a
    # duplicate; markers must stay one per candle regardless.
    _seed_loop_intent(intent_id=f"loop:7:{SYMBOL}:{TIMEFRAME}:{epoch}", side="CALL", epoch=epoch)
    with SessionLocal() as session:
        session.add(OrderIntent(idempotency_key=f"loop:7:{SYMBOL}:{TIMEFRAME}:{epoch + 60}", strategy_version_id=7,
                                symbol=SYMBOL, side="PUT", requested_amount=5.0, status="APPROVED",
                                rationale={}, mode="PRACTICE"))
        session.commit()
    with TestClient(app) as client:
        response = client.get(f"/api/v1/market/chart?symbol={SYMBOL}&timeframe_seconds={TIMEFRAME}")
    body = response.json()
    assert len(body["markers"]) == 2
    marker = next(m for m in body["markers"] if m["candle_open_epoch"] == epoch)
    assert marker["side"] == "CALL"
    assert marker["status"] == "APPROVED"
    assert marker["strategy_version_id"] == 7
    assert marker["symbol"] == SYMBOL


def test_manual_intents_and_other_timeframes_do_not_leak_into_markers() -> None:
    _seed_candles(10)
    epoch = int((datetime.now(UTC) - timedelta(minutes=3)).timestamp())
    with SessionLocal() as session:
        session.add_all([
            OrderIntent(idempotency_key="manual-abc-123", strategy_version_id=None, symbol=SYMBOL,
                        side="CALL", requested_amount=5.0, status="APPROVED", rationale={}, mode="PRACTICE"),
            OrderIntent(idempotency_key=f"loop:7:OTHER:{TIMEFRAME}:{epoch}", strategy_version_id=7,
                        symbol="OTHER", side="CALL", requested_amount=5.0, status="APPROVED", rationale={}, mode="PRACTICE"),
            OrderIntent(idempotency_key=f"loop:7:{SYMBOL}:300:{epoch}", strategy_version_id=7,
                        symbol=SYMBOL, side="PUT", requested_amount=5.0, status="APPROVED", rationale={}, mode="PRACTICE"),
            OrderIntent(idempotency_key=f"loop:7:{SYMBOL}:{TIMEFRAME}:{epoch}", strategy_version_id=7,
                        symbol=SYMBOL, side="CALL", requested_amount=5.0, status="REJECTED", rationale={}, mode="PRACTICE"),
        ])
        session.commit()
    with TestClient(app) as client:
        response = client.get(f"/api/v1/market/chart?symbol={SYMBOL}&timeframe_seconds={TIMEFRAME}")
    markers = response.json()["markers"]
    assert len(markers) == 1
    assert markers[0]["status"] == "REJECTED", "rejected loop signals still belong on the chart"
    assert markers[0]["side"] == "CALL"


def test_chart_is_symmetric_in_symbol_case() -> None:
    _seed_candles(12)
    with TestClient(app) as client:
        lowered = client.get(f"/api/v1/market/chart?symbol={SYMBOL.lower()}&timeframe_seconds={TIMEFRAME}")
    assert lowered.status_code == 200
    assert lowered.json()["symbol"] == SYMBOL
    assert len(lowered.json()["candles"]) == 12
