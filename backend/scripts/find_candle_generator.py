"""Empirically find a deterministic candle generator accepted by evaluate_ema_strategy.

Run: python scripts/find_candle_generator.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from datetime import UTC, datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy import evaluate_ema_strategy


class C:  # minimal candle stand-in
    def __init__(self, open_time, close_price):
        self.open_time = open_time
        self.close_price = close_price
        self.high_price = close_price
        self.low_price = close_price


def generate(n, base, amp, period, drift):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    for i in range(n):
        price = base + amp * math.sin(2 * math.pi * i / period) + drift * i
        out.append(C(t0 + timedelta(minutes=i), round(price, 5)))
    return out


best = []
for n in (60, 90, 120, 150, 200):
    for amp in (0.3, 0.6, 1.0, 1.5):
        for period in (8, 10, 12, 16, 20):
            for drift in (0.0, 0.01, 0.02, 0.05, 0.1):
                candles = generate(n, 100.0, amp, period, drift)
                try:
                    m = evaluate_ema_strategy(candles, {"fast_window": 12, "slow_window": 26, "max_drawdown": 0.05}, 60)
                except ValueError:
                    continue
                if m["trades"] >= 10 and m["total_return"] > 0 and m["max_drawdown"] <= 0.05:
                    best.append((n, amp, period, drift, m["trades"], round(m["total_return"], 4), round(m["max_drawdown"], 4)))

best.sort(key=lambda r: (r[4]), reverse=True)
for row in best[:15]:
    print(row)
print(f"total accepted combos: {len(best)}")
