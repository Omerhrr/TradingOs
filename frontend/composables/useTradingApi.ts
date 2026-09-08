// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
import type { AuditEvent, BrokerConnection, BrokerCredentialInput, LoopRun, LoopStatus, OrderIntent, PositionSnapshot, ReconciliationRun, ResearchRun, RiskPolicy, StrategyDefinition, StrategyEvaluation, StrategyStatusInput, StrategyVersion, SystemState, TradeAnalytics, WatchlistItem } from '~/types/trading'

export function useTradingApi() {
  const config = useRuntimeConfig()
  const apiBaseUrl = config.public.apiBaseUrl

  const request = <T>(path: string, options?: Parameters<typeof $fetch<T>>[1]) =>
    $fetch<T>(`${apiBaseUrl}${path}`, { ...options })

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
    getStrategyEvaluations: (strategyId: number) => request<StrategyEvaluation[]>(`/strategies/${strategyId}/evaluations`),
    createStrategy: (adminToken: string, payload: { strategy_key: string; version: string; definition: StrategyDefinition }) => localControl<StrategyVersion>('/strategies', adminToken, { method: 'POST', body: payload }),
    updateStrategyStatus: (adminToken: string, strategyId: number, payload: StrategyStatusInput) => localControl<StrategyVersion>(`/strategies/${strategyId}/status`, adminToken, { method: 'PUT', body: payload }),
    evaluateStrategy: (adminToken: string, strategyId: number, payload: { symbol: string; timeframe_seconds: number; censor_gap_seconds: number }) => localControl<StrategyEvaluation>(`/strategies/${strategyId}/evaluate`, adminToken, { method: 'POST', body: payload }),
    pause: () => request<SystemState>('/system/pause', { method: 'POST' }),
    storeBrokerCredentials: (adminToken: string, credentials: BrokerCredentialInput) => localControl<BrokerConnection>('/broker/credentials', adminToken, { method: 'POST', body: credentials }),
    connectPracticeBroker: (adminToken: string) => localControl<BrokerConnection>('/broker/connect', adminToken, { method: 'POST' }),
    reconcilePracticeBroker: (adminToken: string) => localControl<ReconciliationRun>('/reconciliation/run', adminToken, { method: 'POST' }),
    runLoopTick: (adminToken: string) => localControl<LoopRun>('/loop/run', adminToken, { method: 'POST' }),
  }
}
