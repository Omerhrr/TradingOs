// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
import type { AuditEvent, AuthLogin, AuthSession, BacktestRun, BacktestSweep, BrokerConnection, BrokerCredentialInput, LoopRun, LoopStatus, MarketChart, OrderIntent, PositionSnapshot, ReconciliationRun, ResearchRun, RiskPolicy, StrategyComparison, StrategyDefinition, StrategyEvaluation, StrategyStatusInput, StrategyVersion, SweepPickSave, SymbolDrilldown, SystemState, TotpProvision, TotpStatus, TradeAnalytics, WatchlistItem } from '~/types/trading'

export function useTradingApi() {
  const config = useRuntimeConfig()
  const apiBaseUrl = config.public.apiBaseUrl

  // credentials: 'include' lets the auth session cookie ride along in
  // remote-gated mode; it changes nothing for the local-first flow.
  // A 401 under an active remote gate means "signed out": the auth state is
  // cleared once and the router middleware takes over from there.
  const client = $fetch.create({
    baseURL: apiBaseUrl,
    credentials: 'include',
    onResponseError(context) {
      if (context.response?.status !== 401 || import.meta.server) return
      const auth = useAuth()
      if (auth.session.value?.remote_access) auth.handleUnauthorized()
    },
  })

  const request = <T>(path: string, options?: Parameters<typeof $fetch<T>>[1]) =>
    client<T>(path, { ...options }) as Promise<T>

  const localControl = <T>(path: string, adminToken: string, options?: Parameters<typeof $fetch<T>>[1]) =>
    request<T>(path, {
      ...options,
      headers: {
        ...options?.headers,
        'X-TradingOS-Token': adminToken,
      },
    })

  return {
    getState: () => request<SystemState>('/state'),
    getRisk: () => request<RiskPolicy>('/risk'),
    getEvents: () => request<AuditEvent[]>('/events'),
    getWatchlist: () => request<WatchlistItem[]>('/watchlist'),
    getStrategies: () => request<StrategyVersion[]>('/strategies'),
    getOrderIntents: () => request<OrderIntent[]>('/order-intents'),
    getPositions: () => request<PositionSnapshot[]>('/positions'),
    getReconciliationRuns: () => request<ReconciliationRun[]>('/reconciliation'),
    getResearchRuns: () => request<ResearchRun[]>('/research/runs'),
    getLoopStatus: () => request<LoopStatus>('/loop/status'),
    getLoopRuns: () => request<LoopRun[]>('/loop/runs'),
    getTradeAnalytics: () => request<TradeAnalytics>('/analytics/trades'),
    getSymbolDrilldown: (symbol: string) => request<SymbolDrilldown>(`/analytics/symbols/${encodeURIComponent(symbol)}`),
    getStrategyComparison: () => request<StrategyComparison>('/analytics/strategies/compare'),
    getMarketChart: (symbol: string, timeframeSeconds: number, limit = 120) =>
      request<MarketChart>(`/market/chart?symbol=${encodeURIComponent(symbol)}&timeframe_seconds=${timeframeSeconds}&limit=${limit}`),
    getStrategyEvaluations: (strategyId: number) => request<StrategyEvaluation[]>(`/strategies/${strategyId}/evaluations`),
    runBacktest: (adminToken: string, payload: { strategy_version_id?: number; definition?: Record<string, unknown>; symbol: string; timeframe_seconds: number; censor_gap_seconds: number }) => localControl<BacktestRun>('/backtest/run', adminToken, { method: 'POST', body: payload }),
    runBacktestSweep: (adminToken: string, payload: { symbol: string; timeframe_seconds: number; censor_gap_seconds: number; fast_windows: number[]; slow_windows: number[]; volatility_window: number }) => localControl<BacktestSweep>('/backtest/sweep', adminToken, { method: 'POST', body: payload }),
    provisionTotp: (adminToken: string) => localControl<TotpProvision>('/auth/totp/provision', adminToken, { method: 'POST' }),
    totpStatus: (adminToken: string) => localControl<TotpStatus>('/auth/totp/status', adminToken),
    disableTotp: (adminToken: string) => localControl<TotpStatus>('/auth/totp/disable', adminToken, { method: 'POST' }),
    createStrategy: (adminToken: string, payload: { strategy_key: string; version: string; definition: StrategyDefinition }) => localControl<StrategyVersion>('/strategies', adminToken, { method: 'POST', body: payload }),
    saveSweepPick: (adminToken: string, payload: { strategy_key: string; version: string; symbol: string; timeframe_seconds: number; censor_gap_seconds: number; fast_window: number; slow_window: number; volatility_window: number }) => localControl<SweepPickSave>('/strategies/from-sweep', adminToken, { method: 'POST', body: payload }),
    updateStrategyStatus: (adminToken: string, strategyId: number, payload: StrategyStatusInput) => localControl<StrategyVersion>(`/strategies/${strategyId}/status`, adminToken, { method: 'PUT', body: payload }),
    evaluateStrategy: (adminToken: string, strategyId: number, payload: { symbol: string; timeframe_seconds: number; censor_gap_seconds: number }) => localControl<StrategyEvaluation>(`/strategies/${strategyId}/evaluate`, adminToken, { method: 'POST', body: payload }),
    pause: () => request<SystemState>('/system/pause', { method: 'POST' }),
    storeBrokerCredentials: (adminToken: string, credentials: BrokerCredentialInput) => localControl<BrokerConnection>('/broker/credentials', adminToken, { method: 'POST', body: credentials }),
    connectPracticeBroker: (adminToken: string) => localControl<BrokerConnection>('/broker/connect', adminToken, { method: 'POST' }),
    reconcilePracticeBroker: (adminToken: string) => localControl<ReconciliationRun>('/reconciliation/run', adminToken, { method: 'POST' }),
    runLoopTick: (adminToken: string) => localControl<LoopRun>('/loop/run', adminToken, { method: 'POST' }),
  }
}
