<!-- Design: The Instrument Room — outcomes are evidence. The page reports what settled, never what was hoped. -->
<script setup lang="ts">
import type { GroupStats, TradeAnalytics } from '~/types/trading'

const api = useTradingApi()
const { isConnected, events } = useLoopSocket()
const analytics = ref<TradeAnalytics | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)

async function loadAnalytics() {
  loading.value = true
  loadError.value = null
  try {
    analytics.value = await api.getTradeAnalytics()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'The outcome ledger could not be read from the local API.'
  } finally {
    loading.value = false
  }
}

const CurveGeometryWidth = 600
const CurveGeometryHeight = 220
const curveGeometry = computed(() => {
  const points = analytics.value?.equity_curve ?? []
  if (points.length < 2) return null
  const pad = 10
  const min = Math.min(0, ...points.map(p => p.equity))
  const max = Math.max(0, ...points.map(p => p.equity))
  const span = max - min || 1
  const x = (i: number) => pad + (i / (points.length - 1)) * (CurveGeometryWidth - 2 * pad)
  const y = (v: number) => CurveGeometryHeight - pad - ((v - min) / span) * (CurveGeometryHeight - 2 * pad)
  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(' ')
  const baseline = y(0).toFixed(1)
  const area = `${line} L${x(points.length - 1).toFixed(1)},${baseline} L${x(0).toFixed(1)},${baseline} Z`
  const lastPoint = points[points.length - 1]
  if (!lastPoint) return null
  return { line, area, baseline, endY: y(lastPoint.equity) }
})

const maxGroupPnl = computed(() => {
  const groups: GroupStats[] = analytics.value?.by_symbol ?? []
  return Math.max(1, ...groups.map(g => Math.abs(g.net_pnl)))
})

const latestEquity = computed(() => {
  const curve = analytics.value?.equity_curve ?? []
  return curve.length ? curve[curve.length - 1]?.equity ?? null : null
})

const peakEquity = computed(() => {
  const curve = analytics.value?.equity_curve ?? []
  return curve.length ? Math.max(0, ...curve.map(p => p.equity)) : null
})

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const sign = value > 0 ? '+' : value < 0 ? '−' : ''
  return `${sign}$${Math.abs(value).toFixed(2)}`
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

const kpis = computed(() => {
  const a = analytics.value
  return [
    { label: 'NET PNL', value: money(a?.net_pnl ?? 0), tone: (a?.net_pnl ?? 0) > 0 ? 'pos' : (a?.net_pnl ?? 0) < 0 ? 'neg' : 'flat' },
    { label: 'WIN RATE', value: percent(a?.win_rate ?? null), tone: 'neutral' },
    { label: 'TRADES', value: `${a?.total_trades ?? 0}`, tone: 'neutral' },
    { label: 'PROFIT FACTOR', value: a?.profit_factor !== null && a?.profit_factor !== undefined ? a.profit_factor.toFixed(2) : '—', tone: (a?.profit_factor ?? 0) >= 1 ? 'pos' : 'neg' },
    { label: 'MAX DRAWDOWN', value: `$${(a?.max_drawdown ?? 0).toFixed(2)}`, tone: 'neg' },
  ]
})

onMounted(loadAnalytics)

// Live settlement: when the reconciler books a trade or the loop completes a
// tick, refresh silently. A static snapshot is always one event behind at most.
watch(events, (list) => {
  const latest = list[0]
  if (latest && (latest.type === 'execution.trade.settled' || latest.type === 'loop.tick.completed')) {
    loadAnalytics()
  }
})

useHead({ title: 'TradingOS · Outcome Analytics' })
</script>

<template>
  <div class="setup-shell">
    <header class="setup-topbar">
      <p class="mono micro">OUTCOME ANALYTICS / SETTLED EVIDENCE ONLY</p>
      <div class="analytics-topbar-right">
        <span :class="['live-chip', isConnected ? 'live-chip--on' : 'live-chip--off']">{{ isConnected ? 'LIVE' : 'STATIC' }}</span>
        <NuxtLink class="return-link" to="/">&larr; CONTROL PLANE</NuxtLink>
      </div>
    </header>

    <section class="setup-hero setup-hero--tight">
      <p class="mono eyebrow">TRADE OUTCOMES</p>
      <h1>What the practice <em>ledger</em> actually says.</h1>
      <p>Every figure below is computed from settled trade outcomes joined back to their intents and strategy versions. Nothing here is estimated while an order is still open — reconciliation must settle it first.</p>
    </section>

    <p v-if="loadError" class="error-note desk-error desk-pad">{{ loadError }}</p>

    <section class="kpi-strip" aria-label="Headline outcome figures">
      <div v-for="kpi in kpis" :key="kpi.label" class="kpi-cell">
        <span class="mono micro">{{ kpi.label }}</span>
        <strong :class="`kpi-value kpi-value--${kpi.tone}`">{{ kpi.value }}</strong>
      </div>
    </section>

    <section class="setup-card analytics-curve-card">
      <div class="setup-heading">
        <div>
          <p class="mono micro">CUMULATIVE PRACTICE PNL</p>
          <h2>Equity curve</h2>
        </div>
        <div class="analytics-heading-right">
          <span class="mono micro">{{ analytics?.total_trades ?? 0 }} SETTLED</span>
          <button class="mini-control" type="button" :disabled="loading" @click="loadAnalytics">{{ loading ? 'READING…' : 'REFRESH' }}</button>
        </div>
      </div>
      <div class="curve-stage">
        <svg v-if="curveGeometry" class="curve-svg" :viewBox="`0 0 ${CurveGeometryWidth} ${CurveGeometryHeight}`" preserveAspectRatio="none" role="img" aria-label="Cumulative practice profit and loss">
          <defs>
            <linearGradient id="curve-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="rgba(131,187,176,.32)" />
              <stop offset="100%" stop-color="rgba(131,187,176,.02)" />
            </linearGradient>
          </defs>
          <line :x1="10" :y1="curveGeometry.baseline" :x2="CurveGeometryWidth - 10" :y2="curveGeometry.baseline" stroke="rgba(235,232,223,.18)" stroke-dasharray="3 5" stroke-width="1" />
          <path :d="curveGeometry.area" fill="url(#curve-fill)" />
          <path :d="curveGeometry.line" fill="none" stroke="#83bbb0" stroke-width="1.6" />
        </svg>
        <div v-else class="curve-empty">
          <span class="empty-glyph">∿</span>
          <p>No settled trades yet. The curve draws itself after the reconciler books the first outcome.</p>
        </div>
      </div>
      <div v-if="analytics?.equity_curve?.length" class="curve-foot mono micro">
        <span>START $0.00</span>
        <span>PEAK {{ money(peakEquity) }}</span>
        <span>NOW {{ money(latestEquity) }}</span>
      </div>
    </section>

    <div class="analytics-grid">
      <section class="setup-card">
        <div class="setup-heading">
          <div>
            <p class="mono micro">BY INSTRUMENT</p>
            <h2>Symbol ledger</h2>
          </div>
        </div>
        <div v-if="analytics?.by_symbol?.length" class="group-table">
          <div class="group-row group-row--head"><span>SYMBOL</span><span>TRADES</span><span>WIN</span><span>NET</span><span></span></div>
          <div v-for="row in analytics.by_symbol" :key="row.group" class="group-row">
            <span class="mono">{{ row.group }}</span>
            <span class="mono">{{ row.trades }}</span>
            <span class="mono">{{ percent(row.win_rate) }}</span>
            <span class="mono" :class="row.net_pnl > 0 ? 'pos' : row.net_pnl < 0 ? 'neg' : ''">{{ money(row.net_pnl) }}</span>
            <span class="group-bar"><span :style="{ width: `${(Math.abs(row.net_pnl) / maxGroupPnl) * 100}%` }" :class="row.net_pnl < 0 ? 'group-bar-fill group-bar-fill--neg' : 'group-bar-fill'"></span></span>
          </div>
        </div>
        <p v-else class="quiet-note desk-pad">No symbols have settled outcomes yet.</p>
      </section>

      <section class="setup-card">
        <div class="setup-heading">
          <div>
            <p class="mono micro">DIRECTION & ORIGIN</p>
            <h2>Side and strategy</h2>
          </div>
        </div>
        <div v-if="analytics?.by_side?.length" class="group-table">
          <div class="group-row group-row--head"><span>SIDE</span><span>TRADES</span><span>WIN</span><span>NET</span><span></span></div>
          <div v-for="row in analytics.by_side" :key="row.group" class="group-row">
            <span class="mono">{{ row.group }}</span>
            <span class="mono">{{ row.trades }}</span>
            <span class="mono">{{ percent(row.win_rate) }}</span>
            <span class="mono" :class="row.net_pnl > 0 ? 'pos' : row.net_pnl < 0 ? 'neg' : ''">{{ money(row.net_pnl) }}</span>
            <span></span>
          </div>
        </div>
        <p v-else class="quiet-note desk-pad">No directional outcomes recorded yet.</p>
        <div v-if="analytics?.by_strategy?.length" class="group-table group-table--split">
          <div class="group-row group-row--head"><span>STRATEGY</span><span>TRADES</span><span>WIN</span><span>NET</span><span></span></div>
          <div v-for="row in analytics.by_strategy" :key="row.group" class="group-row">
            <span class="mono">{{ row.group }}</span>
            <span class="mono">{{ row.trades }}</span>
            <span class="mono">{{ percent(row.win_rate) }}</span>
            <span class="mono" :class="row.net_pnl > 0 ? 'pos' : row.net_pnl < 0 ? 'neg' : ''">{{ money(row.net_pnl) }}</span>
            <span></span>
          </div>
        </div>
        <p v-else class="quiet-note desk-pad">Manual intents appear here as “manual” once they settle.</p>
      </section>
    </div>

    <section class="setup-card analytics-recent">
      <div class="setup-heading">
        <div>
          <p class="mono micro">APPEND-ONLY OUTCOMES</p>
          <h2>Recent settlements</h2>
        </div>
        <span class="panel-index">LAST 50</span>
      </div>
      <div v-if="analytics?.recent?.length" class="group-table">
        <div class="group-row group-row--recent group-row--head"><span>TIME</span><span>SYMBOL</span><span>SIDE</span><span>ORIGIN</span><span>AMOUNT</span><span>PNL</span><span>OUTCOME</span></div>
        <div v-for="trade in analytics.recent" :key="trade.id" class="group-row group-row--recent">
          <span class="mono">{{ new Date(trade.settled_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) }}</span>
          <span class="mono">{{ trade.symbol ?? '—' }}</span>
          <span class="mono">{{ trade.side ?? '—' }}</span>
          <span>{{ trade.strategy_key ? `${trade.strategy_key} #${trade.strategy_version_id}` : 'MANUAL' }}</span>
          <span class="mono">{{ trade.amount !== null ? `$${Number(trade.amount).toFixed(2)}` : '—' }}</span>
          <span class="mono" :class="trade.realized_pnl > 0 ? 'pos' : trade.realized_pnl < 0 ? 'neg' : ''">{{ money(trade.realized_pnl) }}</span>
          <span :class="['strategy-chip', trade.outcome === 'WIN' ? 'strategy-chip--validated' : trade.outcome === 'LOSS' ? 'strategy-chip--retired' : 'strategy-chip--draft']">{{ trade.outcome }}</span>
        </div>
      </div>
      <p v-else class="quiet-note desk-pad">No settlements recorded yet. Run the loop with a connected practice broker; outcomes appear here the moment reconciliation books them.</p>
    </section>
  </div>
</template>
