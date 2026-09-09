<!-- Design: The Instrument Room — alerts are operable pages, not decorations. Silence is a decision that must be visible. -->
<script setup lang="ts">
import type { AlertRow, AlertRule, WebhookDeliveryRow, WebhookPolicy } from '~/types/trading'

const api = useTradingApi()
const { isConnected, events } = useLoopSocket()

const adminToken = ref('')
const alerts = ref<AlertRow[]>([])
const unacknowledged = ref(0)
const rules = ref<AlertRule[]>([])
const deliveries = ref<WebhookDeliveryRow[]>([])
const policy = ref<WebhookPolicy | null>(null)
const loading = ref(true)
const listError = ref<string | null>(null)
const actionError = ref<string | null>(null)
const actionNotice = ref<string | null>(null)

// --- filters -------------------------------------------------------------
const severityFilter = ref<'ALL' | 'WARNING' | 'ERROR' | 'INFO'>('ALL')
const stateFilter = ref<'ALL' | 'OPEN'>('ALL')
const codeFilter = ref('')

const knownCodes = computed(() => Array.from(new Set(rules.value.map(rule => rule.code))).sort())

async function loadAlerts() {
  listError.value = null
  try {
    const body = await api.getAlertsFiltered({
      unacknowledged_only: stateFilter.value === 'OPEN',
      severity: severityFilter.value === 'ALL' ? undefined : severityFilter.value,
      code: codeFilter.value || undefined,
      limit: 100,
    })
    alerts.value = body.alerts
    unacknowledged.value = body.unacknowledged
  } catch (error) {
    listError.value = error instanceof Error ? error.message : 'The alert ledger could not be read from the local API.'
  }
}

async function loadRules() {
  try {
    rules.value = (await api.getAlertRules()).rules
  } catch {
    rules.value = []
  }
}

async function loadWebhook() {
  try {
    const [deliveryBody, policyBody] = await Promise.all([api.getWebhookDeliveries(30), api.getWebhookPolicy()])
    deliveries.value = deliveryBody.deliveries
    policy.value = policyBody
  } catch {
    deliveries.value = []
  }
}

async function refreshAll() {
  loading.value = true
  try {
    await Promise.all([loadAlerts(), loadRules(), loadWebhook()])
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  adminToken.value = window.sessionStorage.getItem('tradingos-local-admin-token') ?? ''
  void refreshAll()
})

watch([severityFilter, stateFilter, codeFilter], () => void loadAlerts())

// --- acknowledgement -----------------------------------------------------
const ackingId = ref<number | null>(null)
const ackingAll = ref(false)

function requireToken(): string | null {
  const token = adminToken.value.trim()
  if (!token) {
    actionError.value = 'The local admin token is required for alert actions. Set it on the Local setup page first.'
    return null
  }
  return token
}

async function acknowledgeAlert(alert: AlertRow) {
  const token = requireToken()
  if (!token) return
  ackingId.value = alert.id
  actionError.value = null
  try {
    await api.ackAlert(token, alert.id)
    await Promise.all([loadAlerts(), loadRules()])
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'The alert could not be acknowledged.'
  } finally {
    ackingId.value = null
  }
}

async function acknowledgeAll() {
  const token = requireToken()
  if (!token) return
  ackingAll.value = true
  actionError.value = null
  try {
    await api.ackAllAlerts(token)
    await loadAlerts()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'The alerts could not be acknowledged.'
  } finally {
    ackingAll.value = false
  }
}

// --- rules ---------------------------------------------------------------
type RuleDraft = { enabled: boolean; severity: string; cooldown_seconds: number | null; notify_webhook: boolean }
const ruleDrafts = ref<Record<string, RuleDraft>>({})
const savingRule = ref<string | null>(null)

function draftFor(rule: AlertRule): RuleDraft {
  if (!ruleDrafts.value[rule.code]) {
    ruleDrafts.value[rule.code] = { enabled: rule.enabled, severity: rule.severity, cooldown_seconds: rule.cooldown_seconds, notify_webhook: rule.notify_webhook }
  }
  return ruleDrafts.value[rule.code]!
}

async function saveRule(rule: AlertRule) {
  const token = requireToken()
  if (!token) return
  const draft = draftFor(rule)
  savingRule.value = rule.code
  actionError.value = null
  actionNotice.value = null
  try {
    await api.updateAlertRule(token, rule.code, {
      enabled: draft.enabled,
      severity: draft.severity,
      cooldown_seconds: draft.cooldown_seconds,
      notify_webhook: draft.notify_webhook,
    })
    actionNotice.value = `Rule ${rule.code} saved.`
    await loadRules()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : `Rule ${rule.code} could not be saved.`
  } finally {
    savingRule.value = null
  }
}

// --- webhook -------------------------------------------------------------
const sendingTest = ref(false)
const retryingId = ref<number | null>(null)

async function sendTest() {
  const token = requireToken()
  if (!token) return
  sendingTest.value = true
  actionError.value = null
  actionNotice.value = null
  try {
    const result = await api.sendTestWebhook(token)
    actionNotice.value = `TEST delivery #${result.delivery_id} queued for ${result.target_url}.`
    await loadWebhook()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'The TEST webhook could not be queued.'
  } finally {
    sendingTest.value = false
  }
}

async function retryDelivery(delivery: WebhookDeliveryRow) {
  const token = requireToken()
  if (!token) return
  retryingId.value = delivery.id
  actionError.value = null
  try {
    await api.retryWebhookDelivery(token, delivery.id)
    await loadWebhook()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : `Delivery #${delivery.id} could not be requeued.`
  } finally {
    retryingId.value = null
  }
}

// --- live refresh ---------------------------------------------------------
watch(events, (list) => {
  const latest = list[0]
  if (!latest) return
  if (latest.type === 'alert.raised' || latest.type === 'alert.acknowledged' || latest.type === 'loop.tick.completed') {
    void loadAlerts()
  }
  if (latest.type === 'webhook.delivered' || latest.type === 'webhook.exhausted' || latest.type === 'alert.raised') {
    void loadWebhook()
  }
})

useHead({ title: 'TradingOS · Alert Center' })

// --- formatting -----------------------------------------------------------
function when(value: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

const openPayload = ref<number | null>(null)
function togglePayload(id: number) {
  openPayload.value = openPayload.value === id ? null : id
}

function deliveryStatusTone(status: string): string {
  if (status === 'DELIVERED') return 'pos'
  if (status === 'EXHAUSTED') return 'neg'
  return 'warn'
}
</script>

<template>
  <div class="setup-shell">
    <header class="setup-topbar">
      <p class="mono micro">ALERT CENTER / GUARDS, TICKS, AND DELIVERIES</p>
      <div class="alerts-topbar-right">
        <span :class="['live-chip', isConnected ? 'live-chip--on' : 'live-chip--off']">{{ isConnected ? 'LIVE' : 'STATIC' }}</span>
        <NuxtLink class="return-link" to="/">&larr; CONTROL PLANE</NuxtLink>
      </div>
    </header>

    <section class="setup-hero setup-hero--tight">
      <p class="mono eyebrow">OPERATIONAL ALERTS</p>
      <h1>When the system speaks, <em>act on it.</em></h1>
      <p>Guard trips, broken ticks, and failed submissions land here with an acknowledgement workflow. Webhook notifications are retried on a bounded exponential backoff, and every alert code can be silenced, re-severitied, or re-timed without a restart.</p>
    </section>

    <p v-if="listError" class="error-note desk-error desk-pad">{{ listError }}</p>
    <p v-if="actionError" class="error-note desk-error desk-pad">{{ actionError }}</p>
    <p v-if="actionNotice" class="quiet-note desk-pad action-notice">{{ actionNotice }}</p>

    <section class="setup-card">
      <div class="setup-heading">
        <div>
          <p class="mono micro">ALERT LEDGER</p>
          <h2>Pages and acknowledgements</h2>
        </div>
        <div class="alerts-heading-right">
          <span class="mono micro">{{ unacknowledged }} OPEN</span>
          <button class="mini-control" type="button" :disabled="loading" @click="refreshAll">{{ loading ? 'READING…' : 'REFRESH' }}</button>
          <button v-if="unacknowledged" class="mini-control mini-control--danger" type="button" :disabled="ackingAll" @click="acknowledgeAll">{{ ackingAll ? 'CLEARING…' : 'ACK ALL' }}</button>
        </div>
      </div>

      <div class="alert-filters">
        <div class="alert-filter-chips" role="group" aria-label="Severity filter">
          <button v-for="option in ['ALL', 'WARNING', 'ERROR', 'INFO'] as const" :key="option" type="button" :class="['filter-chip', { 'filter-chip--on': severityFilter === option }]" @click="severityFilter = option">{{ option }}</button>
        </div>
        <div class="alert-filter-chips" role="group" aria-label="State filter">
          <button v-for="option in ['ALL', 'OPEN'] as const" :key="option" type="button" :class="['filter-chip', { 'filter-chip--on': stateFilter === option }]" @click="stateFilter = option">{{ option }}</button>
        </div>
        <select v-model="codeFilter" class="alert-code-select" aria-label="Filter by alert code">
          <option value="">ALL CODES</option>
          <option v-for="code in knownCodes" :key="code" :value="code">{{ code }}</option>
        </select>
      </div>

      <div class="alert-table">
        <div v-for="alert in alerts" :key="alert.id" :class="['alert-row', { 'alert-row--acked': alert.acknowledged }]">
          <span :class="['mono', 'alert-sev', `alert-sev--${alert.severity.toLowerCase()}`]">{{ alert.severity }}</span>
          <div class="alert-main">
            <span class="mono alert-code">{{ alert.code }}<template v-if="alert.occurrences > 1"> ×{{ alert.occurrences }}</template></span>
            <span class="alert-message">{{ alert.message }}</span>
            <span class="mono micro alert-meta">RAISED {{ when(alert.created_at) }} · LAST SEEN {{ when(alert.last_seen_at) }}<template v-if="alert.acknowledged"> · ACKED {{ when(alert.acknowledged_at) }}</template></span>
            <details v-if="Object.keys(alert.payload ?? {}).length" class="alert-payload">
              <summary class="mono micro">PAYLOAD</summary>
              <pre class="mono">{{ JSON.stringify(alert.payload, null, 2) }}</pre>
            </details>
          </div>
          <button v-if="!alert.acknowledged" class="mini-control" type="button" :disabled="ackingId === alert.id" @click="acknowledgeAlert(alert)">{{ ackingId === alert.id ? '…' : 'ACK' }}</button>
          <span v-else class="mono micro alert-acked-note">ACKED</span>
        </div>
        <p v-if="!alerts.length && !loading" class="quiet-note desk-pad">Nothing matches this filter. A quiet ledger is the intended state — guards pass, ticks land, the operator sleeps.</p>
      </div>
    </section>

    <section class="setup-card">
      <div class="setup-heading">
        <div>
          <p class="mono micro">ALERT RULES</p>
          <h2>Per-code behavior</h2>
        </div>
        <span class="mono micro">SAVED WITHOUT RESTART</span>
      </div>
      <div class="rules-grid">
        <div v-for="rule in rules" :key="rule.code" class="rule-card">
          <div class="rule-head">
            <span class="mono alert-code">{{ rule.code }}</span>
            <label class="rule-toggle">
              <input v-model="draftFor(rule).enabled" type="checkbox">
              <span>ENABLED</span>
            </label>
          </div>
          <p class="rule-description">{{ rule.description }}</p>
          <div class="rule-controls">
            <label class="rule-field">
              <span>SEVERITY</span>
              <select v-model="draftFor(rule).severity">
                <option value="INFO">INFO</option>
                <option value="WARNING">WARNING</option>
                <option value="ERROR">ERROR</option>
              </select>
            </label>
            <label class="rule-field">
              <span>COOLDOWN S</span>
              <input v-model.number="draftFor(rule).cooldown_seconds" type="number" min="5" max="86400" placeholder="default">
            </label>
            <label class="rule-toggle rule-toggle--webhook">
              <input v-model="draftFor(rule).notify_webhook" type="checkbox">
              <span>WEBHOOK</span>
            </label>
            <button class="mini-control" type="button" :disabled="savingRule === rule.code" @click="saveRule(rule)">{{ savingRule === rule.code ? 'SAVING…' : 'SAVE' }}</button>
          </div>
          <p class="mono micro rule-updated">{{ rule.updated_at ? `UPDATED ${when(rule.updated_at)}` : 'FACTORY DEFAULTS' }}</p>
        </div>
        <p v-if="!rules.length" class="quiet-note desk-pad">Rules have not been seeded yet; the built-in defaults are in force.</p>
      </div>
    </section>

    <section class="setup-card">
      <div class="setup-heading">
        <div>
          <p class="mono micro">WEBHOOK DELIVERIES</p>
          <h2>Outbound retry ledger</h2>
        </div>
        <div class="alerts-heading-right">
          <span v-if="policy" class="mono micro">{{ policy.signing_enabled ? 'SIGNED' : 'UNSIGNED' }} · ×{{ policy.max_attempts }} · BACKOFF {{ policy.backoff_base_seconds }}s→{{ policy.backoff_max_seconds }}s</span>
          <button class="mini-control" type="button" :disabled="sendingTest || !policy?.target_configured" @click="sendTest">{{ sendingTest ? 'QUEUING…' : 'SEND TEST WEBHOOK' }}</button>
        </div>
      </div>
      <p v-if="policy && !policy.target_configured" class="quiet-note desk-pad">No webhook target is configured. Set <strong>TRADINGOS_ALERT_WEBHOOK_URL</strong> (and optionally <strong>TRADINGOS_WEBHOOK_SIGNING_SECRET</strong>) on the backend to fan alerts out to an external receiver.</p>
      <div v-if="deliveries.length" class="delivery-table">
        <div class="delivery-row delivery-row--head"><span>ID</span><span>EVENT</span><span>CODE</span><span>STATUS</span><span>ATTEMPTS</span><span>NEXT TRY</span><span>HTTP</span><span>LAST ERROR</span><span></span></div>
        <div v-for="delivery in deliveries" :key="delivery.id" class="delivery-row">
          <span class="mono">#{{ delivery.id }}</span>
          <span class="mono">{{ delivery.event }}</span>
          <span class="mono">{{ delivery.code }}</span>
          <span :class="['mono', 'delivery-status', `delivery-status--${deliveryStatusTone(delivery.status)}`]">{{ delivery.status }}</span>
          <span class="mono">{{ delivery.attempts }}/{{ delivery.max_attempts }}</span>
          <span class="mono">{{ delivery.status === 'PENDING' ? when(delivery.next_attempt_at) : '—' }}</span>
          <span class="mono">{{ delivery.last_http_status ?? '—' }}</span>
          <span class="delivery-error" :title="delivery.last_error ?? ''">{{ delivery.last_error ?? '—' }}</span>
          <button v-if="delivery.status !== 'DELIVERED'" class="mini-control" type="button" :disabled="retryingId === delivery.id" @click="retryDelivery(delivery)">{{ retryingId === delivery.id ? '…' : 'RETRY' }}</button>
          <span v-else class="mono micro alert-acked-note">{{ when(delivery.delivered_at) }}</span>
        </div>
      </div>
      <p v-else-if="policy?.target_configured" class="quiet-note desk-pad">No deliveries yet. The first guard trip or TEST webhook will appear here with its full attempt history.</p>
    </section>
  </div>
</template>

<style scoped>
.alerts-topbar-right, .alerts-heading-right { display: flex; align-items: center; gap: 10px; }
.alert-filters { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin: 6px 0 14px; }
.alert-filter-chips { display: flex; gap: 6px; }
.filter-chip { font-family: var(--mono, monospace); font-size: 10px; letter-spacing: .1em; padding: 5px 10px; border: 1px solid rgba(235,232,223,.2); background: transparent; color: var(--quiet, #8a8f8d); cursor: pointer; }
.filter-chip--on { background: rgba(131,187,176,.16); color: var(--paper, #ebe8df); border-color: rgba(131,187,176,.5); }
.alert-code-select { background: transparent; border: 1px solid rgba(235,232,223,.2); color: var(--paper, #ebe8df); font-family: var(--mono, monospace); font-size: 10px; letter-spacing: .1em; padding: 5px 8px; }
.alert-code-select option { background: #101614; color: #ebe8df; }
.alert-table { display: grid; gap: 8px; }
.alert-row { display: flex; gap: 14px; align-items: flex-start; border: 1px solid rgba(235,232,223,.12); padding: 12px 14px; background: rgba(10,14,13,.45); }
.alert-row--acked { opacity: .55; }
.alert-sev { font-size: 10px; letter-spacing: .14em; padding: 3px 8px; border: 1px solid currentColor; }
.alert-sev--error { color: #d98a7e; }
.alert-sev--warning { color: #b89a6a; }
.alert-sev--info { color: #83bbb0; }
.alert-main { display: grid; gap: 3px; flex: 1; }
.alert-code { font-size: 12px; color: var(--paper, #ebe8df); letter-spacing: .08em; }
.alert-message { font-size: 13px; color: var(--paper, #ebe8df); }
.alert-meta { color: var(--quiet, #8a8f8d); }
.alert-payload summary { cursor: pointer; color: var(--quiet, #8a8f8d); }
.alert-payload pre { font-size: 11px; color: var(--quiet, #8a8f8d); max-height: 180px; overflow: auto; margin: 6px 0 0; }
.alert-acked-note { color: var(--quiet, #8a8f8d); }
.rules-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.rule-card { border: 1px solid rgba(235,232,223,.12); padding: 14px; display: grid; gap: 10px; background: rgba(10,14,13,.45); }
.rule-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.rule-description { font-size: 12px; color: var(--quiet, #8a8f8d); margin: 0; }
.rule-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }
.rule-field { display: grid; gap: 4px; font-size: 10px; }
.rule-field span { color: var(--quiet, #8a8f8d); letter-spacing: .1em; font-family: var(--mono, monospace); }
.rule-field select, .rule-field input { background: transparent; border: 1px solid rgba(235,232,223,.2); color: var(--paper, #ebe8df); padding: 5px 8px; font-family: var(--mono, monospace); font-size: 11px; width: 110px; }
.rule-field select option { background: #101614; color: #ebe8df; }
.rule-toggle { display: flex; gap: 6px; align-items: center; font-family: var(--mono, monospace); font-size: 10px; letter-spacing: .1em; color: var(--quiet, #8a8f8d); cursor: pointer; }
.rule-updated { color: var(--quiet, #8a8f8d); margin: 0; }
.delivery-table { display: grid; gap: 4px; }
.delivery-row { display: grid; grid-template-columns: 52px 62px 1.2fr 96px 70px 110px 52px 1.6fr 70px; gap: 8px; align-items: center; padding: 7px 10px; border: 1px solid rgba(235,232,223,.08); font-size: 11px; }
.delivery-row--head { font-family: var(--mono, monospace); font-size: 9px; letter-spacing: .12em; color: var(--quiet, #8a8f8d); border-bottom: 1px solid rgba(235,232,223,.18); }
.delivery-status--pos { color: #83bbb0; }
.delivery-status--neg { color: #d98a7e; }
.delivery-status--warn { color: #b89a6a; }
.delivery-error { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--quiet, #8a8f8d); }
.action-notice { color: #83bbb0; }
@media (max-width: 900px) {
  .delivery-row { grid-template-columns: 44px 54px 1fr 84px 60px 90px; }
  .delivery-row > :nth-child(7), .delivery-row > :nth-child(8) { display: none; }
}
</style>
