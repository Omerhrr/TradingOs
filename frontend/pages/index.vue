<!-- TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design. -->
<script setup lang="ts">
import type { AlertList, AlertRow, AuditEvent, LoopRun, LoopStatus, MarketChart, OrderIntent, PositionSnapshot, ReconciliationRun, ResearchRun, RiskPolicy, StrategyVersion, SystemState, WatchlistItem } from '~/types/trading'

const api = useTradingApi()
const auth = useAuth()
const { status: socketStatus, events: socketEvents, isConnected } = useLoopSocket()
const isTogglingExposure = ref(false)
const exposureError = ref<string | null>(null)
const isTicking = ref(false)
const tickError = ref<string | null>(null)
const tickNotice = ref<string | null>(null)
const adminToken = ref('')
const now = ref(new Date())
const visualAssets = {
  logo: '/tradingos-mark.svg',
  hero: '/tradingos-hero.svg',
}

// --- Loop-panel market tape --------------------------------------------
const chart = ref<MarketChart | null>(null)
const chartError = ref<string | null>(null)
const chartLoading = ref(false)
const selectedPair = ref('')

const { data: state, pending: statePending, error: stateError, refresh: refreshState } = await useAsyncData<SystemState>('trading-state', api.getState)
const { data: risk, error: riskError } = await useAsyncData<RiskPolicy>('risk-policy', api.getRisk)
const { data: events, refresh: refreshEvents } = await useAsyncData<AuditEvent[]>('audit-events', api.getEvents)
const { data: watchlist, refresh: refreshWatchlist } = await useAsyncData<WatchlistItem[]>('watchlist', api.getWatchlist)
const { data: strategies } = await useAsyncData<StrategyVersion[]>('strategies', api.getStrategies)
const { data: intents, refresh: refreshIntents } = await useAsyncData<OrderIntent[]>('order-intents', api.getOrderIntents)
const { data: positions } = await useAsyncData<PositionSnapshot[]>('positions', api.getPositions)
const { data: reconciliations, refresh: refreshReconciliations } = await useAsyncData<ReconciliationRun[]>('reconciliations', api.getReconciliationRuns)
const { data: researchRuns } = await useAsyncData<ResearchRun[]>('research-runs', api.getResearchRuns)
const { data: loopStatus, refresh: refreshLoopStatus } = await useAsyncData<LoopStatus>('loop-status', api.getLoopStatus)
const { data: loopRuns, refresh: refreshLoopRuns } = await useAsyncData<LoopRun[]>('loop-runs', api.getLoopRuns)
const { data: alertData, refresh: refreshAlerts } = await useAsyncData<AlertList>('guard-alerts', () => api.getAlerts(false))

// --- Loop-panel market tape (needs the watchlist above) ----------------
function pairKey(symbol: string, timeframe: number): string {
  return `${symbol}:${timeframe}`
}

const chartPairs = computed(() => {
  const items = watchlist.value?.filter(item => item.enabled) ?? []
  return items.map(item => ({ symbol: item.symbol, timeframe: item.timeframe_seconds, label: `${item.symbol} · ${item.timeframe_seconds}s` }))
})

async function loadChart() {
  if (!selectedPair.value) return
  const [symbol, timeframeText] = selectedPair.value.split(':')
  const timeframe = Number(timeframeText)
  if (!symbol || !Number.isFinite(timeframe)) return
  chartLoading.value = true
  chartError.value = null
  try {
    chart.value = await api.getMarketChart(symbol, timeframe, 120)
  } catch (error) {
    chartError.value = error instanceof Error ? error.message : 'The market tape could not be read from the local API.'
  } finally {
    chartLoading.value = false
  }
}

watch(watchlist, (items) => {
  if (selectedPair.value || !items?.length) return
  const first = items.find(item => item.enabled) ?? items[0]
  if (first) {
    selectedPair.value = pairKey(first.symbol, first.timeframe_seconds)
    void loadChart()
  }
}, { immediate: true })

const runtime = computed(() => state.value?.system_state ?? 'UNAVAILABLE')
const sourceStatus = computed(() => state.value?.broker_connection === 'CONNECTED' ? 'RECONCILED' : 'AWAITING BROKER')
const latestReconciliation = computed(() => reconciliations.value?.[0])
const latestResearch = computed(() => researchRuns.value?.[0])
const validatedStrategies = computed(() => strategies.value?.filter(strategy => strategy.status === 'VALIDATED').length ?? 0)
const latestIntent = computed(() => intents.value?.[0])
const latestLoopRun = computed(() => loopRuns.value?.[0] ?? loopStatus.value?.last_run ?? null)
const loopMode = computed(() => {
  if (loopStatus.value?.loop_enabled) return 'AUTONOMOUS'
  return 'MANUAL TICK'
})
const loopGateNote = computed(() => {
  if (!loopStatus.value) return 'The loop service has not answered yet.'
  if (loopStatus.value.system_state !== 'ACTIVE') return `The system is ${loopStatus.value.system_state}; the loop refuses new exposure until it is ACTIVE.`
  if (loopStatus.value.broker_connection !== 'CONNECTED') return 'The practice broker is not connected; the loop stays fail-closed.'
  if (!loopStatus.value.practice_execution_enabled) return 'Signals become risk-gated intents. Submission stays disabled by local configuration.'
  return 'Signals become risk-gated intents and approved practice orders are submitted.'
})
const hasAdminToken = computed(() => adminToken.value.trim().length > 0)

// --- Operational alerts (loop guards and tick failures) -----------------
const unacknowledgedAlerts = computed(() => alertData.value?.unacknowledged ?? 0)
const visibleAlerts = computed(() => alertData.value?.alerts?.slice(0, 4) ?? [])
const ackError = ref<string | null>(null)
const ackingId = ref<number | null>(null)
const ackingAll = ref(false)

async function acknowledgeAlert(alert: AlertRow) {
  const token = adminToken.value.trim()
  if (!token) { ackError.value = 'The local admin token is required to acknowledge alerts. Set it on the Local setup page first.'; return }
  ackingId.value = alert.id
  ackError.value = null
  try {
    await api.ackAlert(token, alert.id)
    await refreshAlerts()
  } catch (error) {
    ackError.value = error instanceof Error ? error.message : 'The alert could not be acknowledged.'
  } finally {
    ackingId.value = null
  }
}

async function acknowledgeAllAlerts() {
  const token = adminToken.value.trim()
  if (!token) { ackError.value = 'The local admin token is required to acknowledge alerts. Set it on the Local setup page first.'; return }
  ackingAll.value = true
  ackError.value = null
  try {
    await api.ackAllAlerts(token)
    await refreshAlerts()
  } catch (error) {
    ackError.value = error instanceof Error ? error.message : 'The alerts could not be acknowledged.'
  } finally {
    ackingAll.value = false
  }
}

const systemState = computed(() => state.value?.system_state ?? 'PAUSED')

async function toggleExposure() {
  // The control is state-aware: ACTIVE pauses, PAUSED resumes (the backend
  // answers 409 when the practice broker is not connected or the runtime is
  // HALTED), HALTED is a latched service-restart state and the button says so.
  if (systemState.value === 'HALTED') return
  isTogglingExposure.value = true
  exposureError.value = null
  try {
    if (systemState.value === 'PAUSED') await api.resume()
    else await api.pause()
    await Promise.all([refreshState(), refreshReconciliations(), refreshLoopStatus(), refreshLoopRuns()])
  } catch (error) {
    const detail = (error as { data?: { detail?: string } })?.data?.detail
    exposureError.value = detail ?? (error instanceof Error ? error.message : 'The exposure request could not be confirmed.')
  } finally {
    isTogglingExposure.value = false
  }
}

function loopTickLabel(run: LoopRun | null): string {
  if (!run) return '—'
  if (run.state === 'FAILED') return 'FAILED'
  if (run.summary?.skipped) return 'SKIPPED'
  const signals = run.summary?.signals?.length ?? 0
  const created = run.summary?.intents_created?.length ?? 0
  return `${signals} SIGNAL${signals === 1 ? '' : 'S'} · ${created} INTENT${created === 1 ? '' : 'S'}`
}

async function runLoopTick() {
  if (!hasAdminToken.value) {
    tickError.value = 'The local admin token is missing. Set it on the Local setup page first.'
    return
  }
  isTicking.value = true
  tickError.value = null
  tickNotice.value = null
  try {
    const run = await api.runLoopTick(adminToken.value.trim())
    if (run.state === 'SUCCEEDED') {
      tickNotice.value = run.summary?.skipped
        ? `Tick skipped — ${run.summary?.reason ?? 'guards are not satisfied'}.`
        : `Tick completed — ${run.summary?.signals?.length ?? 0} signal(s), ${run.summary?.intents_created?.length ?? 0} intent(s), ${run.summary?.intents_submitted ?? 0} submitted.`
    } else {
      tickError.value = run.error_message ?? 'The loop tick failed; the system remains fail-closed.'
    }
    await Promise.all([refreshLoopStatus(), refreshLoopRuns(), refreshState(), refreshIntents(), refreshEvents(), refreshReconciliations()])
  } catch (error) {
    tickError.value = error instanceof Error ? error.message : 'The loop tick could not be confirmed.'
  } finally {
    isTicking.value = false
  }
}

onMounted(() => {
  adminToken.value = window.sessionStorage.getItem('tradingos-local-admin-token') ?? ''
  window.setInterval(() => { now.value = new Date() }, 30_000)
  if (auth.session.value === null) void auth.bootstrap()
  if (!selectedPair.value) {
    const first = chartPairs.value[0]
    if (first) {
      selectedPair.value = pairKey(first.symbol, first.timeframe)
      void loadChart()
    }
  }
})

// Live channel: one inbound event is enough to refresh the affected panels.
// The page never waits for its 30s poll to learn that a tick settled a trade.
watch(socketEvents, (list) => {
  const latest = list[0]
  if (!latest) return
  if (latest.type === 'loop.tick.started') {
    tickNotice.value = 'A loop tick is running — reconciling, watching, gating…'
    return
  }
  if (latest.type === 'loop.tick.completed' || latest.type === 'loop.tick.failed') {
    tickNotice.value = null
    void Promise.all([refreshLoopStatus(), refreshLoopRuns(), refreshState(), refreshIntents(), refreshEvents(), refreshReconciliations()])
    if (latest.type === 'loop.tick.completed') void loadChart()
    return
  }
  if (latest.type === 'loop.signal') {
    const payloadSymbol = typeof latest.payload.symbol === 'string' ? latest.payload.symbol : null
    const payloadTimeframe = typeof latest.payload.timeframe_seconds === 'number' ? latest.payload.timeframe_seconds : null
    if (payloadSymbol && payloadTimeframe && selectedPair.value === pairKey(payloadSymbol, payloadTimeframe)) void loadChart()
  }
  if (latest.type === 'alert.raised' || latest.type === 'alert.acknowledged') {
    void refreshAlerts()
    if (latest.type === 'alert.raised') void Promise.all([refreshLoopStatus(), refreshLoopRuns(), refreshEvents()])
    return
  }
  if (latest.type === 'execution.trade.settled' || latest.type === 'execution.order.submitted' || latest.type === 'system.state_changed' || latest.type === 'reconciliation.completed') {
    void Promise.all([refreshState(), refreshIntents(), refreshEvents(), refreshLoopStatus()])
  }
})

// --- Watchlist editor ---------------------------------------------------
// The PUT contract is replace-all, so the editor keeps a local draft of the
// whole observation set and submits it in one shot on save. No admin token:
// watchlist shaping is a local-first control by design.
interface WatchDraftItem { symbol: string; category: string; timeframe_seconds: number; enabled: boolean }
const SYMBOL_PATTERN = /^[A-Za-z0-9._/-]+$/
const watchEditing = ref(false)
const watchSaving = ref(false)
const watchError = ref<string | null>(null)
const watchDraft = ref<WatchDraftItem[]>([])
const watchNewSymbol = ref('')
const watchNewTimeframe = ref(60)
const watchNewCategory = ref('forex')

function startWatchEdit() {
  watchDraft.value = (watchlist.value ?? []).map((item) => ({ symbol: item.symbol, category: item.category, timeframe_seconds: item.timeframe_seconds, enabled: item.enabled }))
  watchNewSymbol.value = ''
  watchNewTimeframe.value = 60
  watchNewCategory.value = 'forex'
  watchError.value = null
  watchEditing.value = true
}

function addWatchDraft() {
  const symbol = watchNewSymbol.value.trim().toUpperCase()
  const timeframe = Math.floor(Number(watchNewTimeframe.value))
  if (!symbol) { watchError.value = 'A symbol is required before it can be observed.'; return }
  if (!SYMBOL_PATTERN.test(symbol)) { watchError.value = 'Symbols may only use letters, digits, dots, dashes, slashes, and underscores.'; return }
  if (!Number.isFinite(timeframe) || timeframe < 1 || timeframe > 86_400) { watchError.value = 'Timeframe must be between 1 and 86400 seconds.'; return }
  if (watchDraft.value.some((item) => item.symbol === symbol && item.timeframe_seconds === timeframe)) {
    watchError.value = `${symbol} on the ${timeframe}s timeframe is already in this draft.`
    return
  }
  if (watchDraft.value.length >= 50) { watchError.value = 'The control plane accepts at most 50 watchlist entries.'; return }
  watchError.value = null
  watchDraft.value.push({ symbol, category: watchNewCategory.value.trim().toLowerCase() || 'forex', timeframe_seconds: timeframe, enabled: true })
  watchNewSymbol.value = ''
}

function removeWatchDraft(index: number) {
  watchDraft.value.splice(index, 1)
}

function cancelWatchEdit() {
  watchEditing.value = false
  watchError.value = null
}

async function saveWatchEdit() {
  watchSaving.value = true
  watchError.value = null
  try {
    await api.updateWatchlist({ items: watchDraft.value.map((item) => ({ ...item })) })
    await Promise.all([refreshWatchlist(), refreshEvents()])
    watchEditing.value = false
  } catch (error) {
    const detail = (error as { data?: { detail?: string } })?.data?.detail
    watchError.value = detail ?? 'The control plane rejected the watchlist. Nothing was changed.'
  } finally {
    watchSaving.value = false
  }
}

const socketChipLabel = computed(() => {
  if (socketStatus.value === 'live') return 'LIVE'
  if (socketStatus.value === 'connecting') return 'LINKING…'
  return 'POLLING'
})

function socketEventNote(type: string, payload: Record<string, unknown>): string {
  if (type === 'loop.tick.completed') {
    const summary = (payload.summary ?? {}) as Record<string, unknown>
    if (summary.skipped) return String(summary.reason ?? 'guards not satisfied')
    const signals = Array.isArray(summary.signals) ? summary.signals.length : 0
    const intents = Array.isArray(summary.intents_created) ? summary.intents_created.length : 0
    return `${signals} signal(s) · ${intents} intent(s) · ${summary.intents_submitted ?? 0} submitted`
  }
  if (type === 'loop.tick.failed') return String(payload.error ?? 'tick failed')
  if (type === 'loop.intent.created') return `#${payload.intent_id} ${payload.symbol ?? ''} ${payload.side ?? ''} → ${payload.status ?? ''}`
  if (type === 'loop.signal') return `${payload.symbol ?? ''} ${payload.signal ?? ''} on ${payload.timeframe_seconds ?? '?'}s`
  if (type === 'execution.order.submitted') return `order #${payload.order_id} ${payload.symbol ?? ''} ${payload.side ?? ''} $${Number(payload.amount ?? 0).toFixed(2)}`
  if (type === 'execution.trade.settled') return `${payload.symbol ?? '—'} settled ${payload.outcome ?? ''} ${Number(payload.pnl ?? 0).toFixed(2)}`
  if (type === 'reconciliation.completed') return `run #${payload.run_id} ${payload.state ?? ''}`
  if (type === 'reconciliation.failed') return String(payload.error ?? 'reconciliation failed')
  if (type === 'system.state_changed') return `system is now ${payload.system_state ?? '?'}`
  if (type === 'alert.raised') return `${payload.code ?? 'alert'} ×${payload.occurrences ?? 1} — ${payload.message ?? ''}`
  if (type === 'alert.acknowledged') return `alert${Array.isArray(payload.alert_ids) && payload.alert_ids.length > 1 ? 's' : ''} ${payload.auto ? 'auto-resolved' : 'acknowledged'}`
  return ''
}
</script>

<template>
  <div class="instrument-shell">
    <aside class="rail" aria-label="TradingOS navigation">
      <div class="brand-lockup">
        <img class="brand-mark" :src="visualAssets.logo" alt="TradingOS instrument symbol">
        <div>
          <p class="mono micro">AUTONOMOUS CONTROL</p>
          <p class="brand-name">TradingOS</p>
        </div>
      </div>

      <nav class="rail-nav" aria-label="Primary">
        <a class="nav-link nav-link--active" href="#overview"><span>01</span> Overview</a>
        <a class="nav-link" href="#watchlist"><span>02</span> Watchlist</a>
        <a class="nav-link" href="#risk"><span>03</span> Risk policy</a>
        <a class="nav-link" href="#research"><span>04</span> Research</a>
        <a class="nav-link" href="#loop"><span>05</span> Strategy loop</a>
        <NuxtLink class="nav-link" to="/alerts"><span>06</span> Alert center<span v-if="unacknowledgedAlerts" class="alert-badge" :title="`${unacknowledgedAlerts} unacknowledged alert(s)`">{{ unacknowledgedAlerts > 9 ? '9+' : unacknowledgedAlerts }}</span></NuxtLink>
        <NuxtLink class="nav-link" to="/strategies"><span>07</span> Strategy desk</NuxtLink>
        <NuxtLink class="nav-link" to="/analytics"><span>08</span> Outcome analytics</NuxtLink>
        <NuxtLink class="nav-link" to="/compare"><span>09</span> Strategy compare</NuxtLink>
        <NuxtLink class="nav-link" to="/backtest"><span>10</span> Backtest lab</NuxtLink>
        <a class="nav-link" href="#evidence"><span>11</span> Evidence log</a>
        <NuxtLink class="nav-link" to="/setup"><span>12</span> Local setup</NuxtLink>
      </nav>

      <div class="rail-foot">
        <div v-if="auth.isRemoteGated.value" class="rail-session">
          <p class="mono micro">REMOTE SESSION</p>
          <p class="rail-session-state" :class="auth.isAuthenticated.value ? 'pos' : 'neg'">
            {{ auth.isAuthenticated.value ? `OPEN · EXPIRES ${auth.session.value?.expires_at ? new Date(auth.session.value.expires_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—'}` : 'CLOSED' }}
          </p>
          <button v-if="auth.isAuthenticated.value" class="mini-control mini-control--danger" type="button" @click="auth.logout()">SIGN OUT</button>
          <NuxtLink v-else class="mini-control" to="/login">SIGN IN</NuxtLink>
        </div>
        <p class="mono micro">SYSTEM CONTRACT</p>
        <p>Local-first. Practice controls are the only execution surface.</p>
      </div>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <div>
          <p class="mono micro">CONTROL PLANE / {{ now.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }).toUpperCase() }}</p>
          <h1>Capital requires a quiet system.</h1>
        </div>
        <div class="topbar-status">
          <NuxtLink class="setup-link" to="/setup">LOCAL SETUP</NuxtLink>
          <span class="status-dot"></span>
          <span class="mono">{{ sourceStatus }}</span>
        </div>
      </header>

      <section id="overview" class="panorama">
        <div class="panorama-copy">
          <p class="mono eyebrow">OPERATING MODE</p>
          <div class="mode-line">
            <span class="mode-sigil"></span>
            <h2>{{ state?.account_mode ?? 'PRACTICE' }}</h2>
          </div>
          <p class="panorama-note">{{ state?.real_execution_enabled ? 'Real execution state requires investigation.' : 'Live execution remains hard-locked. Local practice controls become available only after a verified broker reconciliation.' }}</p>
          <div class="calibration"></div>
          <dl class="instrument-readout">
            <div><dt>RUNTIME</dt><dd>{{ runtime }}</dd></div>
            <div><dt>OPEN EXPOSURE</dt><dd>{{ state?.open_orders ?? 0 }}</dd></div>
            <div><dt>WATCHING</dt><dd>{{ state?.active_watchlist_items ?? 0 }} PAIRS</dd></div>
          </dl>
        </div>
        <div class="signal-field" aria-hidden="true">
          <img class="market-intelligence-art" :src="visualAssets.hero" alt="">
          <div class="signal-orbit signal-orbit--one"></div>
          <div class="signal-orbit signal-orbit--two"></div>
          <div class="signal-path"><span></span><span></span><span></span><span></span></div>
          <p class="mono">EVIDENCE BEFORE ACTION</p>
        </div>
      </section>

      <div class="instrument-grid">
        <section id="risk" class="panel panel--risk">
          <div class="panel-heading">
            <div>
              <p class="mono eyebrow">RISK INSTRUMENT</p>
              <h3>Bounded by policy.</h3>
            </div>
            <span class="panel-index">R-01</span>
          </div>
          <div v-if="risk" class="risk-figures">
            <div><span>PER TRADE</span><strong>{{ (risk.max_risk_fraction * 100).toFixed(2) }}%</strong><small>balance at risk</small></div>
            <div><span>DAY LIMIT</span><strong>{{ (risk.max_daily_loss_fraction * 100).toFixed(1) }}%</strong><small>loss threshold</small></div>
            <div><span>MAX AMOUNT</span><strong>${{ risk.max_trade_amount.toFixed(2) }}</strong><small>practice ceiling</small></div>
          </div>
          <p v-else class="quiet-note">Risk policy has not yet been read from the FastAPI service.</p>
          <p v-if="riskError" class="error-note">The risk policy could not be loaded. New exposure should remain disabled.</p>
        </section>

        <section id="watchlist" class="panel panel--watchlist">
          <div class="panel-heading">
            <div>
              <p class="mono eyebrow">OBSERVATION SET</p>
              <h3>Watchlist</h3>
            </div>
            <span class="panel-index">W-02</span>
          </div>

          <template v-if="watchEditing">
            <div class="watch-editor">
              <p class="watch-editor-hint">Replaces the whole observation set in one commit. Disabled pairs stay listed but the worker skips them.</p>
              <div v-for="(item, index) in watchDraft" :key="`${item.symbol}-${item.timeframe_seconds}`" class="watch-editor-row">
                <strong>{{ item.symbol }}</strong>
                <input v-model="item.category" type="text" maxlength="40" aria-label="category">
                <input v-model.number="item.timeframe_seconds" type="number" min="1" max="86400" aria-label="timeframe seconds">
                <div class="watch-editor-tools">
                  <label class="watch-editor-toggle" title="Enabled pairs are observed by the worker"><input v-model="item.enabled" type="checkbox">ON</label>
                  <button class="mini-control mini-control--danger" type="button" :disabled="watchSaving" @click="removeWatchDraft(index)">DROP</button>
                </div>
              </div>
              <p v-if="!watchDraft.length" class="watch-editor-hint">The draft is empty — the worker will have nothing to observe.</p>
              <div class="watch-editor-add">
                <input v-model="watchNewSymbol" type="text" placeholder="EURUSD" maxlength="40" aria-label="new symbol" @keyup.enter="addWatchDraft">
                <input v-model="watchNewCategory" type="text" placeholder="forex" maxlength="40" aria-label="new category">
                <input v-model.number="watchNewTimeframe" type="number" min="1" max="86400" aria-label="new timeframe seconds">
                <div class="watch-editor-tools">
                  <button class="mini-control" type="button" :disabled="watchSaving" @click="addWatchDraft">ADD PAIR</button>
                </div>
              </div>
              <p v-if="watchError" class="error-note watch-editor-error">{{ watchError }}</p>
              <div class="watch-editor-actions">
                <button class="mini-control" type="button" :disabled="watchSaving" @click="saveWatchEdit">{{ watchSaving ? 'COMMITTING…' : 'COMMIT WATCHLIST' }}</button>
                <button class="mini-control" type="button" :disabled="watchSaving" @click="cancelWatchEdit">CANCEL</button>
              </div>
            </div>
          </template>

          <div v-else-if="watchlist?.length" class="watchlist">
            <div v-for="item in watchlist" :key="item.id" class="watch-item">
              <span class="watch-led" :class="{ 'watch-led--off': !item.enabled }"></span>
              <strong>{{ item.symbol }}</strong>
              <span>{{ item.category }}</span>
              <span class="mono">{{ item.timeframe_seconds }}S</span>
            </div>
            <div class="watch-manage">
              <button class="mini-control" type="button" @click="startWatchEdit">MANAGE OBSERVATION SET</button>
            </div>
          </div>

          <div v-else class="empty-watchlist">
            <span class="empty-glyph">+</span>
            <p>No pairs are selected yet. Open the editor and add the first observation before market intelligence begins.</p>
            <div class="watch-manage">
              <button class="mini-control" type="button" @click="startWatchEdit">OPEN THE WATCHLIST EDITOR</button>
            </div>
          </div>
        </section>
      </div>

      <section id="research" class="operations-grid">
        <section class="panel operations-panel">
          <div class="panel-heading">
            <div>
              <p class="mono eyebrow">BROKER OBSERVATION</p>
              <h3>Reconciliation ledger</h3>
            </div>
            <span :class="['state-chip', `state-chip--${latestReconciliation?.state?.toLowerCase() ?? 'waiting'}`]">{{ latestReconciliation?.state ?? 'WAITING' }}</span>
          </div>
          <div class="operations-body">
            <dl class="compact-readout">
              <div><dt>LAST RUN</dt><dd>{{ latestReconciliation?.finished_at ? new Date(latestReconciliation.finished_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—' }}</dd></div>
              <div><dt>CANDLES</dt><dd>{{ latestReconciliation?.summary?.candles_ingested ?? 0 }}</dd></div>
              <div><dt>POSITIONS</dt><dd>{{ positions?.length ?? 0 }}</dd></div>
            </dl>
            <p class="quiet-note">{{ latestReconciliation?.error_message ?? 'Connection, balance, assets, candles, and positions are persisted before any strategy path can consider new exposure.' }}</p>
          </div>
        </section>

        <section class="panel operations-panel">
          <div class="panel-heading">
            <div>
              <p class="mono eyebrow">VALIDATION DESK</p>
              <h3>Strategies & research</h3>
            </div>
            <span class="panel-index">S-03</span>
          </div>
          <div class="operations-body">
            <dl class="compact-readout">
              <div><dt>VERSIONS</dt><dd>{{ strategies?.length ?? 0 }}</dd></div>
              <div><dt>VALIDATED</dt><dd>{{ validatedStrategies }}</dd></div>
              <div><dt>AI RUNS</dt><dd>{{ researchRuns?.length ?? 0 }}</dd></div>
            </dl>
            <div v-if="latestResearch" class="research-note">
              <span class="mono">{{ latestResearch.output?.disposition ?? latestResearch.status }}</span>
              <p>{{ latestResearch.output?.thesis ?? 'The most recent research workflow has no persisted thesis.' }}</p>
            </div>
            <p v-else class="quiet-note">AI research is opt-in, budgeted, structured, and unable to invoke broker or order operations.</p>
          </div>
        </section>

        <section class="panel operations-panel operations-panel--wide">
          <div class="panel-heading">
            <div>
              <p class="mono eyebrow">IDEMPOTENT GATE</p>
              <h3>Practice intent ledger</h3>
            </div>
            <span class="panel-index">E-04</span>
          </div>
          <div class="intent-table">
            <div class="intent-row intent-row--head"><span>TIME</span><span>SYMBOL</span><span>STRATEGY</span><span>AMOUNT</span><span>STATE</span></div>
            <div v-for="intent in intents?.slice(0, 5)" :key="intent.id" class="intent-row">
              <span class="mono">{{ new Date(intent.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}</span>
              <span>{{ intent.symbol }} · {{ intent.side }}</span>
              <span>{{ intent.idempotency_key?.startsWith('loop:') ? 'AUTO' : 'MANUAL' }} #{{ intent.strategy_version_id ?? '—' }}</span>
              <span>${{ intent.requested_amount.toFixed(2) }}</span>
              <span :class="['severity', `severity--${intent.status.toLowerCase()}`]">{{ intent.status }}</span>
            </div>
            <div v-if="!intents?.length" class="intent-row intent-row--empty"><span>—</span><span>NO INTENTS</span><span>Risk authorization creates a persisted intent before any practice submission is possible.</span><span>—</span><span>LOCKED</span></div>
          </div>
          <p class="ledger-contract">An intent is not an order. Practice submission is separately disabled unless the local configuration explicitly enables it after verified reconciliation.</p>
        </section>

        <section id="loop" class="panel operations-panel operations-panel--wide">
          <div class="panel-heading">
            <div>
              <p class="mono eyebrow">STRATEGY → INTENT → EXECUTION</p>
              <h3>Autonomous practice loop</h3>
            </div>
            <span :class="['state-chip', `state-chip--${latestLoopRun?.state?.toLowerCase() ?? 'waiting'}`]">{{ latestLoopRun?.state ?? 'WAITING' }} · {{ loopMode }}</span>
            <span :class="['live-chip', isConnected ? 'live-chip--on' : 'live-chip--off']" :title="`websocket ${socketStatus}`">{{ socketChipLabel }}</span>
          </div>
          <div class="operations-body">
            <dl class="compact-readout">
              <div><dt>LAST TICK</dt><dd>{{ latestLoopRun?.finished_at ? new Date(latestLoopRun.finished_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—' }}</dd></div>
              <div><dt>OUTCOME</dt><dd>{{ loopTickLabel(latestLoopRun) }}</dd></div>
              <div><dt>SUBMITTED</dt><dd>{{ latestLoopRun?.summary?.intents_submitted ?? 0 }} ORDERS</dd></div>
            </dl>
            <p class="quiet-note">{{ loopGateNote }}</p>
            <p v-if="latestLoopRun?.error_message" class="error-note">{{ latestLoopRun.error_message }}</p>
            <p v-if="tickError" class="error-note">{{ tickError }}</p>
            <p v-if="tickNotice" class="loop-notice">{{ tickNotice }}</p>
            <div class="loop-actions">
              <button class="run-control" type="button" :disabled="isTicking || !hasAdminToken" @click="runLoopTick">
                <span></span>{{ isTicking ? 'RUNNING TICK…' : 'RUN ONE LOOP TICK' }}
              </button>
              <span v-if="!hasAdminToken" class="mono loop-hint">SET THE ADMIN TOKEN ON THE LOCAL SETUP PAGE TO DRIVE THE LOOP</span>
            </div>
          </div>
          <div class="market-tape">
            <div class="tape-head">
              <p class="mono micro">MARKET TAPE · SIGNAL MARKERS</p>
              <div class="tape-controls">
                <select v-model="selectedPair" class="tape-select" aria-label="Chart pair" @change="loadChart">
                  <option v-for="pair in chartPairs" :key="pairKey(pair.symbol, pair.timeframe)" :value="pairKey(pair.symbol, pair.timeframe)">{{ pair.label }}</option>
                </select>
                <button class="mini-control" type="button" :disabled="chartLoading" @click="loadChart">{{ chartLoading ? 'LOADING…' : 'REFRESH' }}</button>
              </div>
            </div>
            <p v-if="chartError" class="error-note tape-note">{{ chartError }}</p>
            <CandleChart
              v-if="chart?.candles?.length"
              :candles="chart.candles"
              :markers="chart.markers"
            />
            <p v-if="chart && chart.markers.length === 0 && chart.candles.length" class="quiet-note tape-note">No loop signals on this pair yet — markers appear the moment the loop gates a CALL or PUT.</p>
          </div>
          <div class="loop-runs">
            <div class="loop-row loop-row--head"><span>TIME</span><span>STATE</span><span>SIGNALS</span><span>INTENTS</span><span>SUBMITTED</span><span>NOTE</span></div>
            <div v-for="run in loopRuns?.slice(0, 5)" :key="run.id" class="loop-row">
              <span class="mono">{{ run.finished_at ? new Date(run.finished_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '…' }}</span>
              <span :class="['severity', `severity--${run.state.toLowerCase()}`]">{{ run.state }}</span>
              <span>{{ run.summary?.signals?.length ?? 0 }}</span>
              <span>{{ run.summary?.intents_created?.length ?? 0 }}</span>
              <span>{{ run.summary?.intents_submitted ?? 0 }}</span>
              <span class="loop-note">{{ run.error_message ?? run.summary?.reason ?? (run.summary?.skipped ? 'guards not satisfied' : 'clean pass') }}</span>
            </div>
            <div v-if="!loopRuns?.length" class="loop-row loop-row--empty"><span>—</span><span>NO TICKS</span><span>—</span><span>—</span><span>—</span><span>The loop has not run yet in this session.</span></div>
          </div>
          <div class="guard-alerts" aria-label="Operational alerts">
            <div class="guard-alerts-head">
              <p class="mono micro ws-feed-title">OPERATIONAL ALERTS<span v-if="unacknowledgedAlerts" class="alert-count">{{ unacknowledgedAlerts > 9 ? '9+' : unacknowledgedAlerts }}</span></p>
              <NuxtLink class="mini-control" to="/alerts">ALERT CENTER</NuxtLink>
              <button v-if="unacknowledgedAlerts" class="mini-control" type="button" :disabled="ackingAll" @click="acknowledgeAllAlerts">{{ ackingAll ? 'CLEARING…' : 'ACK ALL' }}</button>
            </div>
            <div v-for="alert in visibleAlerts" :key="alert.id" :class="['alert-item', { 'alert-item--acked': alert.acknowledged }]">
              <span :class="['mono', 'alert-sev', `alert-sev--${alert.severity.toLowerCase()}`]">{{ alert.severity }}</span>
              <div class="alert-copy">
                <span class="mono alert-code">{{ alert.code }}<template v-if="alert.occurrences > 1"> ×{{ alert.occurrences }}</template></span>
                <span class="alert-message">{{ alert.message }}</span>
              </div>
              <button v-if="!alert.acknowledged" class="mini-control" type="button" :disabled="ackingId === alert.id" @click="acknowledgeAlert(alert)">{{ ackingId === alert.id ? '…' : 'ACK' }}</button>
              <span v-else class="mono micro alert-acked-note">ACKED</span>
            </div>
            <p v-if="!visibleAlerts.length" class="quiet-note alert-empty">No alerts. Guards report through this panel the moment a tick is skipped, a submission fails, or the loop breaks.</p>
            <p v-if="ackError" class="error-note">{{ ackError }}</p>
          </div>
          <div class="ws-feed" aria-label="Live loop event stream">
            <p class="mono micro ws-feed-title">LIVE EVENT STREAM<span :class="['ws-dot', isConnected ? 'ws-dot--on' : 'ws-dot--off']"></span></p>
            <div v-for="(event, index) in socketEvents.slice(0, 6)" :key="`${event.ts}-${index}`" class="ws-row">
              <span class="mono ws-time">{{ new Date(event.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) }}</span>
              <span class="mono ws-type">{{ event.type }}</span>
              <span class="ws-note">{{ socketEventNote(event.type, event.payload) }}</span>
            </div>
            <p v-if="!socketEvents.length" class="ws-row ws-row--empty"><span class="mono ws-time">—</span><span class="mono ws-type">IDLE</span><span class="ws-note">{{ isConnected ? 'Channel is open; waiting for the next loop or settlement event.' : 'The live channel is offline — panels fall back to manual refresh.' }}</span></p>
          </div>
          <p class="ledger-contract">Every tick reconciles first, computes the EMA-cross signal on the latest closed candle, and routes it through the same risk gate as manual intents. One candle can produce one intent — never two.</p>
        </section>
      </section>

      <section id="evidence" class="evidence panel">
        <div class="panel-heading">
          <div>
            <p class="mono eyebrow">APPEND-ONLY RECORD</p>
            <h3>Evidence log</h3>
          </div>
          <button class="pause-control" :class="{ 'pause-control--resume': systemState === 'PAUSED', 'pause-control--halted': systemState === 'HALTED' }" type="button" :disabled="isTogglingExposure || statePending || systemState === 'HALTED'" :title="systemState === 'HALTED' ? 'HALTED is latched fail-closed: restart the TradingOS service to re-arm the runtime.' : systemState === 'PAUSED' ? 'Resume requires a connected practice broker.' : 'Pause rejects every new intent until resumed.'" @click="toggleExposure">
            <span></span>{{ isTogglingExposure ? 'CONFIRMING…' : systemState === 'HALTED' ? 'HALTED — RESTART SERVICE' : systemState === 'PAUSED' ? 'RESUME NEW EXPOSURE' : 'PAUSE NEW EXPOSURE' }}
          </button>
        </div>
        <p v-if="exposureError" class="error-note">{{ exposureError }}</p>
        <p v-if="stateError" class="error-note">The system state is unavailable. The interface assumes a halt until it can be confirmed.</p>
        <div class="event-table" role="table" aria-label="System evidence log">
          <div class="event-row event-row--head" role="row"><span>TIME</span><span>EVENT</span><span>INTERPRETATION</span><span>SEVERITY</span></div>
          <div v-for="event in events?.slice(0, 5)" :key="event.id" class="event-row" role="row">
            <span class="mono">{{ new Date(event.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}</span>
            <span class="event-type">{{ event.event_type.replaceAll('_', ' ') }}</span>
            <span>{{ event.message }}</span>
            <span :class="['severity', `severity--${event.severity.toLowerCase()}`]">{{ event.severity }}</span>
          </div>
          <div v-if="!events?.length" class="event-row event-row--empty"><span>—</span><span>NO EVENTS</span><span>The audit journal will appear after the backend has initialized.</span><span>INFO</span></div>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.watch-manage { display: flex; justify-content: flex-end; padding: 4px 6px 12px; border-top: 1px solid var(--line); }
.empty-watchlist .watch-manage { justify-content: flex-start; border-top: 0; padding: 14px 0 0; }
.watch-editor { display: grid; gap: 10px; padding: 6px 16px 18px; }
.watch-editor-hint { margin: 0; color: var(--quiet); font-size: 11px; line-height: 1.6; }
.watch-editor-row, .watch-editor-add { display: grid; grid-template-columns: 76px minmax(56px, 1fr) 64px auto; gap: 6px; align-items: center; padding: 9px 0; border-top: 1px solid var(--line); }
.watch-editor-row strong, .watch-editor-add input[aria-label="new symbol"] { font-family: 'DM Mono', monospace; font-weight: 500; font-size: 12px; letter-spacing: .04em; }
.watch-editor-row input, .watch-editor-add input { background: rgba(8, 10, 10, .8); border: 1px solid var(--line); color: var(--paper); font: 11px 'DM Mono', monospace; letter-spacing: .04em; padding: 7px 8px; outline: none; min-width: 0; width: 100%; box-sizing: border-box; }
.watch-editor-row input:focus, .watch-editor-add input:focus { outline: 1px solid rgba(184, 154, 106, .55); outline-offset: 1px; }
.watch-editor-tools { display: inline-flex; gap: 6px; align-items: center; justify-content: flex-end; }
.watch-editor-toggle { display: inline-flex; gap: 5px; align-items: center; color: var(--quiet); font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: .06em; white-space: nowrap; cursor: pointer; }
.watch-editor-toggle input { width: auto; accent-color: #83bbb0; }
.watch-editor-add { border-top: 0; padding-top: 2px; }
.watch-editor-error { margin: 0; }
.watch-editor-actions { display: flex; gap: 10px; justify-content: flex-end; }
@media (max-width: 620px) { .watch-editor-row, .watch-editor-add { grid-template-columns: 1fr 1fr; } .watch-editor-actions { justify-content: stretch; } }
</style>
