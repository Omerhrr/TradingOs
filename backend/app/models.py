"""SQLAlchemy entities for TradingOS's practice-first control plane."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AccountMode(str, Enum):
    PRACTICE = "PRACTICE"
    REAL = "REAL"


class SystemState(str, Enum):
    PAUSED = "PAUSED"
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"


class StrategyStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    RETIRED = "RETIRED"


class OrderStatus(str, Enum):
    PROPOSED = "PROPOSED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    SETTLED = "SETTLED"
    UNKNOWN = "UNKNOWN"


class AccountConfig(Base):
    __tablename__ = "account_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_label: Mapped[str] = mapped_column(String(120), default="Primary account")
    mode: Mapped[str] = mapped_column(String(16), default=AccountMode.PRACTICE.value)
    real_execution_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    system_state: Mapped[str] = mapped_column(String(16), default=SystemState.PAUSED.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("symbol", "timeframe_seconds", name="uq_watchlist_symbol_timeframe"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    category: Mapped[str] = mapped_column(String(40), default="forex")
    timeframe_seconds: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskPolicy(Base):
    __tablename__ = "risk_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True, default="risk-v1")
    max_risk_fraction: Mapped[float] = mapped_column(Float, default=0.005)
    max_trade_amount: Mapped[float] = mapped_column(Float, default=5.0)
    max_daily_loss_fraction: Mapped[float] = mapped_column(Float, default=0.02)
    max_drawdown_fraction: Mapped[float] = mapped_column(Float, default=0.05)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=1)
    stale_market_seconds: Mapped[int] = mapped_column(Integer, default=90)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default=StrategyStatus.DRAFT.value)
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderIntent(Base):
    __tablename__ = "order_intents"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    strategy_version_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_versions.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    mode: Mapped[str] = mapped_column(String(20), default=AccountMode.PRACTICE.value)
    side: Mapped[str] = mapped_column(String(16))
    requested_amount: Mapped[float] = mapped_column(Float)
    rationale: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PROPOSED.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderRecord(Base):
    __tablename__ = "order_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_intent_id: Mapped[int] = mapped_column(ForeignKey("order_intents.id"), index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    broker_position_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.APPROVED.value)
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    broker_response: Mapped[dict] = mapped_column(JSON, default=dict)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradeOutcome(Base):
    __tablename__ = "trade_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_record_id: Mapped[int] = mapped_column(ForeignKey("order_records.id"), unique=True)
    realized_pnl: Mapped[float] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(20))
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    learning_tags: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EncryptedBrokerCredential(Base):
    """One locally encrypted broker credential set; plaintext never reaches persistence."""

    __tablename__ = "encrypted_broker_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_ciphertext: Mapped[str] = mapped_column(Text)
    password_ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_config_id: Mapped[int] = mapped_column(ForeignKey("account_configs.id"), index=True)
    account_mode: Mapped[str] = mapped_column(String(16))
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    broker_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class MarketAsset(Base):
    __tablename__ = "market_assets"
    __table_args__ = (UniqueConstraint("category", "ticker", name="uq_market_asset_category_ticker"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    ticker: Mapped[str] = mapped_column(String(80), index=True)
    active_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=False)
    payout: Mapped[float | None] = mapped_column(Float, nullable=True)
    precision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule: Mapped[dict | list] = mapped_column(JSON, default=dict)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("symbol", "timeframe_seconds", "open_time", name="uq_candle_symbol_timeframe_open"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(80), index=True)
    timeframe_seconds: Mapped[int] = mapped_column(Integer, index=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    open_price: Mapped[float] = mapped_column(Float)
    high_price: Mapped[float] = mapped_column(Float)
    low_price: Mapped[float] = mapped_column(Float)
    close_price: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="iqair")
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    __table_args__ = (UniqueConstraint("broker_position_id", name="uq_position_snapshot_broker_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_position_id: Mapped[str] = mapped_column(String(120), index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    instrument_type: Mapped[str] = mapped_column(String(48), index=True)
    symbol: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(24), default="RUNNING")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StrategyEvaluation(Base):
    __tablename__ = "strategy_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    dataset_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dataset_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    censor_gap_seconds: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelUsageRecord(Base):
    __tablename__ = "model_usage_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_name: Mapped[str] = mapped_column(String(100), index=True)
    model_name: Mapped[str] = mapped_column(String(100))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    request_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class FeatureSnapshot(Base):
    __tablename__ = "feature_snapshots"
    __table_args__ = (UniqueConstraint("symbol", "timeframe_seconds", "candle_open_time", "feature_version", name="uq_feature_snapshot_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(80), index=True)
    timeframe_seconds: Mapped[int] = mapped_column(Integer, index=True)
    candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    feature_version: Mapped[str] = mapped_column(String(32), default="features-v1")
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LearningEpisode(Base):
    __tablename__ = "learning_episodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_versions.id"), nullable=True, index=True)
    order_record_id: Mapped[int | None] = mapped_column(ForeignKey("order_records.id"), nullable=True, index=True)
    episode_type: Mapped[str] = mapped_column(String(48), index=True)
    conclusion: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIResearchRun(Base):
    __tablename__ = "ai_research_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_versions.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED")
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_digest: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
