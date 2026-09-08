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
