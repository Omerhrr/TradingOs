"""Database reconciliation of broker observations. It never submits orders."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountConfig, AccountSnapshot, AuditEvent, Candle, LearningEpisode, MarketAsset, OrderRecord, OrderStatus, PositionSnapshot, ReconciliationRun, TradeOutcome, WatchlistItem
from app.services.broker import BrokerAdapter, BrokerError


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return fallback


class Reconciler:
    """Read-only ingestion and state reconciliation against the connected practice account."""

    def __init__(self, broker: BrokerAdapter, candle_count: int) -> None:
        self.broker = broker
        self.candle_count = candle_count

    def run(self, session: Session) -> ReconciliationRun:
        now = datetime.now(UTC)
        run = ReconciliationRun(state="RUNNING", summary={})
        session.add(run)
        session.flush()
        try:
            account = session.scalar(select(AccountConfig).limit(1))
            if account is None:
                raise BrokerError("Account configuration is missing.")
            broker_account = self.broker.account()
            if broker_account.mode != "PRACTICE":
                raise BrokerError("Broker account is not in PRACTICE mode.")
            session.add(AccountSnapshot(account_config_id=account.id, account_mode=broker_account.mode, balance=broker_account.balance, currency=broker_account.currency, broker_timestamp=now, raw_payload=broker_account.raw_payload))

            assets = self.broker.assets()
            for item in assets:
                existing = session.scalar(select(MarketAsset).where(MarketAsset.category == str(item["category"]), MarketAsset.ticker == str(item["ticker"])))
                if existing is None:
                    existing = MarketAsset(category=str(item["category"]), ticker=str(item["ticker"]))
                    session.add(existing)
                existing.active_id = str(item.get("id")) if item.get("id") is not None else None
                existing.is_open = bool(item.get("is_open", False))
                existing.payout = _as_float(item.get("payout"))
                existing.precision = int(item["precision"]) if item.get("precision") is not None else None
                existing.schedule = item.get("schedule") or []
                existing.raw_payload = item

            watchlist = list(session.scalars(select(WatchlistItem).where(WatchlistItem.enabled.is_(True))))
            candle_total = 0
            for watch in watchlist:
                for raw in self.broker.candles(watch.symbol, watch.timeframe_seconds, self.candle_count):
                    opened = _timestamp(raw.get("from") or raw.get("at"), now)
                    existing = session.scalar(select(Candle).where(Candle.symbol == watch.symbol, Candle.timeframe_seconds == watch.timeframe_seconds, Candle.open_time == opened))
                    if existing is None:
                        existing = Candle(symbol=watch.symbol, timeframe_seconds=watch.timeframe_seconds, open_time=opened, close_time=_timestamp(raw.get("to"), opened), open_price=_as_float(raw.get("open")) or 0.0, high_price=_as_float(raw.get("max") or raw.get("high")) or 0.0, low_price=_as_float(raw.get("min") or raw.get("low")) or 0.0, close_price=_as_float(raw.get("close") or raw.get("close_price")) or 0.0, volume=_as_float(raw.get("volume")), raw_payload=raw)
                        session.add(existing)
                        candle_total += 1

            positions = self.broker.positions()
            for raw in positions:
                position_id = str(raw.get("id") or raw.get("external_id") or raw.get("position_id"))
                if not position_id:
                    continue
                existing = session.scalar(select(PositionSnapshot).where(PositionSnapshot.broker_position_id == position_id))
                if existing is None:
                    existing = PositionSnapshot(broker_position_id=position_id, instrument_type=str(raw.get("instrument_type", "unknown")))
                    session.add(existing)
                raw_event = raw.get("raw_event", {}) or {}
                order_ids = raw_event.get("order_ids", []) or []
                existing.broker_order_id = str(order_ids[0]) if order_ids else str(raw.get("order_id")) if raw.get("order_id") is not None else None
                existing.symbol = raw.get("active") or raw.get("instrument_id") or raw.get("underlying")
                existing.state = str(raw.get("status") or raw.get("state") or "UNKNOWN").upper()
                existing.pnl = _as_float(raw.get("pnl_realized") or raw.get("close_profit") or raw.get("pnl"))
                existing.opened_at = _timestamp(raw.get("open_time") or raw.get("created"), now) if raw.get("open_time") or raw.get("created") else None
                existing.closed_at = _timestamp(raw.get("close_time") or raw.get("closed_at"), now) if raw.get("close_time") or raw.get("closed_at") else None
                existing.raw_payload = raw
                terminal_states = {"CLOSED", "WON", "LOST", "SOLD", "CANCELED", "SETTLED"}
                if existing.state in terminal_states and existing.broker_order_id:
                    order = session.scalar(select(OrderRecord).where(OrderRecord.broker_order_id == existing.broker_order_id).limit(1))
                    if order:
                        order.status = OrderStatus.SETTLED.value
                        order.broker_position_id = existing.broker_position_id
                        order.reconciled_at = now
                        outcome = session.scalar(select(TradeOutcome).where(TradeOutcome.order_record_id == order.id).limit(1))
                        if outcome is None:
                            pnl = existing.pnl or 0.0
                            outcome_name = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
                            session.add(TradeOutcome(order_record_id=order.id, realized_pnl=pnl, outcome=outcome_name, settled_at=existing.closed_at or now, learning_tags={"position_state": existing.state, "symbol": existing.symbol, "instrument_type": existing.instrument_type}))
                            session.add(LearningEpisode(order_record_id=order.id, episode_type="PRACTICE_TRADE_OUTCOME", conclusion=f"Practice trade settled as {outcome_name}.", evidence={"pnl": pnl, "position_state": existing.state, "symbol": existing.symbol}))

            run.state = "SUCCEEDED"
            run.summary = {"assets": len(assets), "watchlist": len(watchlist), "candles_ingested": candle_total, "positions": len(positions)}
            run.finished_at = datetime.now(UTC)
            session.add(AuditEvent(event_type="RECONCILIATION_SUCCEEDED", severity="INFO", message="Practice broker observations reconciled without submitting an order.", payload=run.summary))
            session.commit()
            return run
        except Exception as exc:
            run.state = "FAILED"
            run.error_message = str(exc)
            run.finished_at = datetime.now(UTC)
            session.add(AuditEvent(event_type="RECONCILIATION_FAILED", severity="ERROR", message="Broker reconciliation failed; new exposure remains unavailable.", payload={"error_type": type(exc).__name__}))
            session.commit()
            raise
