#!/usr/bin/env python3
"""Seed deterministic synthetic candles into the TradingOS DB for UI audits.

The real candle source is broker reconciliation (needs a live practice
connection); audits exercise the strategy desk / backtest lab / evidence
chain without a broker by writing a bounded, reproducible series directly.

Usage: python3 scripts/seed_audit_candles.py [symbol] [timeframe_seconds] [count]
"""
import math
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/home/z/Trading-OS/backend")

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Candle  # noqa: E402

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "EURUSD"
TF = int(sys.argv[2]) if len(sys.argv) > 2 else 60
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 600


def price(t: int) -> float:
    # Deterministic mix of trend + two sine waves -> EMA crosses happen often.
    return (
        1.1000
        + 0.0040 * math.sin(t / 37.0)
        + 0.0015 * math.sin(t / 11.0)
        + 0.0006 * math.cos(t / 5.0)
    )


def main() -> None:
    now_bucket = int(datetime.now(UTC).timestamp()) // TF * TF
    with SessionLocal() as session:
        existing = session.scalar(
            select(Candle).where(Candle.symbol == SYMBOL, Candle.timeframe_seconds == TF)
        )
        if existing is not None:
            print(f"candles for {SYMBOL} {TF}s already present; nothing to do")
            return
        rows = []
        for i in range(COUNT, 0, -1):
            open_epoch = now_bucket - i * TF
            open_time = datetime.fromtimestamp(open_epoch, tz=UTC)
            open_p = price(open_epoch)
            close_p = price(open_epoch + TF)
            high_p = max(open_p, close_p) + 0.0002
            low_p = min(open_p, close_p) - 0.0002
            rows.append(
                Candle(
                    symbol=SYMBOL,
                    timeframe_seconds=TF,
                    open_time=open_time,
                    close_time=datetime.fromtimestamp(open_epoch + TF, tz=UTC),
                    open_price=round(open_p, 6),
                    high_price=round(high_p, 6),
                    low_price=round(low_p, 6),
                    close_price=round(close_p, 6),
                    volume=None,
                    raw_payload={"source": "audit-seed"},
                )
            )
        session.add_all(rows)
        session.commit()
        print(f"seeded {len(rows)} candles for {SYMBOL} {TF}s (last open {now_bucket})")


if __name__ == "__main__":
    main()
