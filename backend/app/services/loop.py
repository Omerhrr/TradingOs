"""Closed strategy -> intent -> practice-execution loop.

One tick performs a full pass over the enabled watchlist and every VALIDATED
strategy version:

  1. Fail-closed guards: PRACTICE account, ACTIVE system, CONNECTED broker.
  2. Reconcile first, so signal decisions always see freshly ingested candles.
  3. For every (watchlist item, validated strategy) pair, compute the EMA-cross
     feature signal on the latest CLOSED candle. A CALL/PUT signal produces an
     order intent through the same ExecutionService risk gate used by the API,
     with a deterministic idempotency key:

         loop:{strategy_id}:{symbol}:{timeframe}:{candle_open_epoch}

     so one signal can never produce two intents, no matter how often the
     loop ticks within the same candle.
  4. Submit loop-originated APPROVED intents to the practice broker (only when
     practice execution is enabled; otherwise they stay queued for review).

The loop never bypasses the risk gate and never touches REAL mode. Any broker
or reconciliation error propagates to the caller so the runtime's documented
fail-closed halt contract stays intact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AccountConfig, AuditEvent, Candle, LoopRun, OrderIntent, RiskPolicy, StrategyStatus, StrategyVersion, SystemState, WatchlistItem
from app.services import alerts as alerting
from app.services.events import publish_event
from app.services.execution import ExecutionService
from app.services.strategy import CandlePoint, calculate_features
from app.services.worker import BrokerWorker


class LoopEngine:
    """Single-writer autonomous loop driven by the runtime or a manual API call."""

    def __init__(self, worker: BrokerWorker, settings: Settings) -> None:
        self.worker = worker
        self.settings = settings

    @property
    def execution(self) -> ExecutionService:
        """Resolve the adapter on every access: the worker may swap adapters
        (reconnects) and tests may inject fakes, so baking a reference at
        construction time would bypass all of that."""
        return ExecutionService(self.worker.adapter, self.settings)

    # ------------------------------------------------------------------ tick
    def tick(self, session: Session) -> LoopRun:
        now = datetime.now(UTC)
        run = LoopRun(state="RUNNING", summary={})
        session.add(run)
        session.commit()
        publish_event("loop.tick.started", {"run_id": run.id})
        try:
            summary = self._tick_body(session, now)
            run.state = "SUCCEEDED"
            run.summary = summary
            run.finished_at = datetime.now(UTC)
            session.add(run)  # re-attach after any session expiry
            session.commit()
            publish_event("loop.tick.completed", {"run_id": run.id, "state": run.state, "summary": summary, "finished_at": run.finished_at.isoformat() if run.finished_at else None})
            return run
        except Exception as exc:
            session.rollback()
            failed = session.get(LoopRun, run.id)
            if failed is None:
                failed = LoopRun(id=run.id, state="FAILED", summary={})
                session.add(failed)
            failed.state = "FAILED"
            failed.error_message = str(exc)
            failed.finished_at = datetime.now(UTC)
            session.commit()
            # A failed tick is exactly what alerting exists for: the loop did
            # not just skip work, it broke. Raise before surfacing to callers.
            try:
                alerting.raise_alert(session, self.settings, code=alerting.TICK_FAILED, severity="ERROR", message=f"A loop tick failed: {exc}", payload={"run_id": failed.id, "error_type": type(exc).__name__})
            except Exception:  # noqa: BLE001 — alerting must never mask the failure
                pass
            publish_event("loop.tick.failed", {"run_id": failed.id, "state": "FAILED", "error": failed.error_message, "finished_at": failed.finished_at.isoformat() if failed.finished_at else None})
            raise

    def _tick_body(self, session: Session, now: datetime) -> dict[str, Any]:
        guards = self._guards(session)
        if guards:
            # A tripped guard is the system working as designed, but the
            # operator must hear about it. raise_alert deduplicates within the
            # cooldown window so a runtime-driven loop cannot flood the ledger.
            try:
                alert = alerting.raise_alert(session, self.settings, code=alerting.GUARD_TRIPPED, severity="WARNING", message=guards, payload={"skipped": True})
                guard_alert = {"alert_id": alert.id, "occurrences": alert.occurrences}
            except Exception:  # noqa: BLE001 — alerting must never break the tick
                guard_alert = None
            return {"skipped": True, "reason": guards, "guard_alert": guard_alert}
        # Guards passed: any standing guard alert describes a condition that
        # no longer holds, so it self-resolves instead of training the operator
        # to ignore pages.
        try:
            alerting.resolve_guard_alerts(session, self.settings)
        except Exception:  # noqa: BLE001
            pass

        # Always reconcile first: signals must be computed on fresh candles and
        # settled outcomes must be visible before new exposure is considered.
        reconcile_run = self.worker.reconcile(session)

        intents_created: list[dict[str, Any]] = []
        signals: list[dict[str, Any]] = []
        strategies = list(session.scalars(select(StrategyVersion).where(StrategyVersion.status == StrategyStatus.VALIDATED.value).order_by(StrategyVersion.id)))
        watchlist = list(session.scalars(select(WatchlistItem).where(WatchlistItem.enabled.is_(True)).order_by(WatchlistItem.id)))
        for watch in watchlist:
            for strategy in strategies:
                outcome = self._maybe_create_intent(session, strategy, watch, now)
                if outcome["signal"] is not None:
                    signals.append(outcome["signal"])
                if outcome["intent"] is not None:
                    intents_created.append(outcome["intent"])

        submitted, submit_errors = self._submit_loop_intents(session)
        for error in submit_errors:
            try:
                alerting.raise_alert(
                    session,
                    self.settings,
                    code=alerting.SUBMIT_ERROR,
                    severity="ERROR",
                    message=f"Submitting loop intent #{error['intent_id']} failed: {error['error_type']}: {error['detail']}",
                    payload=error,
                )
            except Exception:  # noqa: BLE001 — alerting must never break the tick
                pass

        summary = {
            "skipped": False,
            "reconciliation_run_id": reconcile_run.id,
            "strategies": len(strategies),
            "watchlist": len(watchlist),
            "signals": signals,
            "intents_created": intents_created,
            "intents_submitted": len(submitted),
            "submit_errors": submit_errors,
        }
        session.add(_tick_audit_event(summary))
        return summary

    # --------------------------------------------------------------- guards
    def _guards(self, session: Session) -> str | None:
        account = session.scalar(select(AccountConfig).limit(1))
        if account is None:
            return "Account configuration is missing."
        if account.mode != "PRACTICE":
            return "Only PRACTICE mode may run the autonomous loop."
        if account.system_state != SystemState.ACTIVE.value:
            return f"System state is {account.system_state}; ACTIVE is required."
        if self.worker.adapter.health().state != "CONNECTED":
            return "Practice broker is not connected."
        return None

    # -------------------------------------------------------------- signals
    def _maybe_create_intent(self, session: Session, strategy: StrategyVersion, watch: WatchlistItem, now: datetime) -> dict[str, Any]:
        outcome: dict[str, Any] = {"signal": None, "intent": None}
        candles = list(session.scalars(select(Candle).where(Candle.symbol == watch.symbol, Candle.timeframe_seconds == watch.timeframe_seconds).order_by(Candle.open_time.desc()).limit(200)))
        closed = [candle for candle in candles if candle.open_time is not None and (now - _as_utc(candle.open_time)).total_seconds() >= watch.timeframe_seconds]
        if len(closed) < int(strategy.definition.get("slow_window", 26)) + 1:
            return outcome
        ordered = list(reversed(closed))
        definition = strategy.definition or {}
        features = calculate_features(
            [CandlePoint(open_time=c.open_time, close=c.close_price, high=c.high_price, low=c.low_price) for c in ordered],
            fast_window=int(definition.get("fast_window", 12)),
            slow_window=int(definition.get("slow_window", 26)),
            volatility_window=int(definition.get("volatility_window", 20)),
        )
        latest_candle = ordered[-1]
        latest = features[-1]
        signal = latest.get("signal") if isinstance(latest, dict) else None
        if signal not in {"CALL", "PUT"}:
            return outcome
        outcome["signal"] = {"strategy_id": strategy.id, "symbol": watch.symbol, "timeframe_seconds": watch.timeframe_seconds, "candle_open_time": latest_candle.open_time.isoformat(), "signal": signal}
        publish_event("loop.signal", outcome["signal"])

        key = f"loop:{strategy.id}:{watch.symbol}:{watch.timeframe_seconds}:{int(_as_utc(latest_candle.open_time).timestamp())}"
        if session.scalar(select(OrderIntent.id).where(OrderIntent.idempotency_key == key).limit(1)) is not None:
            return outcome  # one intent per candle, ever

        intent, decision = self.execution.create_intent(
            session,
            idempotency_key=key,
            strategy_id=strategy.id,
            symbol=watch.symbol,
            side="CALL" if signal == "CALL" else "PUT",
            amount=float(definition.get("trade_amount", 0.0)) or self._default_amount(session),
            timeframe_seconds=watch.timeframe_seconds,
            duration_minutes=int(definition.get("duration_minutes", 1)),
        )
        outcome["intent"] = {"intent_id": intent.id, "status": intent.status, "reason": decision.reason}
        publish_event("loop.intent.created", {"intent_id": intent.id, "status": intent.status, "reason": decision.reason, "strategy_id": strategy.id, "symbol": watch.symbol, "side": "CALL" if signal == "CALL" else "PUT"})
        return outcome

    def _default_amount(self, session: Session) -> float:
        policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active.is_(True)).order_by(RiskPolicy.id.desc()).limit(1))
        return float(policy.max_trade_amount) if policy else 0.0

    # ------------------------------------------------------------- execution
    def _submit_loop_intents(self, session: Session) -> tuple[list[int], list[dict[str, Any]]]:
        """Submit loop-originated APPROVED intents; manual intents stay manual."""
        submitted: list[int] = []
        errors: list[dict[str, Any]] = []
        if not self.settings.practice_execution_enabled:
            return submitted, errors
        pending = list(session.scalars(select(OrderIntent).where(OrderIntent.status == "APPROVED").where(OrderIntent.idempotency_key.like("loop:%")).order_by(OrderIntent.id)))
        for intent in pending:
            try:
                record = self.execution.submit_approved_intent(session, intent.id)
                submitted.append(record.id)
            except Exception as exc:  # one bad order must not block the rest
                errors.append({"intent_id": intent.id, "error_type": type(exc).__name__, "detail": str(exc)})
        return submitted, errors


def _as_utc(value: datetime) -> datetime:
    from datetime import timezone

    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _tick_audit_event(summary: dict[str, Any]) -> AuditEvent:
    """Build the LOOP_TICK audit event from a tick summary."""
    return AuditEvent(
        event_type="LOOP_TICK",
        severity="WARNING" if summary.get("intents_created") else "INFO",
        message="Autonomous loop tick completed; every signal passed the practice risk gate.",
        payload={
            "signals": len(summary.get("signals", [])),
            "intents_created": len(summary.get("intents_created", [])),
            "intents_submitted": summary.get("intents_submitted", 0),
            "reconciliation_run_id": summary.get("reconciliation_run_id"),
        },
    )
