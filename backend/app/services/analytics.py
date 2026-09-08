"""Read-only trade-outcome analytics for the UI.

Analytics is a pure projection over settled ``TradeOutcome`` rows joined back
to their order records, intents, and strategy versions. It never mutates
state, never touches the broker, and is safe to call from any request.

Every aggregate is computed from the same ordered trade list so the headline
numbers, the equity curve, and the group breakdowns can never disagree.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderIntent, OrderRecord, StrategyVersion, TradeOutcome


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
