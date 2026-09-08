<!-- TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design. -->
<script setup lang="ts">
import type { AuditEvent, LoopRun, LoopStatus, OrderIntent, PositionSnapshot, ReconciliationRun, ResearchRun, RiskPolicy, StrategyVersion, SystemState, WatchlistItem } from '~/types/trading'

const api = useTradingApi()
const { status: socketStatus, events: socketEvents, isConnected } = useLoopSocket()
const isPausing = ref(false)
const pauseError = ref<string | null>(null)
const isTicking = ref(false)
const tickError = ref<string | null>(null)
const tickNotice = ref<string | null>(null)
const adminToken = ref('')
const now = ref(new Date())
const visualAssets = {
  logo: '/tradingos-mark.svg',
  hero: '/tradingos-hero.svg',
}

const { data: state, pending: statePending, error: stateError, refresh: refreshState } = await useAsyncData<SystemState>('trading-state', api.getState)
const { data: risk, error: riskError } = await useAsyncData<RiskPolicy>('risk-policy', api.getRisk)
const { data: events, refresh: refreshEvents } = await useAsyncData<AuditEvent[]>('audit-events', api.getEvents)
const { data: watchlist } = await useAsyncData<WatchlistItem[]>('watchlist', api.getWatchlist)
const { data: strategies } = await useAsyncData<StrategyVersion[]>('strategies', api.getStrategies)
const { data: intents, refresh: refreshIntents } = await useAsyncData<OrderIntent[]>('order-intents', api.getOrderIntents)
const { data: positions } = await useAsyncData<PositionSnapshot[]>('positions', api.getPositions)
const { data: reconciliations, refresh: refreshReconciliations } = await useAsyncData<ReconciliationRun[]>('reconciliations', api.getReconciliationRuns)
const { data: researchRuns } = await useAsyncData<ResearchRun[]>('research-runs', api.getResearchRuns)
const { data: loopStatus, refresh: refreshLoopStatus } = await useAsyncData<LoopStatus>('loop-status', api.getLoopStatus)
const { data: loopRuns, refresh: refreshLoopRuns } = await useAsyncData<LoopRun[]>('loop-runs', api.getLoopRuns)

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

async function pauseSystem() {
  isPausing.value = true
  pauseError.value = null
  try {
    await api.pause()
    await Promise.all([refreshState(), refreshReconciliations(), refreshLoopStatus(), refreshLoopRuns()])
  } catch (error) {
    pauseError.value = error instanceof Error ? error.message : 'The pause request could not be confirmed.'
  } finally {
    isPausing.value = false
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
    return
  }
  if (latest.type === 'execution.trade.settled' || latest.type === 'execution.order.submitted' || latest.type === 'system.state_changed' || latest.type === 'reconciliation.completed') {
    void Promise.all([refreshState(), refreshIntents(), refreshEvents(), refreshLoopStatus()])
  }
})

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
        <NuxtLink class="nav-link" to="/strategies"><span>06</span> Strategy desk</NuxtLink>
        <NuxtLink class="nav-link" to="/analytics"><span>07</span> Outcome analytics</NuxtLink>
        <a class="nav-link" href="#evidence"><span>08</span> Evidence log</a>
        <NuxtLink class="nav-link" to="/setup"><span>09</span> Local setup</NuxtLink>
      </nav>

      <div class="rail-foot">
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
          <div v-if="watchlist?.length" class="watchlist">
            <div v-for="item in watchlist" :key="item.id" class="watch-item">
              <span class="watch-led" :class="{ 'watch-led--off': !item.enabled }"></span>
              <strong>{{ item.symbol }}</strong>
              <span>{{ item.category }}</span>
              <span class="mono">{{ item.timeframe_seconds }}S</span>
            </div>
          </div>
          <div v-else class="empty-watchlist">
            <span class="empty-glyph">+</span>
            <p>No pairs are selected. Add a practice watchlist through the control-plane API before market observation begins.</p>
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
          <button class="pause-control" type="button" :disabled="isPausing || statePending" @click="pauseSystem">
            <span></span>{{ isPausing ? 'CONFIRMING…' : 'PAUSE NEW EXPOSURE' }}
          </button>
        </div>
        <p v-if="pauseError" class="error-note">{{ pauseError }}</p>
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
