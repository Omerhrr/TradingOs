<!-- Design: The Instrument Room — the lab proves parameters on stored candles before any version earns risk. -->
<script setup lang="ts">
import type { BacktestRun, BacktestSweep, SavedPickCell, StrategyVersion, SweepPickSave, SweepRunRecord } from '~/types/trading'

const api = useTradingApi()

const adminToken = ref('')
const strategies = ref<StrategyVersion[]>([])
const strategiesLoading = ref(true)

const source = ref<'strategy' | 'custom'>('strategy')
const strategyId = ref<number | null>(null)
const symbol = ref('EURUSD')
const timeframeSeconds = ref(60)
const censorGapSeconds = ref(60)
const customFast = ref(8)
const customSlow = ref(30)
const customVol = ref(20)

const sweepFast = ref('4, 8, 12, 16')
const sweepSlow = ref('24, 34, 48, 60')
const sweepRunning = ref(false)
const sweep = ref<BacktestSweep | null>(null)
const sweepError = ref<string | null>(null)

// --- Sweep memory: past surfaces stay replayable, saved cells stay marked ---
const sweepHistory = ref<SweepRunRecord[]>([])
const historyLoading = ref(false)
const savedPicks = ref<SavedPickCell[]>([])
const savedCellKeys = computed(() => new Set(savedPicks.value.map(pick => `${pick.fast_window}:${pick.slow_window}`)))
const savedCellNotes = computed(() => new Map(savedPicks.value.map(pick => [`${pick.fast_window}:${pick.slow_window}`, pick])))
const sweepLens = [
  { key: 'total_return', label: 'RETURN' },
  { key: 'win_rate', label: 'WIN RATE' },
  { key: 'max_drawdown', label: 'MAX DD' },
] as const
type SweepLens = typeof sweepLens[number]['key']
const sweepFocus = ref<SweepLens>('total_return')

const running = ref(false)
const result = ref<BacktestRun | null>(null)
const runError = ref<string | null>(null)

const saveKey = ref('')
const saveVersion = ref('v1')
const saving = ref(false)
const savedPick = ref<SweepPickSave | null>(null)
const saveError = ref<string | null>(null)

function sanitizeKey(raw: string): string {
  return raw.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 100)
}

watch(result, (run) => {
  savedPick.value = null
  saveError.value = null
  if (run) {
    saveVersion.value = 'v1'
    saveKey.value = sanitizeKey(`ema-${run.symbol}-${run.params.fast_window}x${run.params.slow_window}`)
  }
})

// Evidence honesty check: the desk recomputes the walk-forward at save time,
// so a mismatch against the runner output means candles arrived in between.
const savedDrift = computed(() => {
  const saved = savedPick.value
  const run = result.value
  if (!saved || !run) return false
  return saved.evidence.metrics.trades !== run.metrics.trades
    || Math.abs(saved.evidence.metrics.total_return - run.metrics.total_return) > 1e-9
})

// The pick links back to the remembered sweep surface only when the runner's
// current result sits on that surface (same symbol, timeframe, censor gap).
const sweepRunIdForCurrentResult = computed<number | null>(() => {
  const run = result.value
  const activeSweep = sweep.value
  if (!run || !activeSweep?.sweep_run_id) return null
  const sameSignature = activeSweep.symbol === run.symbol
    && activeSweep.timeframe_seconds === run.timeframe_seconds
    && activeSweep.censor_gap_seconds === run.censor_gap_seconds
  return sameSignature ? activeSweep.sweep_run_id : null
})

async function loadSweepHistory() {
  historyLoading.value = true
  try {
    sweepHistory.value = await api.getSweepRuns(20)
  } catch {
    sweepHistory.value = []
  } finally {
    historyLoading.value = false
  }
}

async function loadSavedCells() {
  try {
    savedPicks.value = await api.getSavedPickCells(symbol.value.trim().toUpperCase() || 'EURUSD', timeframeSeconds.value, censorGapSeconds.value)
  } catch {
    savedPicks.value = []
  }
}

function reloadSweepRun(record: SweepRunRecord) {
  sweep.value = {
    symbol: record.symbol,
    timeframe_seconds: record.timeframe_seconds,
    censor_gap_seconds: record.censor_gap_seconds,
    volatility_window: record.volatility_window,
    cells: record.cells,
    generated_at: record.created_at,
    sweep_run_id: record.id,
  }
  sweepFast.value = record.fast_windows.join(', ')
  sweepSlow.value = record.slow_windows.join(', ')
}

function bestCellReturn(record: SweepRunRecord): number | null {
  const values = record.cells.filter(cell => cell.metrics).map(cell => cell.metrics!.total_return)
  return values.length ? Math.max(...values) : null
}

async function savePickAsDraft() {
  const run = result.value
  if (!run) return
  const token = adminToken.value.trim()
  if (!token) { saveError.value = 'The local admin token is required to save the pick. Paste it below; it stays in this tab.'; return }
  const key = sanitizeKey(saveKey.value)
  if (key.length < 3) { saveError.value = 'The strategy key needs at least 3 lowercase letters, digits, hyphens or underscores.'; return }
  if (!saveVersion.value.trim()) { saveError.value = 'Give the draft a version label.'; return }
  saving.value = true
  saveError.value = null
  try {
    savedPick.value = await api.saveSweepPick(token, {
      strategy_key: key,
      version: saveVersion.value.trim(),
      symbol: run.symbol,
      timeframe_seconds: run.timeframe_seconds,
      censor_gap_seconds: run.censor_gap_seconds,
      fast_window: run.params.fast_window,
      slow_window: run.params.slow_window,
      volatility_window: run.params.volatility_window,
      sweep_run_id: sweepRunIdForCurrentResult.value,
    })
    window.sessionStorage.setItem('tradingos-local-admin-token', token)
    await loadStrategies()
    void loadSavedCells()
  } catch (error) {
    const message = error instanceof Error ? error.message : ''
    saveError.value = message.includes('409')
      ? 'A strategy with this key and version already exists — bump the version label.'
      : message.includes('422')
        ? 'The desk refused the pick: windows must satisfy fast < slow and at least 30 candles must be stored for the symbol.'
        : message.includes('401')
          ? 'The admin token was rejected. Check it and try again.'
          : 'The pick could not be saved as a draft strategy.'
  } finally {
    saving.value = false
  }
}

const CurveGeometryWidth = 600
const CurveGeometryHeight = 220
const curveGeometry = computed(() => {
  const points = result.value?.equity_curve ?? []
  if (points.length < 2) return null
  const pad = 10
  const min = Math.min(0.95, ...points.map(p => p.equity))
  const max = Math.max(1.05, ...points.map(p => p.equity))
  const span = max - min || 1
  const x = (i: number) => pad + (i / (points.length - 1)) * (CurveGeometryWidth - 2 * pad)
  const y = (v: number) => CurveGeometryHeight - pad - ((v - min) / span) * (CurveGeometryHeight - 2 * pad)
  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(' ')
  const lastPoint = points[points.length - 1]
  if (!lastPoint) return null
  return { line, baseline: y(1).toFixed(1), endY: y(lastPoint.equity) }
})

const sweepGrid = computed(() => {
  const cells = sweep.value?.cells ?? []
  const fastValues = [...new Set(cells.map(c => c.fast_window))].sort((a, b) => a - b)
  const slowValues = [...new Set(cells.map(c => c.slow_window))].sort((a, b) => a - b)
  const byPair = new Map(cells.map(c => [`${c.fast_window}:${c.slow_window}`, c]))
  const values = cells
    .map(c => sweepMetric(c))
    .filter((v): v is number => v !== null)
  const min = Math.min(...values, 0)
  const max = Math.max(...values, 0)
  return { fastValues, slowValues, byPair, min, max }
})

function sweepMetric(cell: { metrics: { total_return: number; win_rate: number; max_drawdown: number } | null; error: string | null }): number | null {
  if (!cell.metrics) return null
  if (sweepFocus.value === 'max_drawdown') return -cell.metrics.max_drawdown // drawn inverted: higher is better
  return cell.metrics[sweepFocus.value]
}

function cellStyle(cell: { metrics: { total_return: number; win_rate: number; max_drawdown: number } | null; error: string | null }): string {
  const value = sweepMetric(cell)
  if (value === null) return 'background: rgba(235,232,223,.04)'
  const { min, max } = sweepGrid.value
  const span = max - min || 1
  const intensity = 0.12 + 0.55 * ((value - min) / span)
  return value >= 0
    ? `background: rgba(131, 187, 176, ${intensity.toFixed(3)})`
    : `background: rgba(207, 106, 92, ${intensity.toFixed(3)})`
}

function parseWindows(raw: string): number[] {
  return [...new Set(raw.split(/[,;\s]+/).map(part => Number.parseInt(part, 10)).filter(value => Number.isFinite(value) && value > 0))]
}

async function loadStrategies() {
  strategiesLoading.value = true
  try {
    strategies.value = await api.getStrategies()
    const firstStrategy = strategies.value[0]
    if (strategyId.value === null && firstStrategy) strategyId.value = firstStrategy.id
  } catch {
    strategies.value = []
  } finally {
    strategiesLoading.value = false
  }
}

function requireToken(): string | null {
  if (adminToken.value.trim()) return adminToken.value
  runError.value = 'The local admin token is required to drive the lab. Paste it below; it stays in this tab.'
  return null
}

async function runBacktest(override?: { fast: number; slow: number; vol?: number }) {
  const token = requireToken()
  if (!token) return
  running.value = true
  runError.value = null
  try {
    const payload: Record<string, unknown> = {
      symbol: symbol.value.trim().toUpperCase(),
      timeframe_seconds: timeframeSeconds.value,
      censor_gap_seconds: censorGapSeconds.value,
    }
    if (override) {
      payload.definition = { kind: 'ema_cross', fast_window: override.fast, slow_window: override.slow, volatility_window: override.vol ?? customVol.value }
    } else if (source.value === 'strategy') {
      if (!strategyId.value) { runError.value = 'Create a strategy version first — the lab runs stored definitions or inline custom ones.'; return }
      payload.strategy_version_id = strategyId.value
    } else {
      if (customFast.value >= customSlow.value) { runError.value = 'The fast EMA window must be smaller than the slow one.'; return }
      payload.definition = { kind: 'ema_cross', fast_window: customFast.value, slow_window: customSlow.value, volatility_window: customVol.value }
    }
    result.value = await api.runBacktest(token, payload as Parameters<typeof api.runBacktest>[1])
    window.sessionStorage.setItem('tradingos-local-admin-token', token)
  } catch (error) {
    const message = error instanceof Error ? error.message : ''
    runError.value = message.includes('422')
      ? 'The runner refused the request: at least 30 stored candles are needed, windows must be valid, and the censor gap must leave room to trade.'
      : message.includes('401')
        ? 'The admin token was rejected. Check it and try again.'
        : message.includes('404')
          ? 'That strategy version no longer exists.'
          : 'The backtest could not be completed against the local API.'
  } finally {
    running.value = false
  }
}

async function runSweep() {
  const token = requireToken()
  if (!token) return
  const fasts = parseWindows(sweepFast.value)
  const slows = parseWindows(sweepSlow.value)
  if (!fasts.length || !slows.length) { sweepError.value = 'Enter comma-separated positive integers for both window ranges.'; return }
  if (fasts.length * slows.length > 24) { sweepError.value = 'The grid is capped at 24 cells server-side; narrow the ranges.'; return }
  sweepRunning.value = true
  sweepError.value = null
  try {
    sweep.value = await api.runBacktestSweep(token, {
      symbol: symbol.value.trim().toUpperCase(),
      timeframe_seconds: timeframeSeconds.value,
      censor_gap_seconds: censorGapSeconds.value,
      fast_windows: fasts,
      slow_windows: slows,
      volatility_window: customVol.value,
    })
    window.sessionStorage.setItem('tradingos-local-admin-token', token)
    void loadSavedCells()
    void loadSweepHistory()
  } catch (error) {
    sweepError.value = error instanceof Error ? error.message : 'The sweep could not be completed.'
  } finally {
    sweepRunning.value = false
  }
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(2)}%`
}

function moneyish(value: number): string {
  return `${value >= 0 ? '+' : '−'}${Math.abs(value * 100).toFixed(2)}%`
}

onMounted(() => {
  adminToken.value = window.sessionStorage.getItem('tradingos-local-admin-token') ?? ''
  loadStrategies()
  void loadSweepHistory()
  void loadSavedCells()
})

// A new sweep signature (symbol / timeframe / censor gap) needs fresh markers.
watch([symbol, timeframeSeconds, censorGapSeconds], () => { void loadSavedCells() })

useHead({ title: 'TradingOS · Backtest Lab' })
</script>

<template>
  <div class="setup-shell">
    <header class="setup-topbar">
      <p class="mono micro">BACKTEST LAB / STORED CANDLES · NO FORWARD LOOKING</p>
      <NuxtLink class="return-link" to="/">&larr; CONTROL PLANE</NuxtLink>
    </header>

    <section class="setup-hero setup-hero--tight">
      <p class="mono eyebrow">STRATEGY DESK / LAB</p>
      <h1>Prove the <em>parameters</em> before they earn risk.</h1>
      <p>The runner walks forward over candles the control plane already stores — decision at candle i, entry and exit strictly after the censor gap — and writes nothing: no evaluations, no status, no audit rows. Committing a result still happens on the Strategy desk.</p>
    </section>

    <section class="setup-card setup-card--form">
      <div class="setup-heading">
        <div>
          <p class="mono micro">RUNNER INPUT</p>
          <h2>What should the walker test?</h2>
        </div>
        <span class="connection-chip">{{ strategies.length }} STORED VERSION{{ strategies.length === 1 ? '' : 'S' }}</span>
      </div>
      <label class="field-token">LOCAL ADMIN TOKEN<span>stored in this tab only</span>
        <input v-model="adminToken" type="password" autocomplete="off" placeholder="TRADINGOS_LOCAL_ADMIN_TOKEN">
      </label>
      <label class="field-token">MARKET<span>symbol stored by the broker worker</span>
        <input v-model="symbol" type="text" placeholder="EURUSD">
      </label>
      <div class="backtest-inline">
        <label class="field-token">TIMEFRAME (S)<input v-model.number="timeframeSeconds" type="number" min="1" max="86400"></label>
        <label class="field-token">CENSOR GAP (S)<input v-model.number="censorGapSeconds" type="number" min="1" max="86400"></label>
      </div>
      <div class="backtest-inline">
        <label class="field-token">SOURCE
          <select v-model="source">
            <option value="strategy">Stored strategy version</option>
            <option value="custom">Custom parameters (no version)</option>
          </select>
        </label>
        <template v-if="source === 'strategy'">
          <label class="field-token">STRATEGY VERSION
            <select v-model="strategyId" :disabled="strategiesLoading || !strategies.length">
              <option v-if="!strategies.length" :value="null">{{ strategiesLoading ? 'READING…' : 'none stored yet' }}</option>
              <option v-for="strategy in strategies" :key="strategy.id" :value="strategy.id">#{{ strategy.id }} {{ strategy.strategy_key }} v{{ strategy.version }} ({{ strategy.status }})</option>
            </select>
          </label>
        </template>
        <template v-else>
          <label class="field-token">FAST / SLOW EMA
            <div class="backtest-pair">
              <input v-model.number="customFast" type="number" min="1" max="200">
              <input v-model.number="customSlow" type="number" min="2" max="400">
            </div>
          </label>
        </template>
      </div>
      <p v-if="runError" class="setup-error">{{ runError }}</p>
      <button class="setup-action" type="button" :disabled="running" @click="runBacktest()">{{ running ? 'WALKING FORWARD…' : 'RUN BACKTEST' }}</button>
    </section>

    <section v-if="result" class="kpi-strip" aria-label="Backtest headline figures">
      <div class="kpi-cell"><span class="mono micro">TRADES</span><strong class="kpi-value kpi-value--neutral">{{ result.metrics.trades }}</strong></div>
      <div class="kpi-cell"><span class="mono micro">WIN RATE</span><strong class="kpi-value kpi-value--neutral">{{ percent(result.metrics.win_rate) }}</strong></div>
      <div class="kpi-cell"><span class="mono micro">TOTAL RETURN</span><strong :class="['kpi-value', result.metrics.total_return > 0 ? 'kpi-value--pos' : result.metrics.total_return < 0 ? 'kpi-value--neg' : 'kpi-value--neutral']">{{ moneyish(result.metrics.total_return) }}</strong></div>
      <div class="kpi-cell"><span class="mono micro">MAX DRAWDOWN</span><strong class="kpi-value kpi-value--neg">{{ percent(result.metrics.max_drawdown) }}</strong></div>
      <div class="kpi-cell"><span class="mono micro">AVG TRADE</span><strong :class="['kpi-value', result.metrics.average_trade_return > 0 ? 'kpi-value--pos' : 'kpi-value--neg']">{{ moneyish(result.metrics.average_trade_return) }}</strong></div>
    </section>

    <section v-if="result" class="setup-card">
      <div class="setup-heading">
        <div>
          <p class="mono micro">MULTIPLICATIVE EQUITY · START 1.00</p>
          <h2>Walk-forward equity curve</h2>
        </div>
        <span class="panel-index">EMA {{ result.params.fast_window }}/{{ result.params.slow_window }}</span>
      </div>
      <div class="curve-stage">
        <svg v-if="curveGeometry" class="curve-svg" :viewBox="`0 0 ${CurveGeometryWidth} ${CurveGeometryHeight}`" preserveAspectRatio="none" role="img" aria-label="Backtest equity curve">
          <line :x1="10" :y1="curveGeometry.baseline" :x2="CurveGeometryWidth - 10" :y2="curveGeometry.baseline" stroke="rgba(235,232,223,.18)" stroke-dasharray="3 5" stroke-width="1" />
          <path :d="curveGeometry.line" fill="none" :stroke="result.metrics.total_return >= 0 ? '#83bbb0' : '#cf6a5c'" stroke-width="1.6" />
        </svg>
        <div v-else class="curve-empty">
          <span class="empty-glyph">∿</span>
          <p>This parameter set produced no trades on the stored window.</p>
        </div>
      </div>
      <div v-if="result.trades.length" class="group-table">
        <div class="group-row group-row--recent group-row--head"><span>#</span><span>DECIDED</span><span>SIGNAL</span><span>ENTRY</span><span>EXIT</span><span>RETURN</span></div>
        <div v-for="trade in [...result.trades].reverse().slice(0, 12)" :key="trade.index" class="group-row group-row--recent">
          <span class="mono">{{ trade.index }}</span>
          <span class="mono">{{ new Date(trade.decision_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}</span>
          <span :class="['strategy-chip', trade.signal === 'CALL' ? 'strategy-chip--validated' : 'strategy-chip--retired']">{{ trade.signal }}</span>
          <span class="mono">{{ trade.entry_close.toFixed(5) }}</span>
          <span class="mono">{{ trade.exit_close.toFixed(5) }}</span>
          <span class="mono" :class="trade.trade_return >= 0 ? 'pos' : 'neg'">{{ moneyish(trade.trade_return) }}</span>
        </div>
      </div>
      <p class="quiet-note desk-pad">Showing the {{ Math.min(12, result.trades.length) }} most recent of {{ result.trades.length }} simulated trades. Entry/exit are closes one censor gap apart; nothing here touched a broker.</p>
    </section>

    <section v-if="result" class="setup-card setup-card--form">
      <div class="setup-heading">
        <div>
          <p class="mono micro">LAB &rarr; STRATEGY DESK</p>
          <h2>Save this pick as a draft strategy</h2>
        </div>
        <span class="panel-index">EMA {{ result.params.fast_window }}/{{ result.params.slow_window }} · {{ result.symbol }}</span>
      </div>
      <p class="quiet-note desk-pad">The desk never trusts numbers echoed back by a browser: saving recomputes the walk-forward over the candles stored right now and persists that as the draft's evidence. The draft stays DRAFT — VALIDATED is still earned only on the Strategy desk.</p>
      <div class="backtest-inline">
        <label class="field-token">STRATEGY KEY<span>lowercase letters, digits, - and _</span><input v-model="saveKey" type="text"></label>
        <label class="field-token">VERSION<span>draft label</span><input v-model="saveVersion" type="text"></label>
      </div>
      <p v-if="saveError" class="setup-error">{{ saveError }}</p>
      <button class="setup-action" type="button" :disabled="saving" @click="savePickAsDraft">{{ saving ? 'RECOMPUTING EVIDENCE…' : 'SAVE AS DRAFT STRATEGY' }}</button>
      <div v-if="savedPick" class="save-confirmation">
        <div class="save-confirmation-head">
          <strong>DRAFT SAVED · #{{ savedPick.strategy.id }} {{ savedPick.strategy.strategy_key }} v{{ savedPick.strategy.version }}</strong>
          <NuxtLink class="return-link" to="/strategies">OPEN STRATEGY DESK &rarr;</NuxtLink>
        </div>
        <div class="save-confirmation-grid mono">
          <span>SYMBOL {{ savedPick.evidence.symbol }} · {{ savedPick.evidence.timeframe_seconds }}s</span>
          <span>TRADES {{ savedPick.evidence.metrics.trades }}</span>
          <span>WIN RATE {{ percent(savedPick.evidence.metrics.win_rate) }}</span>
          <span>RETURN {{ moneyish(savedPick.evidence.metrics.total_return) }}</span>
          <span>MAX DD {{ percent(savedPick.evidence.metrics.max_drawdown) }}</span>
          <span>DD GATE {{ percent(savedPick.evidence.max_drawdown_gate) }}</span>
          <span v-if="savedPick.sweep_pick?.sweep_run_id">CELL LINKED TO SWEEP #{{ savedPick.sweep_pick.sweep_run_id }}</span>
          <span v-else>CELL MEMORY RECORDED</span>
        </div>
        <p v-if="savedDrift" class="save-drift">Candles moved between the run and the save — the draft's stored evidence reflects the newer data, not the figures you just previewed.</p>
      </div>
    </section>

    <section class="setup-card setup-card--form">
      <div class="setup-heading">
        <div>
          <p class="mono micro">PARAMETER SURFACE</p>
          <h2>Fast × slow sweep</h2>
        </div>
        <div class="backtest-lens">
          <button v-for="lens in sweepLens" :key="lens.key" type="button" :class="['focus-chip', { 'focus-chip--active': sweepFocus === lens.key }]" @click="sweepFocus = lens.key">{{ lens.label }}</button>
        </div>
      </div>
      <div class="backtest-inline">
        <label class="field-token">FAST WINDOWS<span>comma separated</span><input v-model="sweepFast" type="text"></label>
        <label class="field-token">SLOW WINDOWS<span>comma separated</span><input v-model="sweepSlow" type="text"></label>
      </div>
      <p v-if="sweepError" class="setup-error">{{ sweepError }}</p>
      <button class="setup-action setup-action--quiet" type="button" :disabled="sweepRunning" @click="runSweep">{{ sweepRunning ? 'SWEEPING…' : 'RUN SWEEP' }}</button>
      <div v-if="sweep?.cells?.length" class="backtest-grid-wrap desk-pad">
        <div class="backtest-grid" :style="{ gridTemplateColumns: `48px repeat(${sweepGrid.slowValues.length}, minmax(64px, 1fr))` }">
          <span></span>
          <span v-for="slow in sweepGrid.slowValues" :key="`head-${slow}`" class="mono micro backtest-grid-head">SLOW {{ slow }}</span>
          <template v-for="fast in sweepGrid.fastValues" :key="`row-${fast}`">
            <span class="mono micro backtest-grid-head">FAST {{ fast }}</span>
            <button
              v-for="slow in sweepGrid.slowValues"
              :key="`${fast}:${slow}`"
              type="button"
              :class="['backtest-cell', 'mono', { 'backtest-cell--saved': savedCellKeys.has(`${fast}:${slow}`) }]"
              :title="savedCellKeys.has(`${fast}:${slow}`) ? `Already saved as ${savedCellNotes.get(`${fast}:${slow}`)?.strategy_key} v${savedCellNotes.get(`${fast}:${slow}`)?.version}` : undefined"
              :style="cellStyle(sweepGrid.byPair.get(`${fast}:${slow}`) ?? { metrics: null, error: null })"
              :disabled="running"
              @click="runBacktest({ fast, slow })"
            >
              <template v-if="sweepGrid.byPair.get(`${fast}:${slow}`)?.metrics">
                {{ sweepFocus === 'max_drawdown' ? percent(sweepGrid.byPair.get(`${fast}:${slow}`)?.metrics?.max_drawdown) : sweepFocus === 'win_rate' ? percent(sweepGrid.byPair.get(`${fast}:${slow}`)?.metrics?.win_rate) : moneyish(sweepGrid.byPair.get(`${fast}:${slow}`)?.metrics?.total_return ?? 0) }}
              </template>
              <template v-else>—</template>
              <span v-if="savedCellKeys.has(`${fast}:${slow}`)" class="saved-dot" aria-hidden="true">●</span>
            </button>
          </template>
        </div>
        <p class="quiet-note">Tap a cell to load those windows into the runner above, then save the pick as a draft straight from the lab. Inverted pairs and candle-starved cells report an em dash. Green deepens with the lens value; red marks negative ones (max-drawdown lens inverts so deeper green is safer). A ● dot marks a cell already promoted to a draft for this exact surface.</p>
      </div>
    </section>

    <section class="setup-card setup-card--form">
      <div class="setup-heading">
        <div>
          <p class="mono micro">SWEEP MEMORY · LAST 20 SURFACES</p>
          <h2>Every grid is remembered — picks recall their cell</h2>
        </div>
        <button class="setup-action setup-action--quiet" type="button" :disabled="historyLoading" @click="loadSweepHistory">{{ historyLoading ? 'READING…' : 'REFRESH HISTORY' }}</button>
      </div>
      <p class="quiet-note desk-pad">The lab stays read-only; the desk keeps a bounded memory of the surfaces it swept. Reload one to re-open its heatmap, and a pick saved from it will carry the exact (fast × slow) cell in its provenance — visible on the Strategy desk and in every evidence export.</p>
      <div v-if="sweepHistory.length" class="sweep-history desk-pad">
        <div v-for="record in sweepHistory.slice(0, 8)" :key="record.id" class="sweep-history-row">
          <span class="mono">#{{ record.id }}</span>
          <span class="mono">{{ record.symbol }} · {{ record.timeframe_seconds }}s</span>
          <span class="mono">{{ record.fast_windows.length }}×{{ record.slow_windows.length }} CELLS</span>
          <span class="mono" :class="(bestCellReturn(record) ?? 0) >= 0 ? 'pos' : 'neg'">BEST {{ bestCellReturn(record) === null ? '—' : moneyish(bestCellReturn(record)!) }}</span>
          <span class="mono sweep-history-when">{{ new Date(record.created_at).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) }}</span>
          <button class="mini-control" type="button" @click="reloadSweepRun(record)">RELOAD</button>
        </div>
      </div>
      <p v-else class="quiet-note desk-pad">No sweeps recorded yet — run one above and it will be remembered here, newest first, with the grid capped at 24 surfaces.</p>
    </section>
  </div>
</template>

<style scoped>
.backtest-inline { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: 0 26px; }
.backtest-inline .field-token { margin: 0; display: grid; gap: 8px; }
.field-token { display: grid; gap: 8px; margin: 0 26px; color: var(--paper); font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: .06em; }
.field-token span { color: var(--quiet); }
.field-token input, .field-token select { width: 100%; border: 1px solid var(--line); border-radius: 0; outline: 0; padding: 13px 14px; background: rgba(8, 10, 10, .75); color: var(--paper); font: 12px 'DM Mono', monospace; }
.field-token input:focus, .field-token select:focus { border-color: var(--teal); }
.backtest-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.backtest-lens { display: flex; gap: 8px; flex-wrap: wrap; }
.backtest-grid-wrap { display: grid; gap: 14px; margin: 0 26px 26px; }
.backtest-grid { display: grid; gap: 6px; align-items: stretch; }
.backtest-grid-head { color: var(--quiet); align-self: center; }
.backtest-cell { border: 1px solid var(--line); padding: 12px 6px; color: var(--paper); font-size: 10px; letter-spacing: .02em; transition: outline .12s ease; }
.backtest-cell:hover:not(:disabled) { outline: 1px solid var(--brass); }
.save-confirmation { margin: 0 26px 26px; border: 1px solid rgba(131,187,176,.4); background: rgba(131,187,176,.06); padding: 16px 18px; display: grid; gap: 10px; }
.backtest-cell--saved { outline: 1px solid var(--brass); }
.saved-dot { display: block; color: var(--brass); font-size: 8px; line-height: 1; margin-top: 4px; }
.sweep-history { display: grid; gap: 0; margin: 0 26px 26px; }
.sweep-history-row { display: grid; grid-template-columns: 56px minmax(140px, 1fr) 96px 110px minmax(130px, .8fr) auto; gap: 12px; align-items: center; padding: 11px 0; border-top: 1px solid var(--line); color: var(--paper); font-size: 11px; }
.sweep-history-when { color: var(--quiet); }
.save-confirmation-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.save-confirmation-head strong { font-family: 'DM Mono', monospace; font-size: 11px; letter-spacing: .08em; color: var(--teal); }
.save-confirmation-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px 16px; font-size: 10px; letter-spacing: .05em; color: var(--paper); }
.save-drift { margin: 0; font-size: 11px; color: var(--brass); }
</style>
