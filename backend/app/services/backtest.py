"""Read-only backtest runner for the strategy lab.

The persisted evaluation path (``POST /strategies/{id}/evaluate``) is the only
way a strategy earns VALIDATED status. The runner in this module is a pure
projection over stored candles: it never mutates strategy state, never writes
an evaluation, learning episode, or audit row, and never touches the broker.

Two operations are offered:

* ``run_backtest`` — one full walk-forward with the per-trade evidence trail
  (equity curve, entry/exit, signal, return) for the UI to render.
* ``run_sweep`` — a bounded fast/slow EMA grid over the same candles so the
  operator can see the parameter surface before committing a strategy version.
  The grid is capped: a single request must never turn the API into a
  compute farm.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Candle
from app.services.strategy import walk_forward_detail

MAX_SWEEP_CELLS = 24
DEFAULT_MAX_DRAWDOWN = 1.0  # sweeps report; nothing here is ever "accepted"


def run_backtest(session: Session, definition: dict, symbol: str, timeframe_seconds: int, censor_gap_seconds: int) -> dict[str, Any]:
    """Run one deterministic walk-forward over stored candles. Writes nothing."""
    candles = list(session.scalars(
        select(Candle)
        .where(Candle.symbol == symbol.upper(), Candle.timeframe_seconds == timeframe_seconds)
        .order_by(Candle.open_time)
    ))
    detail = walk_forward_detail(candles, definition, censor_gap_seconds)
    return {
        "symbol": symbol.upper(),
        "timeframe_seconds": timeframe_seconds,
        "censor_gap_seconds": censor_gap_seconds,
        "params": {
            "fast_window": int(definition.get("fast_window", 12)),
            "slow_window": int(definition.get("slow_window", 26)),
            "volatility_window": int(definition.get("volatility_window", 20)),
            "max_drawdown": definition.get("max_drawdown"),
        },
        "metrics": detail["metrics"],
        "equity_curve": detail["equity_curve"],
        "trades": detail["trades"],
        "generated_at": datetime.now(UTC),
    }


def run_sweep(
    session: Session,
    symbol: str,
    timeframe_seconds: int,
    censor_gap_seconds: int,
    fast_windows: list[int],
    slow_windows: list[int],
    volatility_window: int,
) -> dict[str, Any]:
    """Evaluate a bounded fast/slow EMA grid; invalid pairs are reported, not raised."""
    if len(fast_windows) * len(slow_windows) > MAX_SWEEP_CELLS:
        raise ValueError(f"The sweep grid is capped at {MAX_SWEEP_CELLS} cells; narrow the window ranges.")
    candles = list(session.scalars(
        select(Candle)
        .where(Candle.symbol == symbol.upper(), Candle.timeframe_seconds == timeframe_seconds)
        .order_by(Candle.open_time)
    ))
    cells: list[dict[str, Any]] = []
    for fast in sorted(set(fast_windows)):
        for slow in sorted(set(slow_windows)):
            if fast >= slow:
                cells.append({"fast_window": fast, "slow_window": slow, "metrics": None, "error": "fast_window must be smaller than slow_window."})
                continue
            definition = {
                "kind": "ema_cross",
                "fast_window": fast,
                "slow_window": slow,
                "volatility_window": volatility_window,
                "max_drawdown": DEFAULT_MAX_DRAWDOWN,
            }
            try:
                detail = walk_forward_detail(candles, definition, censor_gap_seconds)
            except ValueError as exc:
                cells.append({"fast_window": fast, "slow_window": slow, "metrics": None, "error": str(exc)})
                continue
            cells.append({"fast_window": fast, "slow_window": slow, "metrics": detail["metrics"], "error": None})
    return {
        "symbol": symbol.upper(),
        "timeframe_seconds": timeframe_seconds,
        "censor_gap_seconds": censor_gap_seconds,
        "volatility_window": volatility_window,
        "cells": cells,
        "generated_at": datetime.now(UTC),
    }
