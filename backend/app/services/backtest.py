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

from app.models import AuditEvent, Candle, StrategyVersion, SweepPickRecord, SweepRunRecord
from app.services.strategy import walk_forward_detail

MAX_SWEEP_CELLS = 24
MAX_SWEEP_RUN_RECORDS = 24  # bounded memory: the lab keeps the newest surfaces
DEFAULT_MAX_DRAWDOWN = 1.0  # sweeps report; nothing here is ever "accepted"
DRAFT_MAX_DRAWDOWN = 0.05  # a saved pick starts life as a draft with the standard gate


class SweepPickRejected(ValueError):
    """A sweep pick cannot become a draft strategy; carries the HTTP status."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


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


def record_sweep_run(session: Session, sweep_result: dict[str, Any]) -> SweepRunRecord:
    """Persist one lab sweep surface so past parameter grids can be re-opened.

    Called by the API route AFTER ``run_sweep`` returns: the runner service
    itself remains a pure projection (its zero-persistence contract is tested),
    while the operator's lab activity gets a bounded, replayable history.
    """
    record = SweepRunRecord(
        symbol=sweep_result["symbol"],
        timeframe_seconds=sweep_result["timeframe_seconds"],
        censor_gap_seconds=sweep_result["censor_gap_seconds"],
        volatility_window=sweep_result["volatility_window"],
        fast_windows=sorted({cell["fast_window"] for cell in sweep_result["cells"]}),
        slow_windows=sorted({cell["slow_window"] for cell in sweep_result["cells"]}),
        cells=sweep_result["cells"],
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    # Prune to a bounded ring: newest surfaces stay, the past ages out.
    stale_ids = list(session.scalars(
        select(SweepRunRecord.id).order_by(SweepRunRecord.id.desc()).offset(MAX_SWEEP_RUN_RECORDS).limit(100)
    ))
    if stale_ids:
        session.query(SweepRunRecord).where(SweepRunRecord.id.in_(stale_ids)).delete(synchronize_session=False)
        session.commit()
    return record


def sweep_run_history(session: Session, limit: int = 20) -> list[SweepRunRecord]:
    """Recent sweep surfaces, newest first, for the lab's history card."""
    return list(session.scalars(select(SweepRunRecord).order_by(SweepRunRecord.id.desc()).limit(limit)))


def strategy_sweep_picks(session: Session, strategy_id: int) -> list[SweepPickRecord]:
    """Cell memory for one strategy: which lab cell(s) it was promoted from."""
    if session.get(StrategyVersion, strategy_id) is None:
        raise ValueError(f"Strategy version {strategy_id} does not exist.")
    return list(session.scalars(select(SweepPickRecord).where(SweepPickRecord.strategy_version_id == strategy_id).order_by(SweepPickRecord.id)))


def serialize_pick_record(pick: SweepPickRecord) -> dict[str, Any]:
    """The wire shape for cell memory, shared by the pick response and evidence bundles."""
    return {
        "id": pick.id,
        "strategy_version_id": pick.strategy_version_id,
        "sweep_run_id": pick.sweep_run_id,
        "symbol": pick.symbol,
        "timeframe_seconds": pick.timeframe_seconds,
        "censor_gap_seconds": pick.censor_gap_seconds,
        "fast_window": pick.fast_window,
        "slow_window": pick.slow_window,
        "volatility_window": pick.volatility_window,
        "metrics": pick.metrics or {},
        "created_at": pick.created_at,
    }


def saved_pick_cells(session: Session, symbol: str, timeframe_seconds: int, censor_gap_seconds: int) -> list[dict[str, Any]]:
    """Saved (fast, slow) cells for one sweep signature, for heatmap markers.

    A cell that already became a draft shows up here so the operator cannot
    re-promote the same surface twice without noticing.
    """
    rows = session.execute(
        select(SweepPickRecord, StrategyVersion)
        .join(StrategyVersion, SweepPickRecord.strategy_version_id == StrategyVersion.id)
        .where(SweepPickRecord.symbol == symbol.upper())
        .where(SweepPickRecord.timeframe_seconds == timeframe_seconds)
        .where(SweepPickRecord.censor_gap_seconds == censor_gap_seconds)
        .order_by(SweepPickRecord.id)
    ).all()
    return [
        {
            "fast_window": pick.fast_window,
            "slow_window": pick.slow_window,
            "strategy_version_id": strategy.id,
            "strategy_key": strategy.strategy_key,
            "version": strategy.version,
            "status": strategy.status,
            "saved_at": pick.created_at,
        }
        for pick, strategy in rows
    ]


def save_sweep_pick(
    session: Session,
    *,
    strategy_key: str,
    version: str,
    symbol: str,
    timeframe_seconds: int,
    censor_gap_seconds: int,
    fast_window: int,
    slow_window: int,
    volatility_window: int,
    sweep_run_id: int | None = None,
) -> dict[str, Any]:
    """Promote one sweep pick into a DRAFT strategy version with honest evidence.

    The lab is read-only, so a pick carries no authority of its own: instead of
    trusting metrics echoed back by the client, the service re-runs the same
    walk-forward over the candles stored RIGHT NOW and persists that as the
    draft's ``validation_summary``. If candles moved since the sweep, the
    operator sees the recomputed numbers, not the ones they clicked on. The
    draft starts at the standard 0.05 max-drawdown gate — the sweep's reporting
    sentinel (1.0) must never leak into an acceptance threshold.

    The pick remembers its cell: a ``SweepPickRecord`` pins the exact (fast,
    slow) pair, the sweep context, and the recomputed evidence snapshot, and
    optionally links back to the persisted sweep surface it came from.
    """
    fast_window = int(fast_window)
    slow_window = int(slow_window)
    if fast_window <= 0 or slow_window <= 0 or volatility_window <= 0:
        raise SweepPickRejected("Sweep windows must be positive integers.")
    if fast_window >= slow_window:
        raise SweepPickRejected("The fast EMA window must be smaller than the slow one.")

    duplicate = session.scalar(
        select(StrategyVersion).where(StrategyVersion.strategy_key == strategy_key, StrategyVersion.version == version)
    )
    if duplicate is not None:
        raise SweepPickRejected("A strategy with this key and version already exists.", status_code=409)

    candles = list(session.scalars(
        select(Candle)
        .where(Candle.symbol == symbol.upper(), Candle.timeframe_seconds == timeframe_seconds)
        .order_by(Candle.open_time)
    ))
    definition = {
        "kind": "ema_cross",
        "fast_window": fast_window,
        "slow_window": slow_window,
        "volatility_window": volatility_window,
        "max_drawdown": DRAFT_MAX_DRAWDOWN,
    }
    try:
        detail = walk_forward_detail(candles, definition, censor_gap_seconds)
    except ValueError as exc:
        raise SweepPickRejected(str(exc)) from exc

    sweep_run: SweepRunRecord | None = None
    if sweep_run_id is not None:
        sweep_run = session.get(SweepRunRecord, sweep_run_id)
        if sweep_run is None:
            raise SweepPickRejected("The referenced sweep run does not exist; it may have aged out of the bounded history.")

    metrics = detail["metrics"]
    evidence = {
        "origin": "backtest_lab",
        "symbol": symbol.upper(),
        "timeframe_seconds": timeframe_seconds,
        "censor_gap_seconds": censor_gap_seconds,
        "fast_window": fast_window,
        "slow_window": slow_window,
        "volatility_window": volatility_window,
        "max_drawdown_gate": DRAFT_MAX_DRAWDOWN,
        "metrics": metrics,
        "saved_at": datetime.now(UTC).isoformat(),
    }
    strategy = StrategyVersion(strategy_key=strategy_key, version=version, definition=definition)
    strategy.validation_summary = evidence
    session.add(strategy)
    session.flush()  # the pick record needs the strategy id
    pick_record = SweepPickRecord(
        strategy_version_id=strategy.id,
        sweep_run_id=sweep_run.id if sweep_run is not None else None,
        symbol=symbol.upper(),
        timeframe_seconds=timeframe_seconds,
        censor_gap_seconds=censor_gap_seconds,
        fast_window=fast_window,
        slow_window=slow_window,
        volatility_window=volatility_window,
        metrics=metrics,
    )
    session.add(pick_record)
    session.add(AuditEvent(
        event_type="STRATEGY_DRAFTED_FROM_LAB",
        severity="INFO",
        message=f"Sweep pick {strategy_key} v{version} ({symbol.upper()} {fast_window}/{slow_window}) was saved as a DRAFT strategy with recomputed walk-forward evidence.",
        payload={
            "strategy_key": strategy_key,
            "version": version,
            "symbol": symbol.upper(),
            "timeframe_seconds": timeframe_seconds,
            "fast_window": fast_window,
            "slow_window": slow_window,
            "trades": metrics["trades"],
            "win_rate": metrics["win_rate"],
            "total_return": metrics["total_return"],
            **({"sweep_run_id": sweep_run.id} if sweep_run is not None else {}),
        },
    ))
    session.commit()
    session.refresh(strategy)
    session.refresh(pick_record)
    return {"strategy": strategy, "evidence": evidence, "sweep_pick": pick_record}
