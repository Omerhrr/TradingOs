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
