// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
export interface SystemState {
  account_label: string
  account_mode: 'PRACTICE' | 'REAL'
  system_state: 'PAUSED' | 'ACTIVE' | 'HALTED'
  real_execution_enabled: boolean
  broker_connection: string
  open_orders: number
  active_watchlist_items: number
  active_risk_policy: string | null
}

export interface RiskPolicy {
  version: string
  max_risk_fraction: number
  max_trade_amount: number
  max_daily_loss_fraction: number
  max_drawdown_fraction: number
  max_open_positions: number
  stale_market_seconds: number
  active: boolean
}

export interface AuditEvent {
  id: number
  event_type: string
  severity: 'INFO' | 'WARNING' | 'ERROR'
  message: string
  payload: Record<string, unknown>
  created_at: string
}

export interface WatchlistItem {
  id: number
  symbol: string
  category: string
  timeframe_seconds: number
  enabled: boolean
  created_at: string
}

export interface StrategyVersion {
  id: number
  strategy_key: string
  version: string
  status: 'DRAFT' | 'VALIDATING' | 'VALIDATED' | 'RETIRED'
  definition: Record<string, unknown>
  validation_summary: Record<string, number | string>
  created_at: string
}

export interface OrderIntent {
  id: number
  idempotency_key: string
  strategy_version_id: number | null
  symbol: string
  mode: string
  side: string
  requested_amount: number
  rationale: Record<string, unknown>
  status: string
  created_at: string
}

export interface ReconciliationRun {
  id: number
  state: string
  error_message: string | null
  summary: Record<string, number>
  started_at: string
  finished_at: string | null
}

export interface LoopSignal {
  strategy_id: number
  symbol: string
  timeframe_seconds: number
  candle_open_time: string
  signal: 'CALL' | 'PUT'
}

export interface LoopIntentOutcome {
  intent_id: number
  status: string
  reason: string
}

export interface LoopRun {
  id: number
  state: 'RUNNING' | 'SUCCEEDED' | 'FAILED'
  error_message: string | null
  summary: {
    skipped?: boolean
    reason?: string
    signals?: LoopSignal[]
    intents_created?: LoopIntentOutcome[]
    intents_submitted?: number
    submit_errors?: Array<{ intent_id: number; error_type: string; detail: string }>
    reconciliation_run_id?: number
    strategies?: number
    watchlist?: number
  }
  started_at: string
  finished_at: string | null
}

export interface LoopStatus {
  loop_enabled: boolean
  practice_execution_enabled: boolean
  broker_connection: string
  system_state: 'PAUSED' | 'ACTIVE' | 'HALTED' | null
  last_run: LoopRun | null
}

export interface PositionSnapshot {
  id: number
  broker_position_id: string
  broker_order_id: string | null
  instrument_type: string
  symbol: string | null
  state: string
  pnl: number | null
  observed_at: string
}

export interface ResearchRun {
  id: number
  strategy_version_id: number | null
  status: string
  model_name: string | null
  output: { disposition?: string; confidence?: number; thesis?: string; risk_flags?: string[] }
  error_message: string | null
  created_at: string
  completed_at: string | null
}

export interface BrokerConnection {
  state: string
  detail: string | null
  account_mode: 'PRACTICE' | 'REAL' | null
  balance: number | null
  currency: string | null
}

export interface BrokerCredentialInput {
  email: string
  password: string
}

export interface StrategyDefinition {
  kind: string
  fast_window: number
  slow_window: number
  volatility_window: number
  max_drawdown: number
  trade_amount?: number
  duration_minutes?: number
  [key: string]: unknown
}

export interface StrategyStatusInput {
  status: 'DRAFT' | 'RETIRED'
}

export interface StrategyEvaluation {
  id: number
  strategy_version_id: number
  dataset_start: string
  dataset_end: string
  censor_gap_seconds: number
  metrics: {
    trades?: number
    wins?: number
    win_rate?: number
    total_return?: number
    max_drawdown?: number
    average_trade_return?: number
    method?: string
    censor_gap_seconds?: number
  }
  accepted: boolean
  created_at: string
}

export interface EquityPoint {
  index: number
  settled_at: string
  pnl: number
  equity: number
}

export interface GroupStats {
  group: string
  trades: number
  wins: number
  losses: number
  net_pnl: number
  win_rate: number
}

export interface TradeAnalyticsRow {
  id: number
  settled_at: string
  symbol: string | null
  side: string | null
  amount: number | null
  strategy_key: string | null
  strategy_version_id: number | null
  realized_pnl: number
  outcome: 'WIN' | 'LOSS' | 'FLAT'
}

export interface TradeAnalytics {
  total_trades: number
  wins: number
  losses: number
  flat: number
  win_rate: number | null
  net_pnl: number
  avg_pnl: number | null
  avg_win: number | null
  avg_loss: number | null
  profit_factor: number | null
  max_drawdown: number
  best_pnl: number | null
  worst_pnl: number | null
  equity_curve: EquityPoint[]
  by_symbol: GroupStats[]
  by_side: GroupStats[]
  by_strategy: GroupStats[]
  recent: TradeAnalyticsRow[]
}

export interface LoopSocketEvent {
  type: string
  ts: string
  payload: Record<string, unknown>
}

export interface ChartCandle {
  open_time: string
  open: number
  high: number
  low: number
  close: number
}

export interface ChartMarker {
  intent_id: number
  strategy_version_id: number | null
  symbol: string
  side: 'CALL' | 'PUT' | string
  status: string
  amount: number
  candle_open_epoch: number
  candle_open_time: string
  created_at: string
}

export interface MarketChart {
  symbol: string
  timeframe_seconds: number
  candles: ChartCandle[]
  markers: ChartMarker[]
  generated_at: string
}

export interface ComparisonParams {
  fast_window: number | null
  slow_window: number | null
  volatility_window: number | null
  trade_amount: number | null
  duration_minutes: number | null
}

export interface ComparisonLive {
  trades: number
  wins: number
  losses: number
  win_rate: number | null
  net_pnl: number
  avg_pnl: number | null
  profit_factor: number | null
  max_drawdown: number
  best_pnl: number | null
  worst_pnl: number | null
}

export interface ComparisonActivity {
  intents: number
  approved: number
  submitted: number
  rejected: number
}

export interface ComparisonEvaluation {
  evaluated: boolean
  accepted: boolean | null
  evaluated_at: string | null
  metrics: {
    trades?: number
    win_rate?: number
    total_return?: number
    max_drawdown?: number
    method?: string
  }
}

export interface StrategyComparisonRow {
  strategy_version_id: number
  strategy_key: string
  version: string
  status: string
  created_at: string
  params: ComparisonParams
  live: ComparisonLive
  activity: ComparisonActivity
  evaluation: ComparisonEvaluation
  equity_curve: Array<{ index: number; settled_at: string; equity: number }>
}

export interface StrategyComparison {
  strategies: StrategyComparisonRow[]
  generated_at: string
}

export interface AuthSession {
  authenticated: boolean
  remote_access: boolean
  expires_at: string | null
  totp_required?: boolean
}

export interface AuthLogin {
  session_token: string
  expires_at: string
  cookie_name: string
}

export interface TotpProvision {
  secret: string
  otpauth_uri: string
  qr_svg: string
}

export interface TotpStatus {
  required: boolean
  provisioned: boolean
}

export interface BacktestMetrics {
  trades: number
  wins: number
  win_rate: number
  total_return: number
  max_drawdown: number
  average_trade_return: number
  method: string
  censor_gap_seconds: number
}

export interface BacktestEquityPoint {
  index: number
  open_time: string
  trade_return: number | null
  equity: number
}

export interface BacktestTradeRow {
  index: number
  decision_time: string
  entry_time: string
  exit_time: string
  signal: 'CALL' | 'PUT' | string
  entry_close: number
  exit_close: number
  trade_return: number
}

export interface BacktestRun {
  symbol: string
  timeframe_seconds: number
  censor_gap_seconds: number
  params: { fast_window: number; slow_window: number; volatility_window: number; max_drawdown: number | null }
  metrics: BacktestMetrics
  equity_curve: BacktestEquityPoint[]
  trades: BacktestTradeRow[]
  generated_at: string
}

export interface BacktestSweepCell {
  fast_window: number
  slow_window: number
  metrics: BacktestMetrics | null
  error: string | null
}

export interface BacktestSweep {
  symbol: string
  timeframe_seconds: number
  censor_gap_seconds: number
  volatility_window: number
  cells: BacktestSweepCell[]
  generated_at: string
  sweep_run_id?: number | null
}

export interface SweepRunRecord {
  id: number
  symbol: string
  timeframe_seconds: number
  censor_gap_seconds: number
  volatility_window: number
  fast_windows: number[]
  slow_windows: number[]
  cells: BacktestSweepCell[]
  created_at: string
}

export interface SweepPickRecord {
  id: number
  strategy_version_id: number
  sweep_run_id: number | null
  symbol: string
  timeframe_seconds: number
  censor_gap_seconds: number
  fast_window: number
  slow_window: number
  volatility_window: number
  metrics: BacktestMetrics
  created_at: string
}

export interface SavedPickCell {
  fast_window: number
  slow_window: number
  strategy_version_id: number
  strategy_key: string
  version: string
  status: string
  saved_at: string
}

export interface EvidenceStrategyIdentity {
  id: number
  strategy_key: string
  version: string
  status: string
  created_at: string
}

export interface StrategyEvidence {
  generated_at: string
  strategy: EvidenceStrategyIdentity
  definition: Record<string, unknown>
  evaluation: Record<string, unknown>
  lab_provenance: SweepPickRecord[]
  origin: string
  live: Record<string, unknown>
  activity: Record<string, unknown>
  trades: Array<Record<string, unknown>>
}

export interface AlertRow {
  id: number
  code: string
  severity: string
  message: string
  payload: Record<string, unknown>
  occurrences: number
  acknowledged: boolean
  acknowledged_at: string | null
  created_at: string
  last_seen_at: string
}

export interface AlertList {
  alerts: AlertRow[]
  unacknowledged: number
}

export interface SweepPickSave {
  strategy: StrategyVersion
  evidence: {
    origin: string
    symbol: string
    timeframe_seconds: number
    censor_gap_seconds: number
    fast_window: number
    slow_window: number
    volatility_window: number
    max_drawdown_gate: number
    metrics: BacktestMetrics
    saved_at: string
  }
  sweep_pick?: SweepPickRecord | null
}

export interface SymbolDrilldown {
  symbol: string
  total_trades: number
  wins: number
  losses: number
  flat: number
  win_rate: number | null
  net_pnl: number
  avg_pnl: number | null
  avg_win: number | null
  avg_loss: number | null
  profit_factor: number | null
  max_drawdown: number
  best_pnl: number | null
  worst_pnl: number | null
  equity_curve: EquityPoint[]
  by_side: GroupStats[]
  by_strategy: GroupStats[]
  recent: TradeAnalyticsRow[]
}
