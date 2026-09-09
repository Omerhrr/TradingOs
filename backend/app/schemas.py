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


class LoopRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    state: str
    error_message: str | None
    summary: dict
    started_at: datetime
    finished_at: datetime | None


class LoopStatusResponse(BaseModel):
    loop_enabled: bool
    practice_execution_enabled: bool
    broker_connection: str
    system_state: str | None
    last_run: LoopRunResponse | None


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


class StrategyStatusUpdateInput(BaseModel):
    """VALIDATED status is earned only through the evaluation endpoint."""

    status: str = Field(pattern=r"^(DRAFT|RETIRED)$")


class TradeAnalyticsRow(BaseModel):
    id: int
    settled_at: datetime
    symbol: str | None
    side: str | None
    amount: float | None
    strategy_key: str | None
    strategy_version_id: int | None
    realized_pnl: float
    outcome: str


class EquityPoint(BaseModel):
    index: int
    settled_at: datetime
    pnl: float
    equity: float


class GroupStats(BaseModel):
    group: str
    trades: int
    wins: int
    losses: int
    net_pnl: float
    win_rate: float


class TradeAnalyticsResponse(BaseModel):
    total_trades: int
    wins: int
    losses: int
    flat: int
    win_rate: float | None
    net_pnl: float
    avg_pnl: float | None
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    max_drawdown: float
    best_pnl: float | None
    worst_pnl: float | None
    equity_curve: list[EquityPoint]
    by_symbol: list[GroupStats]
    by_side: list[GroupStats]
    by_strategy: list[GroupStats]
    recent: list[TradeAnalyticsRow]


class ChartCandle(BaseModel):
    open_time: datetime
    open: float
    high: float
    low: float
    close: float


class ChartMarker(BaseModel):
    intent_id: int
    strategy_version_id: int | None
    symbol: str
    side: str
    status: str
    amount: float
    candle_open_epoch: int
    candle_open_time: datetime
    created_at: datetime


class MarketChartResponse(BaseModel):
    symbol: str
    timeframe_seconds: int
    candles: list[ChartCandle]
    markers: list[ChartMarker]
    generated_at: datetime


class ComparisonParams(BaseModel):
    fast_window: int | None = None
    slow_window: int | None = None
    volatility_window: int | None = None
    trade_amount: float | None = None
    duration_minutes: int | None = None


class ComparisonLive(BaseModel):
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    net_pnl: float
    avg_pnl: float | None
    profit_factor: float | None
    max_drawdown: float
    best_pnl: float | None
    worst_pnl: float | None


class ComparisonActivity(BaseModel):
    intents: int
    approved: int
    submitted: int
    rejected: int


class ComparisonEvaluation(BaseModel):
    evaluated: bool
    accepted: bool | None
    evaluated_at: datetime | None
    metrics: dict


class ComparisonEquityPoint(BaseModel):
    index: int
    settled_at: datetime
    equity: float


class StrategyComparisonRow(BaseModel):
    strategy_version_id: int
    strategy_key: str
    version: str
    status: str
    created_at: datetime
    params: ComparisonParams
    live: ComparisonLive
    activity: ComparisonActivity
    evaluation: ComparisonEvaluation
    equity_curve: list[ComparisonEquityPoint] = []


class StrategyComparisonResponse(BaseModel):
    strategies: list[StrategyComparisonRow]
    generated_at: datetime


class SymbolDrilldownResponse(BaseModel):
    """Per-symbol drill-down: the same settled-evidence projection as the
    headline analytics, scoped to one instrument."""

    symbol: str
    total_trades: int
    wins: int
    losses: int
    flat: int
    win_rate: float | None
    net_pnl: float
    avg_pnl: float | None
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    max_drawdown: float
    best_pnl: float | None
    worst_pnl: float | None
    equity_curve: list[EquityPoint]
    by_side: list[GroupStats]
    by_strategy: list[GroupStats]
    recent: list[TradeAnalyticsRow]


class AuthLoginInput(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    totp_code: str | None = Field(default=None, min_length=1, max_length=12)


class AuthLoginResponse(BaseModel):
    session_token: str
    expires_at: datetime
    cookie_name: str


class AuthSessionResponse(BaseModel):
    authenticated: bool
    remote_access: bool
    expires_at: datetime | None = None
    totp_required: bool = False


class TotpProvisionResponse(BaseModel):
    """The provisioning material is returned exactly once and never persisted in plaintext.

    The QR SVG encodes the otpauth URI — it is equivalent to the secret itself
    and lives only inside this one response.
    """

    secret: str
    otpauth_uri: str
    qr_svg: str


class TotpStatusResponse(BaseModel):
    required: bool
    provisioned: bool


class BacktestParams(BaseModel):
    fast_window: int
    slow_window: int
    volatility_window: int
    max_drawdown: float | None = None


class BacktestMetrics(BaseModel):
    trades: int
    wins: int
    win_rate: float
    total_return: float
    max_drawdown: float
    average_trade_return: float
    method: str
    censor_gap_seconds: int


class BacktestEquityPoint(BaseModel):
    index: int
    open_time: datetime
    trade_return: float | None
    equity: float


class BacktestTradeRow(BaseModel):
    index: int
    decision_time: datetime
    entry_time: datetime
    exit_time: datetime
    signal: str
    entry_close: float
    exit_close: float
    trade_return: float


class BacktestRunInput(BaseModel):
    strategy_version_id: int | None = None
    definition: dict | None = None
    symbol: str = Field(min_length=1, max_length=80)
    timeframe_seconds: int = Field(default=60, ge=1, le=86_400)
    censor_gap_seconds: int = Field(default=60, ge=1, le=86_400)


class BacktestRunResponse(BaseModel):
    symbol: str
    timeframe_seconds: int
    censor_gap_seconds: int
    params: BacktestParams
    metrics: BacktestMetrics
    equity_curve: list[BacktestEquityPoint]
    trades: list[BacktestTradeRow]
    generated_at: datetime


class BacktestSweepInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)
    timeframe_seconds: int = Field(default=60, ge=1, le=86_400)
    censor_gap_seconds: int = Field(default=60, ge=1, le=86_400)
    fast_windows: list[int] = Field(min_length=1, max_length=12)
    slow_windows: list[int] = Field(min_length=1, max_length=12)
    volatility_window: int = Field(default=20, ge=1, le=500)


class BacktestSweepCell(BaseModel):
    fast_window: int
    slow_window: int
    metrics: BacktestMetrics | None = None
    error: str | None = None


class BacktestSweepResponse(BaseModel):
    symbol: str
    timeframe_seconds: int
    censor_gap_seconds: int
    volatility_window: int
    cells: list[BacktestSweepCell]
    generated_at: datetime
    sweep_run_id: int | None = None


class SweepRunRecordResponse(BaseModel):
    """One remembered lab sweep surface: grid, cells, and when it ran."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    timeframe_seconds: int
    censor_gap_seconds: int
    volatility_window: int
    fast_windows: list[int]
    slow_windows: list[int]
    cells: list[BacktestSweepCell]
    created_at: datetime


class SweepPickRecordResponse(BaseModel):
    """Cell memory: the exact lab cell a draft strategy was promoted from."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_version_id: int
    sweep_run_id: int | None = None
    symbol: str
    timeframe_seconds: int
    censor_gap_seconds: int
    fast_window: int
    slow_window: int
    volatility_window: int
    metrics: dict
    created_at: datetime


class SavedPickCellResponse(BaseModel):
    """A saved cell in one sweep signature, for heatmap markers."""

    fast_window: int
    slow_window: int
    strategy_version_id: int
    strategy_key: str
    version: str
    status: str
    saved_at: datetime


class StrategyFromSweepInput(BaseModel):
    """One sweep pick promoted from the lab. Metrics are NOT accepted here:
    the server recomputes the walk-forward over stored candles so persisted
    evidence can never be fabricated by the client."""

    strategy_key: str = Field(min_length=3, max_length=100, pattern=r"^[a-z0-9_-]+$")
    version: str = Field(min_length=1, max_length=32)
    symbol: str = Field(min_length=1, max_length=80)
    timeframe_seconds: int = Field(default=60, ge=1, le=86_400)
    censor_gap_seconds: int = Field(default=60, ge=1, le=86_400)
    fast_window: int = Field(ge=1, le=200)
    slow_window: int = Field(ge=2, le=400)
    volatility_window: int = Field(default=20, ge=1, le=500)
    sweep_run_id: int | None = None


class SweepPickEvidence(BaseModel):
    origin: str
    symbol: str
    timeframe_seconds: int
    censor_gap_seconds: int
    fast_window: int
    slow_window: int
    volatility_window: int
    max_drawdown_gate: float
    metrics: BacktestMetrics
    saved_at: datetime


class SweepPickSaveResponse(BaseModel):
    strategy: StrategyResponse
    evidence: SweepPickEvidence
    sweep_pick: SweepPickRecordResponse | None = None


class StrategyIdentityResponse(BaseModel):
    """Identity block of an evidence bundle (definition lives beside it)."""

    id: int
    strategy_key: str
    version: str
    status: str
    created_at: datetime


class StrategyEvidenceResponse(BaseModel):
    """The honest per-strategy evidence bundle behind the CSV/PDF exports."""

    generated_at: datetime
    strategy: StrategyIdentityResponse
    definition: dict
    evaluation: dict
    lab_provenance: list[SweepPickRecordResponse]
    origin: str
    live: dict
    activity: dict
    trades: list[dict]


class AlertResponse(BaseModel):
    """One operable operational alert with its acknowledgement state."""

    id: int
    code: str
    severity: str
    message: str
    payload: dict
    occurrences: int
    acknowledged: bool
    acknowledged_at: datetime | None = None
    created_at: datetime
    last_seen_at: datetime


class AlertListResponse(BaseModel):
    alerts: list[AlertResponse]
    unacknowledged: int


class AlertUnreadResponse(BaseModel):
    unacknowledged: int


class AlertAckResponse(BaseModel):
    acknowledged: list[int]
    auto: bool = False


class AlertRuleResponse(BaseModel):
    """Operator-configurable rule for one alert code."""

    id: int
    code: str
    enabled: bool
    severity: str
    cooldown_seconds: int | None = None
    notify_webhook: bool
    description: str
    updated_at: datetime | None = None


class AlertRuleListResponse(BaseModel):
    rules: list[AlertRuleResponse]


class AlertRuleUpdate(BaseModel):
    """Partial alert-rule update; omitted fields keep their current value."""

    enabled: bool | None = None
    severity: str | None = Field(default=None, description="INFO, WARNING, or ERROR")
    cooldown_seconds: int | None = Field(default=None, ge=5, le=86_400)
    notify_webhook: bool | None = None


class WebhookDeliveryResponse(BaseModel):
    """One outbound webhook notification with its retry-policy state."""

    id: int
    alert_id: int | None = None
    event: str
    code: str
    target_url: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None = None
    last_http_status: int | None = None
    last_error: str | None = None
    delivered_at: datetime | None = None
    created_at: datetime


class WebhookDeliveryListResponse(BaseModel):
    deliveries: list[WebhookDeliveryResponse]


class WebhookTestResponse(BaseModel):
    delivery_id: int
    target_url: str
    status: str


class WebhookPolicyResponse(BaseModel):
    """The active retry policy plus whether a target and signing key exist."""

    target_configured: bool
    target_url: str | None = None
    max_attempts: int
    backoff_base_seconds: int
    backoff_max_seconds: int
    timeout_seconds: int
    signing_enabled: bool


class AIStatusResponse(BaseModel):
    """LLM readiness probe. Never contains the API key itself."""

    ai_enabled: bool
    model: str
    base_url: str | None = None
    api_key_configured: bool
    ready: bool
    budget: dict
