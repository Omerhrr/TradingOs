<!-- Design: The Instrument Room — comparison is evidence laid side by side, not a leaderboard of hopes. -->
<script setup lang="ts">
import type { StrategyComparisonRow } from '~/types/trading'

const api = useTradingApi()
const { isConnected, events } = useLoopSocket()
const comparison = ref<StrategyComparisonRow[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const SORT_KEYS = [
  { key: 'net_pnl', label: 'NET PNL' },
  { key: 'win_rate', label: 'WIN RATE' },
  { key: 'trades', label: 'TRADES' },
  { key: 'profit_factor', label: 'PROFIT FACTOR' },
  { key: 'max_drawdown', label: 'MAX DRAWDOWN' },
] as const
type SortKey = typeof SORT_KEYS[number]['key']

const focus = ref<SortKey>('net_pnl')

async function loadComparison() {
  loading.value = true
  loadError.value = null
  try {
    comparison.value = (await api.getStrategyComparison()).strategies
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'The strategy comparison could not be read from the local API.'
  } finally {
    loading.value = false
  }
}

const ranked = computed(() => {
  const rows = [...comparison.value]
  rows.sort((a, b) => {
    if (focus.value === 'max_drawdown') {
      return a.live.max_drawdown - b.live.max_drawdown // lower is better
    }
    const left = a.live[focus.value] ?? 0
    const right = b.live[focus.value] ?? 0
    return Number(right) - Number(left)
  })
  return rows
})

const leaderValue = computed(() => {
  const values = comparison.value.map(row => focus.value === 'max_drawdown'
    ? (Math.max(row.live.max_drawdown, 0.0001))
    : Math.max(Number(row.live[focus.value] ?? 0), 0.0001))
  return Math.max(...values, 0.0001)
})

function barWidth(row: StrategyComparisonRow): number {
  const value = focus.value === 'max_drawdown'
    ? Math.max(row.live.max_drawdown, 0)
    : Math.max(Number(row.live[focus.value] ?? 0), 0)
  return Math.min(100, (value / leaderValue.value) * 100)
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const sign = value > 0 ? '+' : value < 0 ? '−' : ''
  return `${sign}$${Math.abs(value).toFixed(2)}`
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

function paramsNote(row: StrategyComparisonRow): string {
  const params = row.params
  const parts = [
    params.fast_window !== null ? `EMA ${params.fast_window}/${params.slow_window}` : null,
    params.volatility_window !== null ? `VOL ${params.volatility_window}` : null,
    params.trade_amount !== null ? `$${Number(params.trade_amount).toFixed(2)}` : null,
    params.duration_minutes !== null ? `${params.duration_minutes}m` : null,
  ].filter(Boolean)
  return parts.length ? parts.join(' · ') : 'defaults'
}

function evaluationNote(row: StrategyComparisonRow): string {
  const evaluation = row.evaluation
  if (!evaluation.evaluated) return 'not evaluated'
  const metrics = evaluation.metrics
  const parts = [
    metrics.trades !== undefined ? `${metrics.trades} trades` : null,
    metrics.total_return !== undefined ? `${(metrics.total_return * 100).toFixed(1)}% return` : null,
    metrics.method ?? null,
  ].filter(Boolean)
  return `${evaluation.accepted ? 'accepted' : 'rejected'} · ${parts.join(', ') || 'no metrics'}`
}

onMounted(loadComparison)

// Live freshness: a settlement or a completed tick can reorder the table.
watch(events, (list) => {
  const latest = list[0]
  if (latest && (latest.type === 'execution.trade.settled' || latest.type === 'loop.tick.completed')) {
    loadComparison()
  }
})

useHead({ title: 'TradingOS · Strategy Comparison' })
</script>

<template>
  <div class="setup-shell">
    <header class="setup-topbar">
      <p class="mono micro">STRATEGY COMPARISON / SIDE BY SIDE EVIDENCE</p>
      <div class="analytics-topbar-right">
        <span :class="['live-chip', isConnected ? 'live-chip--on' : 'live-chip--off']">{{ isConnected ? 'LIVE' : 'STATIC' }}</span>
        <NuxtLink class="return-link" to="/">&larr; CONTROL PLANE</NuxtLink>
      </div>
    </header>

    <section class="setup-hero setup-hero--tight">
      <p class="mono eyebrow">STRATEGY DESK / COMPARISON</p>
      <h1>Which <em>version</em> earns the risk?</h1>
      <p>Every strategy version is reported with three evidence layers: what its settled practice trades actually did, how far its intents got through the gate, and whether its latest walk-forward evaluation earned VALIDATED status. Manual traffic is reported on the analytics page, not here.</p>
    </section>

    <p v-if="loadError" class="error-note desk-error desk-pad">{{ loadError }}</p>

    <section class="setup-card compare-focus-card">
      <div class="setup-heading">
        <div>
          <p class="mono micro">RANKING LENS</p>
          <h2>Sort the evidence</h2>
        </div>
        <button class="mini-control" type="button" :disabled="loading" @click="loadComparison">{{ loading ? 'READING…' : 'REFRESH' }}</button>
      </div>
      <div class="compare-focus desk-pad">
        <button
          v-for="option in SORT_KEYS"
          :key="option.key"
          type="button"
          :class="['focus-chip', { 'focus-chip--active': focus === option.key }]"
          @click="focus = option.key"
        >{{ option.label }}</button>
        <span v-if="focus === 'max_drawdown'" class="mono micro compare-focus-note">LOWER IS BETTER</span>
      </div>
    </section>

    <section v-if="ranked.length" class="compare-stack">
      <article
        v-for="(row, index) in ranked"
        :key="row.strategy_version_id"
        :class="['setup-card', 'compare-card', { 'compare-card--leader': index === 0 }]"
      >
        <div class="setup-heading compare-heading">
          <div class="compare-id">
            <span class="mono micro compare-rank">{{ index === 0 ? 'LEAD' : `#${index + 1}` }}</span>
            <div>
              <p class="mono micro">STRATEGY VERSION #{{ row.strategy_version_id }}</p>
              <h2>{{ row.strategy_key }} <span class="strategy-version">v{{ row.version }}</span></h2>
            </div>
          </div>
          <div class="compare-chips">
            <span :class="['strategy-chip', `strategy-chip--${row.status.toLowerCase()}`]">{{ row.status }}</span>
            <span class="connection-chip">{{ paramsNote(row) }}</span>
          </div>
        </div>

        <div class="compare-body">
          <div class="compare-metric compare-metric--focus">
            <span class="mono micro">{{ focus === 'max_drawdown' ? 'MAX DRAWDOWN' : focus.replaceAll('_', ' ') }}</span>
            <strong :class="focus === 'max_drawdown' ? 'neg' : (Number(row.live[focus] ?? 0) >= 0 ? 'pos' : 'neg')">
              {{ focus === 'net_pnl' ? money(row.live.net_pnl)
                : focus === 'win_rate' ? percent(row.live.win_rate)
                : focus === 'trades' ? `${row.live.trades}`
                : focus === 'profit_factor' ? (row.live.profit_factor !== null ? row.live.profit_factor.toFixed(2) : '—')
                : `$${row.live.max_drawdown.toFixed(2)}` }}
            </strong>
            <span class="group-bar"><span :class="['group-bar-fill', Number(row.live[focus] ?? 0) < 0 || (focus === 'max_drawdown' && row.live.max_drawdown > 0) ? 'group-bar-fill--neg' : '']" :style="{ width: `${barWidth(row)}%` }"></span></span>
          </div>

          <dl class="compare-facts">
            <div><dt>TRADES</dt><dd>{{ row.live.trades }}</dd></div>
            <div><dt>WIN RATE</dt><dd>{{ percent(row.live.win_rate) }}</dd></div>
            <div><dt>NET PNL</dt><dd :class="row.live.net_pnl > 0 ? 'pos' : row.live.net_pnl < 0 ? 'neg' : ''">{{ money(row.live.net_pnl) }}</dd></div>
            <div><dt>AVG</dt><dd>{{ money(row.live.avg_pnl) }}</dd></div>
            <div><dt>PROFIT FACTOR</dt><dd>{{ row.live.profit_factor !== null ? row.live.profit_factor.toFixed(2) : '—' }}</dd></div>
            <div><dt>MAX DD</dt><dd class="neg">${{ row.live.max_drawdown.toFixed(2) }}</dd></div>
            <div><dt>BEST</dt><dd class="pos">{{ money(row.live.best_pnl) }}</dd></div>
            <div><dt>WORST</dt><dd class="neg">{{ money(row.live.worst_pnl) }}</dd></div>
          </dl>

          <div class="compare-side">
            <div class="compare-gate">
              <span class="mono micro">GATE PATH</span>
              <p class="mono">{{ row.activity.intents }} INTENT<span v-if="row.activity.intents !== 1">S</span>
                → {{ row.activity.approved }} APPROVED
                → {{ row.activity.submitted }} SUBMITTED
                · {{ row.activity.rejected }} REJECTED</p>
            </div>
            <div class="compare-eval">
              <span class="mono micro">WALK-FORWARD</span>
              <p :class="row.evaluation.evaluated ? (row.evaluation.accepted ? 'pos' : '') : ''">{{ evaluationNote(row) }}</p>
            </div>
          </div>
        </div>
      </article>
    </section>

    <section v-else class="setup-card compare-empty">
      <span class="empty-glyph">⇆</span>
      <p>No strategy versions exist yet. Create and validate one on the Strategy desk; it appears here the moment it exists — with zeros until practice evidence arrives.</p>
      <NuxtLink class="return-link" to="/strategies">OPEN THE STRATEGY DESK →</NuxtLink>
    </section>
  </div>
</template>
