// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
import type { AIStatus, AlertList, AlertRuleList, AlertRuleUpdateInput, AuditEvent, AuthLogin, AuthSession, BacktestRun, BacktestSweep, BrokerConnection, BrokerCredentialInput, LoopRun, LoopStatus, MarketChart, OrderIntent, PositionSnapshot, ReconciliationRun, ResearchRun, RiskPolicy, SavedPickCell, StrategyComparison, StrategyDefinition, StrategyEvaluation, StrategyStatusInput, StrategyVersion, SweepPickRecord, SweepPickSave, SweepRunRecord, SymbolDrilldown, SystemState, TotpProvision, TotpStatus, TradeAnalytics, WatchlistItem, WebhookDeliveryList, WebhookPolicy } from '~/types/trading'

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
    getSweepRuns: (limit = 20) => request<SweepRunRecord[]>(`/backtest/sweeps?limit=${limit}`),
    getSavedPickCells: (symbol: string, timeframeSeconds: number, censorGapSeconds: number) =>
      request<SavedPickCell[]>(`/backtest/picks?symbol=${encodeURIComponent(symbol)}&timeframe_seconds=${timeframeSeconds}&censor_gap_seconds=${censorGapSeconds}`),
    getStrategySweepHistory: (strategyId: number) => request<SweepPickRecord[]>(`/strategies/${strategyId}/sweep-history`),
    getAlerts: (unacknowledgedOnly = false) => request<AlertList>(`/alerts${unacknowledgedOnly ? '?unacknowledged_only=true' : ''}`),
    getAlertsFiltered: (query: { unacknowledged_only?: boolean; severity?: string; code?: string; limit?: number }) => {
      const params = new URLSearchParams()
      if (query.unacknowledged_only) params.set('unacknowledged_only', 'true')
      if (query.severity) params.set('severity', query.severity)
      if (query.code) params.set('code', query.code)
      if (query.limit) params.set('limit', String(query.limit))
      const qs = params.toString()
      return request<AlertList>(`/alerts${qs ? `?${qs}` : ''}`)
    },
    getAlertRules: () => request<AlertRuleList>('/alerts/rules'),
    updateAlertRule: (adminToken: string, code: string, payload: AlertRuleUpdateInput) => localControl<unknown>(`/alerts/rules/${encodeURIComponent(code)}`, adminToken, { method: 'PUT', body: payload }),
    getWebhookDeliveries: (limit = 30) => request<WebhookDeliveryList>(`/alerts/deliveries?limit=${limit}`),
    retryWebhookDelivery: (adminToken: string, deliveryId: number) => localControl<unknown>(`/alerts/deliveries/${deliveryId}/retry`, adminToken, { method: 'POST' }),
    getWebhookPolicy: () => request<WebhookPolicy>('/alerts/webhook/policy'),
    sendTestWebhook: (adminToken: string) => localControl<{ delivery_id: number; target_url: string; status: string }>('/alerts/webhook/test', adminToken, { method: 'POST' }),
    getAiStatus: (adminToken: string) => localControl<AIStatus>('/ai/status', adminToken),
    ackAlert: (adminToken: string, alertId: number) => localControl<{ acknowledged: number[] }>(`/alerts/${alertId}/ack`, adminToken, { method: 'POST' }),
    ackAllAlerts: (adminToken: string) => localControl<{ acknowledged: number[] }>('/alerts/ack-all', adminToken, { method: 'POST' }),
    // The evidence exports are report-of-record downloads: fetched as a blob
    // with the admin-token header so they work in local AND remote-gated mode
    // (a plain <a href> navigation would drop the header and the strict cookie).
    downloadEvidence: async (adminToken: string, strategyId: number, format: 'csv' | 'pdf'): Promise<Blob> =>
      localControl<Blob>(`/strategies/${strategyId}/evidence/export.${format}`, adminToken, { responseType: 'blob' }),
    provisionTotp: (adminToken: string) => localControl<TotpProvision>('/auth/totp/provision', adminToken, { method: 'POST' }),
    totpStatus: (adminToken: string) => localControl<TotpStatus>('/auth/totp/status', adminToken),
    disableTotp: (adminToken: string) => localControl<TotpStatus>('/auth/totp/disable', adminToken, { method: 'POST' }),
    createStrategy: (adminToken: string, payload: { strategy_key: string; version: string; definition: StrategyDefinition }) => localControl<StrategyVersion>('/strategies', adminToken, { method: 'POST', body: payload }),
    saveSweepPick: (adminToken: string, payload: { strategy_key: string; version: string; symbol: string; timeframe_seconds: number; censor_gap_seconds: number; fast_window: number; slow_window: number; volatility_window: number; sweep_run_id?: number | null }) => localControl<SweepPickSave>('/strategies/from-sweep', adminToken, { method: 'POST', body: payload }),
    updateStrategyStatus: (adminToken: string, strategyId: number, payload: StrategyStatusInput) => localControl<StrategyVersion>(`/strategies/${strategyId}/status`, adminToken, { method: 'PUT', body: payload }),
    evaluateStrategy: (adminToken: string, strategyId: number, payload: { symbol: string; timeframe_seconds: number; censor_gap_seconds: number }) => localControl<StrategyEvaluation>(`/strategies/${strategyId}/evaluate`, adminToken, { method: 'POST', body: payload }),
    pause: () => request<SystemState>('/system/pause', { method: 'POST' }),
    storeBrokerCredentials: (adminToken: string, credentials: BrokerCredentialInput) => localControl<BrokerConnection>('/broker/credentials', adminToken, { method: 'POST', body: credentials }),
    connectPracticeBroker: (adminToken: string) => localControl<BrokerConnection>('/broker/connect', adminToken, { method: 'POST' }),
    reconcilePracticeBroker: (adminToken: string) => localControl<ReconciliationRun>('/reconciliation/run', adminToken, { method: 'POST' }),
    runLoopTick: (adminToken: string) => localControl<LoopRun>('/loop/run', adminToken, { method: 'POST' }),
  }
}
