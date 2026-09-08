"""Read-only trade-outcome analytics for the UI.

Analytics is a pure projection over settled ``TradeOutcome`` rows joined back
to their order records, intents, and strategy versions. It never mutates
state, never touches the broker, and is safe to call from any request.

Every aggregate is computed from the same ordered trade list so the headline
numbers, the equity curve, and the group breakdowns can never disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderIntent, StrategyEvaluation, StrategyVersion, TradeOutcome, OrderRecord


def _group_label(strategy_key: str | None) -> str:
    return strategy_key if strategy_key else "manual"


def _group_stats(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row[key]), []).append(row)

    stats: list[dict[str, Any]] = []
    for label, bucket in buckets.items():
        wins = sum(1 for row in bucket if row["outcome"] == "WIN")
        losses = sum(1 for row in bucket if row["outcome"] == "LOSS")
        stats.append({
            "group": label,
            "trades": len(bucket),
            "wins": wins,
            "losses": losses,
            "net_pnl": round(sum(float(row["realized_pnl"]) for row in bucket), 6),
            "win_rate": round(wins / len(bucket), 6) if bucket else 0.0,
        })
    return sorted(stats, key=lambda stat: (-stat["net_pnl"], stat["group"]))


def trade_analytics(session: Session) -> dict[str, Any]:
    """Build the full analytics payload shown on the outcomes page."""
    joined = session.execute(
        select(TradeOutcome, OrderRecord, OrderIntent, StrategyVersion)
        .join(OrderRecord, TradeOutcome.order_record_id == OrderRecord.id)
        .join(OrderIntent, OrderRecord.order_intent_id == OrderIntent.id)
        .outerjoin(StrategyVersion, OrderIntent.strategy_version_id == StrategyVersion.id)
        .order_by(TradeOutcome.settled_at.asc(), TradeOutcome.id.asc())
    ).all()

    trades: list[dict[str, Any]] = []
    for outcome, record, intent, strategy in joined:
        trades.append({
            "id": outcome.id,
            "settled_at": outcome.settled_at,
            "realized_pnl": float(outcome.realized_pnl),
            "outcome": outcome.outcome,
            "symbol": intent.symbol,
            "side": intent.side,
            "amount": intent.requested_amount,
            "strategy_version_id": intent.strategy_version_id,
            "strategy_key": strategy.strategy_key if strategy is not None else None,
        })

    total = len(trades)
    wins = sum(1 for row in trades if row["outcome"] == "WIN")
    losses = sum(1 for row in trades if row["outcome"] == "LOSS")
    flat = total - wins - losses
    net_pnl = sum(row["realized_pnl"] for row in trades)
    win_pnls = [row["realized_pnl"] for row in trades if row["realized_pnl"] > 0]
    loss_pnls = [row["realized_pnl"] for row in trades if row["realized_pnl"] < 0]
    gross_win = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    equity_curve: list[dict[str, Any]] = []
    for index, row in enumerate(trades, start=1):
        equity += row["realized_pnl"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        equity_curve.append({"index": index, "settled_at": row["settled_at"], "pnl": row["realized_pnl"], "equity": round(equity, 6)})

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 6) if values else None

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate": round(wins / total, 6) if total else None,
        "net_pnl": round(net_pnl, 6),
        "avg_pnl": _mean([row["realized_pnl"] for row in trades]),
        "avg_win": _mean(win_pnls),
        "avg_loss": _mean(loss_pnls),
        # Profit factor: gross profit over gross loss. No losers means the
        # ratio is undefined (infinite); reporting None keeps charts honest.
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss > 0 else None,
        "max_drawdown": round(max_drawdown, 6),
        "best_pnl": round(max(row["realized_pnl"] for row in trades), 6) if trades else None,
        "worst_pnl": round(min(row["realized_pnl"] for row in trades), 6) if trades else None,
        "equity_curve": equity_curve,
        "by_symbol": _group_stats(trades, "symbol"),
        "by_side": _group_stats(trades, "side"),
        "by_strategy": _group_stats([{**row, "strategy_key": _group_label(row["strategy_key"])} for row in trades], "strategy_key"),
        "recent": [
            {
                "id": row["id"],
                "settled_at": row["settled_at"],
                "symbol": row["symbol"],
                "side": row["side"],
                "amount": row["amount"],
                "strategy_key": row["strategy_key"],
                "strategy_version_id": row["strategy_version_id"],
                "realized_pnl": row["realized_pnl"],
                "outcome": row["outcome"],
            }
            for row in reversed(trades[-50:])
        ],
    }


def strategy_comparison(session: Session) -> dict[str, Any]:
    """Side-by-side view of every strategy version.

    Three evidence layers per strategy, all read-only:

    * live practice outcomes (settled trades joined through intents),
    * loop/gate activity (intents raised, how far each got),
    * the latest walk-forward evaluation (the only path to VALIDATED).

    Manual intents (no strategy) are intentionally excluded: the comparison
    exists to rank strategies, and the analytics page already reports the
    manual bucket.
    """
    strategies = list(session.scalars(select(StrategyVersion).order_by(StrategyVersion.id.desc())))

    settled = session.execute(
        select(OrderIntent, TradeOutcome)
        .join(OrderRecord, TradeOutcome.order_record_id == OrderRecord.id)
        .join(OrderIntent, OrderRecord.order_intent_id == OrderIntent.id)
        .where(OrderIntent.strategy_version_id.is_not(None))
        .order_by(TradeOutcome.settled_at.asc(), TradeOutcome.id.asc())
    ).all()

    outcomes_by_strategy: dict[int, list[dict[str, Any]]] = {}
    for intent, outcome in settled:
        if intent.strategy_version_id is None:
            continue
        outcomes_by_strategy.setdefault(intent.strategy_version_id, []).append({
            "realized_pnl": float(outcome.realized_pnl),
            "outcome": outcome.outcome,
        })

    latest_evaluations = {
        evaluation.strategy_version_id: evaluation
        for evaluation in session.scalars(select(StrategyEvaluation).order_by(StrategyEvaluation.id.asc()))
    }

    rows: list[dict[str, Any]] = []
    for strategy in strategies:
        bucket = outcomes_by_strategy.get(strategy.id, [])
        trades = len(bucket)
        wins = sum(1 for row in bucket if row["outcome"] == "WIN")
        losses = sum(1 for row in bucket if row["outcome"] == "LOSS")
        pnls = [row["realized_pnl"] for row in bucket]
        gross_win = sum(pnl for pnl in pnls if pnl > 0)
        gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))

        # Drawdown over this strategy's own equity slice only, so a strategy
        # cannot inherit a hole another one dug.
        equity = peak = max_drawdown = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        intents = list(session.scalars(select(OrderIntent).where(OrderIntent.strategy_version_id == strategy.id)))
        intent_states = [intent.status for intent in intents]

        definition = strategy.definition or {}
        evaluation = latest_evaluations.get(strategy.id)
        rows.append({
            "strategy_version_id": strategy.id,
            "strategy_key": strategy.strategy_key,
            "version": strategy.version,
            "status": strategy.status,
            "created_at": strategy.created_at,
            "params": {
                "fast_window": definition.get("fast_window"),
                "slow_window": definition.get("slow_window"),
                "volatility_window": definition.get("volatility_window"),
                "trade_amount": definition.get("trade_amount"),
                "duration_minutes": definition.get("duration_minutes"),
            },
            "live": {
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / trades, 6) if trades else None,
                "net_pnl": round(sum(pnls), 6),
                "avg_pnl": round(sum(pnls) / trades, 6) if trades else None,
                "profit_factor": round(gross_win / gross_loss, 6) if gross_loss > 0 else None,
                "max_drawdown": round(max_drawdown, 6),
                "best_pnl": round(max(pnls), 6) if pnls else None,
                "worst_pnl": round(min(pnls), 6) if pnls else None,
            },
            "activity": {
                "intents": len(intent_states),
                "approved": sum(1 for state in intent_states if state == "APPROVED"),
                "submitted": sum(1 for state in intent_states if state in {"SUBMITTED", "OPEN", "SETTLED", "UNKNOWN"}),
                "rejected": sum(1 for state in intent_states if state == "REJECTED"),
            },
            "evaluation": {
                "evaluated": evaluation is not None,
                "accepted": evaluation.accepted if evaluation else None,
                "evaluated_at": evaluation.created_at if evaluation else None,
                "metrics": evaluation.metrics if evaluation else {},
            },
        })

    return {"strategies": rows, "generated_at": datetime.now(UTC)}
