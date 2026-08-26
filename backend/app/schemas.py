"""Pydantic request and response contracts for TradingOS APIs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    api_version: str
    broker_connection: str
    database: str
    live_execution: bool


class AccountStateResponse(BaseModel):
    account_label: str
    account_mode: str
    system_state: str
    real_execution_enabled: bool
    broker_connection: str
    open_orders: int
    active_watchlist_items: int
    active_risk_policy: str | None


class WatchlistItemInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._/-]+$")
    category: str = Field(default="forex", min_length=1, max_length=40)
    timeframe_seconds: int = Field(default=60, ge=1, le=86_400)
    enabled: bool = True


class WatchlistItemResponse(WatchlistItemInput):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime


class WatchlistUpdate(BaseModel):
    items: list[WatchlistItemInput] = Field(max_length=50)


class RiskPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: str
    max_risk_fraction: float
    max_trade_amount: float
    max_daily_loss_fraction: float
    max_drawdown_fraction: float
    max_open_positions: int
    stale_market_seconds: int
    active: bool


class StrategyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    strategy_key: str
    version: str
    status: str
    definition: dict
    validation_summary: dict
    created_at: datetime


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    broker_order_id: str | None
    broker_position_id: str | None
    status: str
    order_intent_id: int
    created_at: datetime


class TradeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_record_id: int
    realized_pnl: float
    outcome: str
    settled_at: datetime
    learning_tags: dict


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_type: str
    severity: str
    message: str
    payload: dict
    created_at: datetime


class BrokerCredentialInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=512)


class BrokerConnectionResponse(BaseModel):
    state: str
    detail: str
    account_mode: str | None = None
    balance: float | None = None
    currency: str | None = None


class MarketAssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category: str
    ticker: str
    active_id: str | None
    is_open: bool
    payout: float | None
    precision: int | None
    last_seen_at: datetime


class CandleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    symbol: str
    timeframe_seconds: int
    open_time: datetime
    close_time: datetime | None
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float | None


class PositionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    broker_position_id: str
    broker_order_id: str | None
    instrument_type: str
    symbol: str | None
    state: str
    pnl: float | None
    opened_at: datetime | None
    closed_at: datetime | None
    observed_at: datetime


class ReconciliationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    state: str
    error_message: str | None
    summary: dict
    started_at: datetime
    finished_at: datetime | None


class StrategyCreateInput(BaseModel):
    strategy_key: str = Field(min_length=3, max_length=100, pattern=r"^[a-z0-9_-]+$")
    version: str = Field(min_length=1, max_length=32)
    definition: dict = Field(default_factory=lambda: {"kind": "ema_cross", "fast_window": 12, "slow_window": 26, "volatility_window": 20, "max_drawdown": 0.05})


class StrategyEvaluationInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)
    timeframe_seconds: int = Field(default=60, ge=1, le=86_400)
    censor_gap_seconds: int = Field(default=60, ge=1, le=86_400)


class StrategyEvaluationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    strategy_version_id: int
    dataset_start: datetime
    dataset_end: datetime
    censor_gap_seconds: int
    metrics: dict
    accepted: bool
    created_at: datetime


class FeatureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    symbol: str
    timeframe_seconds: int
    candle_open_time: datetime
    feature_version: str
    values: dict


class ResearchRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    strategy_version_id: int | None
    status: str
    model_name: str | None
    input_digest: dict
    output: dict
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class OrderIntentInput(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)
    strategy_version_id: int = Field(ge=1)
    symbol: str = Field(min_length=1, max_length=80)
    side: str = Field(pattern=r"^(CALL|PUT|call|put)$")
    amount: float = Field(gt=0, le=10_000)
    timeframe_seconds: int = Field(default=60, ge=1, le=86_400)
    duration_minutes: int = Field(default=1, ge=1, le=60)


class OrderIntentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    idempotency_key: str
    strategy_version_id: int | None
    symbol: str
    mode: str
    side: str
    requested_amount: float
    rationale: dict
    status: str
    created_at: datetime
