"""Loop-panel market view: candles plus the signals the loop produced.

The chart endpoint is a read-only projection. Candles come straight from the
ingested ledger (oldest -> newest). Signal markers are derived from
loop-originated order intents: every accepted loop signal carries a
deterministic idempotency key

    loop:{strategy_id}:{symbol}:{timeframe}:{candle_open_epoch}

so the key itself is the durable record of which candle the signal was
computed on. Signals that the risk gate rejected still exist as intents
(REJECTED) and still appear — the chart shows what the loop saw, not only
what the broker took.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Candle, OrderIntent


def _marker_from_intent(intent: OrderIntent, timeframe_seconds: int) -> dict[str, Any] | None:
    """Rebuild the signal position from a loop idempotency key.

    The key shape is ``loop:{strategy_id}:{symbol}:{timeframe}:{epoch}``; the
    symbol comparison happens at the call site, the timeframe here.
    """
    key = intent.idempotency_key or ""
    if not key.startswith("loop:"):
        return None
    head, _, epoch_part = key.rpartition(":")
    if not head.startswith("loop:") or not epoch_part.isdigit():
        return None
    # head = "loop:{strategy_id}:{symbol}:{timeframe}" -> four parts
    parts = head.split(":")
    if len(parts) != 4 or parts[3] != str(timeframe_seconds):
        return None
    try:
        open_epoch = int(epoch_part)
    except ValueError:  # pragma: no cover - isdigit already guarantees int
        return None
    return {
        "intent_id": intent.id,
        "strategy_version_id": intent.strategy_version_id,
        "symbol": intent.symbol,
        "side": intent.side.upper(),
        "status": intent.status,
        "amount": intent.requested_amount,
        "candle_open_epoch": open_epoch,
        "candle_open_time": datetime.fromtimestamp(open_epoch, tz=UTC),
        "created_at": intent.created_at,
    }


def market_chart(session: Session, symbol: str, timeframe_seconds: int, limit: int = 120) -> dict[str, Any]:
    """Candles (ascending) and loop-signal markers for one symbol/timeframe."""
    limit = max(10, min(int(limit), 500))
    symbol = symbol.upper()
    candles = list(
        session.scalars(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.timeframe_seconds == timeframe_seconds)
            .order_by(Candle.open_time.desc())
            .limit(limit)
        )
    )
    candles.reverse()

    markers: list[dict[str, Any]] = []
    seen: set[int] = set()
    # Newest first so the most recent signals survive the scan window; the
    # result is re-sorted ascending for the chart afterwards.
    intents = list(session.scalars(select(OrderIntent).order_by(OrderIntent.id.desc()).limit(1_000)))
    for intent in intents:
        if intent.symbol != symbol:
            continue
        marker = _marker_from_intent(intent, timeframe_seconds)
        if marker is None:
            continue
        # Defensive dedupe: the idempotency key is unique, but the marker must
        # also be unique per candle when several intents share an epoch.
        if marker["candle_open_epoch"] in seen:
            continue
        seen.add(marker["candle_open_epoch"])
        markers.append(marker)
    markers.sort(key=lambda marker: marker["candle_open_epoch"])

    return {
        "symbol": symbol,
        "timeframe_seconds": timeframe_seconds,
        "candles": [
            {
                "open_time": candle.open_time,
                "open": candle.open_price,
                "high": candle.high_price,
                "low": candle.low_price,
                "close": candle.close_price,
            }
            for candle in candles
        ],
        "markers": markers,
        "generated_at": datetime.now(UTC),
    }
