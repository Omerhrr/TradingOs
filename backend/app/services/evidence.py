"""Per-strategy evidence bundles and their CSV / PDF exports.

Evidence is the honest, read-only answer to "why does this strategy version
exist and what has it actually done?". One bundle gathers, in one projection:

* identity (key, version, status, definition, created),
* the latest persisted walk-forward evaluation (the only path to VALIDATED),
* the lab provenance — which sweep cell(s) the version was promoted from,
* live practice outcomes settled through the risk-gated pipeline,
* gate activity (intents raised, how far each got).

Exports never invent numbers: CSV and PDF both render the same bundle, and
the bundle is rebuilt from the database on every request. An export is a
report of record, not a screenshot the client could have edited.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderIntent, StrategyEvaluation, StrategyVersion
from app.services.analytics import _settled_rows
from app.services.backtest import serialize_pick_record, strategy_sweep_picks


class StrategyMissing(ValueError):
    """The requested strategy version does not exist; maps to HTTP 404."""


def strategy_evidence(session: Session, strategy_id: int) -> dict[str, Any]:
    """Build the full evidence bundle for one strategy version."""
    strategy = session.get(StrategyVersion, strategy_id)
    if strategy is None:
        raise StrategyMissing(f"Strategy version {strategy_id} does not exist.")

    evaluation = session.scalar(
        select(StrategyEvaluation)
        .where(StrategyEvaluation.strategy_version_id == strategy_id)
        .order_by(StrategyEvaluation.id.desc())
        .limit(1)
    )

    # Live settled outcomes for exactly this strategy version, reusing the one
    # settled-trade projection every other aggregate is built from.
    trades = [row for row in _settled_rows(session) if row["strategy_version_id"] == strategy_id]
    pnls = [row["realized_pnl"] for row in trades]
    wins = sum(1 for row in trades if row["outcome"] == "WIN")
    intents = list(session.scalars(select(OrderIntent).where(OrderIntent.strategy_version_id == strategy_id)))
    intent_states = [intent.status for intent in intents]

    try:
        provenance = [serialize_pick_record(pick) for pick in strategy_sweep_picks(session, strategy_id)]
    except ValueError:  # unreachable here: the strategy exists
        provenance = []

    definition = strategy.definition or {}
    summary = strategy.validation_summary or {}
    return {
        "generated_at": datetime.now(UTC),
        "strategy": {
            "id": strategy.id,
            "strategy_key": strategy.strategy_key,
            "version": strategy.version,
            "status": strategy.status,
            "created_at": strategy.created_at,
        },
        "definition": {
            "kind": definition.get("kind", "ema_cross"),
            "fast_window": definition.get("fast_window"),
            "slow_window": definition.get("slow_window"),
            "volatility_window": definition.get("volatility_window"),
            "max_drawdown_gate": definition.get("max_drawdown"),
            "trade_amount": definition.get("trade_amount"),
            "duration_minutes": definition.get("duration_minutes"),
        },
        "evaluation": {
            "evaluated": evaluation is not None,
            "accepted": evaluation.accepted if evaluation else None,
            "evaluated_at": evaluation.created_at if evaluation else None,
            "dataset_start": evaluation.dataset_start if evaluation else None,
            "dataset_end": evaluation.dataset_end if evaluation else None,
            "censor_gap_seconds": evaluation.censor_gap_seconds if evaluation else None,
            "metrics": evaluation.metrics if evaluation else {},
        },
        "lab_provenance": provenance,
        "origin": summary.get("origin", "strategy_desk") if isinstance(summary, dict) else "strategy_desk",
        "live": {
            "trades": len(trades),
            "wins": wins,
            "losses": sum(1 for row in trades if row["outcome"] == "LOSS"),
            "win_rate": round(wins / len(trades), 6) if trades else None,
            "net_pnl": round(sum(pnls), 6),
            "best_pnl": round(max(pnls), 6) if pnls else None,
            "worst_pnl": round(min(pnls), 6) if pnls else None,
        },
        "activity": {
            "intents": len(intent_states),
            "approved": sum(1 for state in intent_states if state == "APPROVED"),
            "submitted": sum(1 for state in intent_states if state in {"SUBMITTED", "OPEN", "SETTLED", "UNKNOWN"}),
            "rejected": sum(1 for state in intent_states if state == "REJECTED"),
        },
        "trades": [
            {
                "id": row["id"],
                "settled_at": row["settled_at"],
                "symbol": row["symbol"],
                "side": row["side"],
                "amount": row["amount"],
                "outcome": row["outcome"],
                "realized_pnl": row["realized_pnl"],
            }
            for row in trades
        ],
    }


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def evidence_csv(evidence: dict[str, Any]) -> str:
    """Render the evidence bundle as a sectioned CSV report.

    Sections are marked with ``#`` comment lines so the file stays valid CSV
    while remaining readable in a text editor: identity, definition, evaluation
    metrics, lab provenance, gate activity, then the settled-trade ledger.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    strategy = evidence["strategy"]
    writer.writerow(["# TradingOS per-strategy evidence export"])
    writer.writerow(["generated_at", _fmt(evidence["generated_at"])])
    writer.writerow([])
    writer.writerow(["# Identity"])
    writer.writerow(["strategy_id", strategy["id"]])
    writer.writerow(["strategy_key", strategy["strategy_key"]])
    writer.writerow(["version", strategy["version"]])
    writer.writerow(["status", strategy["status"]])
    writer.writerow(["created_at", _fmt(strategy["created_at"])])
    writer.writerow(["origin", evidence["origin"]])
    writer.writerow([])
    writer.writerow(["# Definition"])
    for key, value in evidence["definition"].items():
        writer.writerow([key, _fmt(value)])
    writer.writerow([])
    writer.writerow(["# Latest persisted walk-forward evaluation"])
    evaluation = evidence["evaluation"]
    writer.writerow(["evaluated", _fmt(evaluation["evaluated"])])
    writer.writerow(["accepted", _fmt(evaluation["accepted"])])
    writer.writerow(["evaluated_at", _fmt(evaluation["evaluated_at"])])
    writer.writerow(["dataset_start", _fmt(evaluation["dataset_start"])])
    writer.writerow(["dataset_end", _fmt(evaluation["dataset_end"])])
    writer.writerow(["censor_gap_seconds", _fmt(evaluation["censor_gap_seconds"])])
    for key, value in (evaluation["metrics"] or {}).items():
        writer.writerow([f"metric_{key}", _fmt(value)])
    writer.writerow([])
    writer.writerow(["# Lab provenance (sweep cells this version was promoted from)"])
    writer.writerow(["fast_window", "slow_window", "volatility_window", "symbol", "timeframe_seconds", "censor_gap_seconds", "sweep_run_id", "saved_at", "cell_trades", "cell_win_rate", "cell_total_return"])
    for pick in evidence["lab_provenance"]:
        cell_metrics = pick.get("metrics") or {}
        writer.writerow([
            pick["fast_window"], pick["slow_window"], pick["volatility_window"], pick["symbol"],
            pick["timeframe_seconds"], pick["censor_gap_seconds"], _fmt(pick["sweep_run_id"]), _fmt(pick["created_at"]),
            _fmt(cell_metrics.get("trades")), _fmt(cell_metrics.get("win_rate")), _fmt(cell_metrics.get("total_return")),
        ])
    writer.writerow([])
    writer.writerow(["# Gate activity (order intents and how far each got)"])
    for key, value in evidence["activity"].items():
        writer.writerow([key, _fmt(value)])
    writer.writerow([])
    writer.writerow(["# Live practice outcomes"])
    for key, value in evidence["live"].items():
        writer.writerow([key, _fmt(value)])
    writer.writerow([])
    writer.writerow(["# Settled trade ledger (oldest first)"])
    writer.writerow(["trade_id", "settled_at", "symbol", "side", "amount", "outcome", "realized_pnl"])
    for trade in evidence["trades"]:
        writer.writerow([trade["id"], _fmt(trade["settled_at"]), trade["symbol"], trade["side"], _fmt(trade["amount"]), trade["outcome"], _fmt(trade["realized_pnl"])])
    return buffer.getvalue()


def _latin(text: Any) -> str:
    """PDF core fonts are Latin-1; degrade gracefully instead of crashing."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def evidence_pdf(evidence: dict[str, Any]) -> bytes:
    """Render the evidence bundle as a compact, dependency-light PDF report."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    strategy = evidence["strategy"]
    evaluation = evidence["evaluation"]

    pdf = FPDF(format="A4")
    pdf.set_title(f"TradingOS evidence - {strategy['strategy_key']} v{strategy['version']}")
    pdf.set_author("TradingOS control plane")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()

    # Header band
    pdf.set_fill_color(24, 28, 28)
    pdf.set_text_color(235, 232, 223)
    pdf.rect(x=0, y=0, w=pdf.w, h=26, style="F")
    pdf.set_y(7)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, "TradingOS Evidence Report", align="C")
    pdf.set_y(12)
    pdf.cell(0, 6, _latin(f"{strategy['strategy_key']}  v{strategy['version']}  |  status {strategy['status']}"), align="C")
    pdf.set_y(20)
    pdf.set_font("Helvetica", "", 7)
    pdf.cell(0, 4, _latin(f"generated {datetime.now(UTC).isoformat(timespec='seconds')}Z  |  practice-only control plane"), align="C")
    pdf.set_y(32)
    pdf.set_text_color(20, 20, 20)

    def heading(text: str) -> None:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(20, 60, 55)
        pdf.cell(0, 7, _latin(text.upper()), ln=1)
        pdf.set_draw_color(180, 180, 180)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2)
        pdf.set_text_color(20, 20, 20)

    def kv_table(rows: list[tuple[str, Any]], width: float = 90) -> None:
        pdf.set_font("Helvetica", "", 9)
        for label, value in rows:
            pdf.cell(width, 5.5, _latin(label))
            # Explicit cursor control: after the value block the pen returns to
            # the left margin, so the next label cell can never overflow.
            pdf.multi_cell(0, 5.5, _latin(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    heading("Identity")
    kv_table([
        ("Strategy", f"#{strategy['id']} {strategy['strategy_key']} v{strategy['version']}"),
        ("Status", strategy["status"]),
        ("Created", _fmt(strategy["created_at"])),
        ("Origin", evidence["origin"]),
    ])
    pdf.ln(3)

    heading("Definition")
    kv_table([
        ("Kind", evidence["definition"]["kind"]),
        ("Fast / slow EMA", f"{evidence['definition']['fast_window']} / {evidence['definition']['slow_window']}"),
        ("Volatility window", _fmt(evidence["definition"]["volatility_window"])),
        ("Max drawdown gate", _fmt(evidence["definition"]["max_drawdown_gate"])),
        ("Trade amount", _fmt(evidence["definition"]["trade_amount"]) or "(policy default)"),
        ("Duration (minutes)", _fmt(evidence["definition"]["duration_minutes"]) or "(policy default)"),
    ])
    pdf.ln(3)

    heading("Latest walk-forward evaluation")
    metrics = evaluation.get("metrics") or {}
    if evaluation["evaluated"]:
        kv_table([
            ("Accepted", _fmt(evaluation["accepted"])),
            ("Evaluated at", _fmt(evaluation["evaluated_at"])),
            ("Dataset", f"{_fmt(evaluation['dataset_start'])} .. {_fmt(evaluation['dataset_end'])}"),
            ("Trades / wins", f"{metrics.get('trades', '-')} / {metrics.get('wins', '-')}"),
            ("Win rate", _fmt(metrics.get("win_rate"))),
            ("Total return", _fmt(metrics.get("total_return"))),
            ("Max drawdown", _fmt(metrics.get("max_drawdown"))),
            ("Censor gap (s)", _fmt(evaluation["censor_gap_seconds"])),
        ])
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 5.5, "This version has no persisted evaluation yet; VALIDATED is earned only through evaluation.", ln=1)
    pdf.ln(3)

    heading("Lab provenance")
    if evidence["lab_provenance"]:
        pdf.set_font("Helvetica", "", 9)
        for pick in evidence["lab_provenance"]:
            cell_metrics = pick.get("metrics") or {}
            pdf.multi_cell(0, 5.5, _latin(
                f"cell {pick['fast_window']}/{pick['slow_window']} on {pick['symbol']} {pick['timeframe_seconds']}s"
                f" (censor {pick['censor_gap_seconds']}s)"
                + (f" - sweep run #{pick['sweep_run_id']}" if pick["sweep_run_id"] else "")
                + f" - trades {cell_metrics.get('trades', '-')}, win rate {_fmt(cell_metrics.get('win_rate'))}, return {_fmt(cell_metrics.get('total_return'))}"
                + f" - saved {_fmt(pick['created_at'])}"
            ), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 5.5, "No lab provenance: this version was not promoted from a sweep cell.", ln=1)
    pdf.ln(3)

    heading("Practice outcomes and gate activity")
    live = evidence["live"]
    activity = evidence["activity"]
    kv_table([
        ("Settled trades", f"{live['trades']} (wins {live['wins']}, losses {live['losses']})"),
        ("Win rate", _fmt(live["win_rate"])),
        ("Net PnL", _fmt(live["net_pnl"])),
        ("Best / worst", f"{_fmt(live['best_pnl'])} / {_fmt(live['worst_pnl'])}"),
        ("Intents", f"{activity['intents']} (approved {activity['approved']}, submitted {activity['submitted']}, rejected {activity['rejected']})"),
    ])
    pdf.ln(3)

    heading("Settled trade ledger")
    if evidence["trades"]:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(238, 238, 232)
        for column, title in zip((22, 40, 30, 16, 22, 60), ("ID", "SETTLED AT", "SYMBOL", "SIDE", "AMOUNT", "OUTCOME / PNL"), strict=False):
            pdf.cell(column, 6, title, border=1, fill=True)
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)
        for trade in evidence["trades"]:
            pdf.cell(22, 6, _latin(trade["id"]), border=1)
            pdf.cell(40, 6, _latin(_fmt(trade["settled_at"])), border=1)
            pdf.cell(30, 6, _latin(trade["symbol"]), border=1)
            pdf.cell(16, 6, _latin(trade["side"]), border=1)
            pdf.cell(22, 6, _latin(_fmt(trade["amount"])), border=1)
            pdf.cell(60, 6, _latin(f"{trade['outcome']}  {_fmt(trade['realized_pnl'])}"), border=1)
            pdf.ln()
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 5.5, "No settled practice outcomes yet for this strategy version.", ln=1)

    return bytes(pdf.output())
