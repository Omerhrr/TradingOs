// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
import type { AuditEvent, OrderIntent, PositionSnapshot, ReconciliationRun, ResearchRun, RiskPolicy, StrategyVersion, SystemState, WatchlistItem } from '~/types/trading'

export function useTradingApi() {
  const config = useRuntimeConfig()
  const apiBaseUrl = config.public.apiBaseUrl

  const request = <T>(path: string, options?: Parameters<typeof $fetch<T>>[1]) =>
    $fetch<T>(`${apiBaseUrl}${path}`, { ...options })

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
    pause: () => request<SystemState>('/system/pause', { method: 'POST' }),
  }
}
