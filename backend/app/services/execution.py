"""Persisted, idempotent practice execution. This service never supports REAL mode."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AccountConfig, AccountMode, AccountSnapshot, AuditEvent, Candle, OrderIntent, OrderRecord, OrderStatus, PositionSnapshot, RiskPolicy, StrategyStatus, StrategyVersion, SystemState, TradeOutcome
from app.services.broker import BrokerAdapter, BrokerError
from app.services.risk import RiskDecision, gate_entry


class ExecutionService:
    """Creates an append-only intent before authorizing or submitting a practice order."""

    def __init__(self, broker: BrokerAdapter, settings: Settings) -> None:
        self.broker = broker
        self.settings = settings

    def _risk_inputs(self, session: Session, account: AccountConfig, policy: RiskPolicy, symbol: str, timeframe_seconds: int) -> tuple[bool, int, float, float]:
        now = datetime.now(UTC)
        latest_candle = session.scalar(select(Candle).where(Candle.symbol == symbol, Candle.timeframe_seconds == timeframe_seconds).order_by(Candle.open_time.desc()).limit(1))
        fresh = latest_candle is not None and (now - latest_candle.open_time).total_seconds() <= policy.stale_market_seconds + timeframe_seconds
        open_positions = session.scalar(select(func.count()).select_from(PositionSnapshot).where(PositionSnapshot.state.in_(["OPEN", "PENDING", "ACTIVE"]))) or 0
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_pnl = session.scalar(select(func.coalesce(func.sum(TradeOutcome.realized_pnl), 0.0)).where(TradeOutcome.settled_at >= day_start)) or 0.0
        latest_balance = session.scalar(select(AccountSnapshot).where(AccountSnapshot.account_config_id == account.id).order_by(AccountSnapshot.captured_at.desc()).limit(1))
        peak_balance = session.scalar(select(func.max(AccountSnapshot.balance)).where(AccountSnapshot.account_config_id == account.id))
        balance = latest_balance.balance if latest_balance and latest_balance.balance else 0.0
        daily_loss_fraction = max(0.0, -float(daily_pnl) / balance) if balance else 1.0
        drawdown_fraction = max(0.0, (float(peak_balance) - balance) / float(peak_balance)) if peak_balance and balance else 1.0
        return fresh, int(open_positions), daily_loss_fraction, drawdown_fraction

    def create_intent(self, session: Session, *, idempotency_key: str | None, strategy_id: int, symbol: str, side: str, amount: float, timeframe_seconds: int, duration_minutes: int) -> tuple[OrderIntent, RiskDecision]:
        key = idempotency_key or str(uuid4())
        existing = session.scalar(select(OrderIntent).where(OrderIntent.idempotency_key == key))
        if existing:
            return existing, RiskDecision(existing.status == OrderStatus.APPROVED.value, "Idempotent replay returned the original intent.", existing.requested_amount)
        account = session.scalar(select(AccountConfig).limit(1))
        policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active.is_(True)).order_by(RiskPolicy.id.desc()).limit(1))
        strategy = session.get(StrategyVersion, strategy_id)
        if account is None or policy is None:
            raise RuntimeError("Account or active risk policy is not configured.")
        if strategy is None or strategy.status != StrategyStatus.VALIDATED.value:
            decision = RiskDecision(False, "Only a validated strategy version can create a practice intent.")
        else:
            fresh, open_positions, daily_loss, drawdown = self._risk_inputs(session, account, policy, symbol.upper(), timeframe_seconds)
            decision = gate_entry(account_mode=account.mode, system_state=account.system_state, broker_connected=self.broker.health().state == "CONNECTED", market_is_fresh=fresh, open_positions=open_positions, max_open_positions=policy.max_open_positions, requested_amount=amount, max_trade_amount=policy.max_trade_amount, daily_loss_fraction=daily_loss, max_daily_loss_fraction=policy.max_daily_loss_fraction, drawdown_fraction=drawdown, max_drawdown_fraction=policy.max_drawdown_fraction)
        intent = OrderIntent(idempotency_key=key, strategy_version_id=strategy_id, symbol=symbol.upper(), mode=AccountMode.PRACTICE.value, side=side.upper(), requested_amount=amount, rationale={"timeframe_seconds": timeframe_seconds, "duration_minutes": duration_minutes, "risk_reason": decision.reason}, status=OrderStatus.APPROVED.value if decision.accepted else OrderStatus.REJECTED.value)
        session.add(intent)
        session.add(AuditEvent(event_type="ORDER_INTENT_AUTHORIZED" if decision.accepted else "ORDER_INTENT_REJECTED", severity="INFO" if decision.accepted else "WARNING", message=decision.reason, payload={"idempotency_key": key, "symbol": symbol.upper(), "amount": amount, "duration_minutes": duration_minutes}))
        session.commit()
        session.refresh(intent)
        return intent, decision

    def submit_approved_intent(self, session: Session, intent_id: int) -> OrderRecord:
        if not self.settings.practice_execution_enabled:
            raise BrokerError("Practice execution is disabled in local configuration; no broker request was sent.")
        intent = session.get(OrderIntent, intent_id)
        if intent is None:
            raise ValueError("Order intent does not exist.")
        if intent.mode != AccountMode.PRACTICE.value or intent.status != OrderStatus.APPROVED.value:
            raise BrokerError("Only APPROVED PRACTICE intents may be submitted.")
        existing = session.scalar(select(OrderRecord).where(OrderRecord.order_intent_id == intent.id).limit(1))
        if existing:
            return existing
        duration = int(intent.rationale.get("duration_minutes", 1))
        response = self.broker.submit_practice_option(intent.symbol, intent.side, intent.requested_amount, duration)
        record = OrderRecord(order_intent_id=intent.id, broker_order_id=response["broker_order_id"], status=OrderStatus.SUBMITTED.value, request_payload={"symbol": intent.symbol, "side": intent.side, "amount": intent.requested_amount, "duration_minutes": duration, "mode": "PRACTICE"}, broker_response=response, submitted_at=datetime.now(UTC))
        intent.status = OrderStatus.SUBMITTED.value
        session.add(record)
        session.add(AuditEvent(event_type="PRACTICE_ORDER_SUBMITTED", severity="WARNING", message="A risk-authorized PRACTICE order was submitted to the broker.", payload={"intent_id": intent.id, "broker_order_id": response["broker_order_id"]}))
        session.commit()
        session.refresh(record)
        return record
