<!-- Design: The Instrument Room — the strategy desk drafts parameter sets; validation is earned from censored data, never assumed. -->
<script setup lang="ts">
import type { StrategyEvaluation, StrategyVersion } from '~/types/trading'

const api = useTradingApi()
const adminToken = ref('')
const strategies = ref<StrategyVersion[]>([])
const evaluations = ref<Record<number, StrategyEvaluation[]>>({})
const loading = ref(true)
const loadError = ref<string | null>(null)
const notice = ref<string | null>(null)
const formError = ref<string | null>(null)
const isCreating = ref(false)
const evalOpenFor = ref<number | null>(null)
const evalBusyFor = ref<number | null>(null)
const evalError = ref<string | null>(null)
const historyOpenFor = ref<number | null>(null)
const historyBusyFor = ref<number | null>(null)
const statusBusyFor = ref<number | null>(null)
const statusError = ref<string | null>(null)

const form = ref({
  strategy_key: '',
  version: 'v1',
  fast_window: 12,
  slow_window: 26,
  volatility_window: 20,
  max_drawdown_percent: 5,
  trade_amount: 0,
  duration_minutes: 1,
})

const evalForm = ref({ symbol: 'EURUSD', timeframe_seconds: 60, censor_gap_seconds: 60 })

const keyIsValid = computed(() => /^[a-z0-9_-]+$/.test(form.value.strategy_key))
const windowsAreValid = computed(() => form.value.fast_window >= 1 && form.value.slow_window >= 2 && form.value.fast_window < form.value.slow_window)
const amountIsValid = computed(() => form.value.trade_amount === null || form.value.trade_amount === 0 || (form.value.trade_amount > 0 && form.value.trade_amount <= 10_000))
const formIsValid = computed(() => keyIsValid.value && windowsAreValid.value && amountIsValid.value && form.value.version.trim().length > 0 && form.value.volatility_window >= 1 && form.value.max_drawdown_percent > 0 && form.value.max_drawdown_percent <= 100)

const statusChipClass: Record<string, string> = { DRAFT: 'strategy-chip--draft', VALIDATED: 'strategy-chip--validated', RETIRED: 'strategy-chip--retired', VALIDATING: 'strategy-chip--draft' }

function messageFor(error: unknown, fallback: string) {
  if (error && typeof error === 'object' && 'data' in error) {
    const detail = (error as { data?: { detail?: string } }).data?.detail
    if (detail) return detail
  }
  return error instanceof Error ? error.message : fallback
}

async function loadStrategies() {
  loading.value = true
  loadError.value = null
  try {
    strategies.value = await api.getStrategies()
  } catch (error) {
    loadError.value = messageFor(error, 'The strategy ledger could not be read from the local API.')
  } finally {
    loading.value = false
  }
}

async function createStrategy() {
  if (!adminToken.value.trim()) { formError.value = 'The local admin token is required to draft a strategy.'; return }
  isCreating.value = true
  formError.value = null
  notice.value = null
  try {
    const created = await api.createStrategy(adminToken.value.trim(), {
      strategy_key: form.value.strategy_key.trim(),
      version: form.value.version.trim(),
      definition: {
        kind: 'ema_cross',
        fast_window: Number(form.value.fast_window),
        slow_window: Number(form.value.slow_window),
        volatility_window: Number(form.value.volatility_window),
        max_drawdown: Number(form.value.max_drawdown_percent) / 100,
        ...(Number(form.value.trade_amount) > 0 ? { trade_amount: Number(form.value.trade_amount) } : {}),
        duration_minutes: Number(form.value.duration_minutes),
      },
    })
    window.sessionStorage.setItem('tradingos-local-admin-token', adminToken.value.trim())
    strategies.value = [created, ...strategies.value]
    notice.value = `Draft ${created.strategy_key} ${created.version} is persisted. It stays out of the loop until an evaluation validates it against censored data.`
    form.value.strategy_key = ''
  } catch (error) {
    formError.value = messageFor(error, 'The strategy draft could not be persisted. Check the API and token.')
  } finally {
    isCreating.value = false
  }
}

function toggleEval(strategyId: number) {
  evalError.value = null
  evalOpenFor.value = evalOpenFor.value === strategyId ? null : strategyId
}

async function runEvaluation(strategyId: number) {
  if (!adminToken.value.trim()) { evalError.value = 'The local admin token is required to run an evaluation.'; return }
  evalBusyFor.value = strategyId
  evalError.value = null
  try {
    const evaluation = await api.evaluateStrategy(adminToken.value.trim(), strategyId, { ...evalForm.value })
    evaluations.value = { ...evaluations.value, [strategyId]: [evaluation, ...(evaluations.value[strategyId] ?? [])] }
    await loadStrategies()
  } catch (error) {
    evalError.value = messageFor(error, 'The evaluation could not run. Censored validation needs candles for the symbol first.')
  } finally {
    evalBusyFor.value = null
  }
}

async function toggleHistory(strategyId: number) {
  historyOpenFor.value = historyOpenFor.value === strategyId ? null : strategyId
  if (historyOpenFor.value === strategyId && !evaluations.value[strategyId]) {
    historyBusyFor.value = strategyId
    try {
      const rows = await api.getStrategyEvaluations(strategyId)
      evaluations.value = { ...evaluations.value, [strategyId]: rows }
    } catch (error) {
      evalError.value = messageFor(error, 'Evaluation history could not be read.')
    } finally {
      historyBusyFor.value = null
    }
  }
}

async function changeStatus(strategy: StrategyVersion, status: 'DRAFT' | 'RETIRED') {
  if (!adminToken.value.trim()) { statusError.value = 'The local admin token is required to change a strategy state.'; return }
  statusBusyFor.value = strategy.id
  statusError.value = null
  try {
    const updated = await api.updateStrategyStatus(adminToken.value.trim(), strategy.id, { status })
    strategies.value = strategies.value.map(item => (item.id === updated.id ? updated : item))
  } catch (error) {
    statusError.value = messageFor(error, 'The strategy state could not be changed.')
  } finally {
    statusBusyFor.value = null
  }
}

function paramList(strategy: StrategyVersion): string {
  const d = strategy.definition ?? {}
  const parts = [`fast ${d.fast_window ?? '—'}`, `slow ${d.slow_window ?? '—'}`, `vol ${d.volatility_window ?? '—'}`, `dd ${((Number(d.max_drawdown) || 0) * 100).toFixed(1)}%`]
  if (d.trade_amount) parts.push(`$${Number(d.trade_amount).toFixed(2)}`)
  if (d.duration_minutes) parts.push(`${d.duration_minutes}m`)
  return parts.join(' · ')
}

onMounted(async () => {
  adminToken.value = window.sessionStorage.getItem('tradingos-local-admin-token') ?? ''
  await loadStrategies()
})

useHead({ title: 'TradingOS · Strategy Desk' })
</script>

<template>
  <div class="setup-shell">
    <header class="setup-topbar">
      <p class="mono micro">STRATEGY DESK / DRAFT · VALIDATE · RETIRE</p>
      <NuxtLink class="return-link" to="/">&larr; CONTROL PLANE</NuxtLink>
    </header>

    <section class="setup-hero">
      <p class="mono eyebrow">PARAMETER GOVERNANCE</p>
      <h1>A draft is a <em>hypothesis</em>, nothing more.</h1>
      <p>Strategy versions are drafted here with explicit parameters, then validated against time-ordered, gap-censored candles. Only a VALIDATED version is visible to the autonomous loop — and every signal still passes the practice risk gate.</p>
    </section>

    <p v-if="notice" class="loop-notice desk-notice">{{ notice }}</p>

    <div class="setup-grid">
      <section class="setup-card">
        <div class="setup-heading">
          <div>
            <p class="mono micro">NEW DRAFT</p>
            <h2>Create a strategy version</h2>
          </div>
          <span class="panel-index">EMA CROSS</span>
        </div>
        <div class="desk-form">
          <div class="field-pair">
            <label>STRATEGY KEY<span>a-z 0-9 _ -</span>
              <input v-model="form.strategy_key" type="text" placeholder="ema_eurusd_1m" autocomplete="off" spellcheck="false">
            </label>
            <label>VERSION
              <input v-model="form.version" type="text" placeholder="v1" autocomplete="off" spellcheck="false">
            </label>
          </div>
          <div class="field-pair">
            <label>FAST WINDOW
              <input v-model.number="form.fast_window" type="number" min="1" step="1">
            </label>
            <label>SLOW WINDOW
              <input v-model.number="form.slow_window" type="number" min="2" step="1">
            </label>
          </div>
          <p v-if="!windowsAreValid" class="field-warning">Fast window must be a positive integer strictly below the slow window.</p>
          <div class="field-pair">
            <label>VOLATILITY WINDOW
              <input v-model.number="form.volatility_window" type="number" min="1" step="1">
            </label>
            <label>MAX DRAWDOWN %
              <input v-model.number="form.max_drawdown_percent" type="number" min="0.1" max="100" step="0.1">
            </label>
          </div>
          <div class="field-pair">
            <label>TRADE AMOUNT $<span>0 falls back to the risk cap</span>
              <input v-model.number="form.trade_amount" type="number" min="0" step="0.5">
            </label>
            <label>DURATION MINUTES
              <input v-model.number="form.duration_minutes" type="number" min="1" max="60" step="1">
            </label>
          </div>
          <label class="field-token">LOCAL ADMIN TOKEN<span>stored in this tab only</span>
            <input v-model="adminToken" type="password" autocomplete="off" placeholder="TRADINGOS_LOCAL_ADMIN_TOKEN">
          </label>
          <p v-if="formError" class="error-note desk-error">{{ formError }}</p>
          <button class="run-control" type="button" :disabled="isCreating || !formIsValid || !adminToken.trim()" @click="createStrategy">
            <span></span>{{ isCreating ? 'PERSISTING…' : 'PERSIST DRAFT VERSION' }}
          </button>
          <p class="quiet-note desk-contract">A persisted draft never trades. Validation requires at least 10 censored walk-forward trades, positive return, and drawdown inside the declared bound.</p>
        </div>
      </section>

      <section class="setup-card">
        <div class="setup-heading">
          <div>
            <p class="mono micro">VERSION LEDGER</p>
            <h2>Drafted strategies</h2>
          </div>
          <span class="panel-index">{{ strategies.length }} VERSIONS</span>
        </div>
        <p v-if="loading" class="quiet-note desk-pad">Reading the strategy ledger…</p>
        <p v-else-if="loadError" class="error-note desk-error desk-pad">{{ loadError }}</p>
        <p v-else-if="!strategies.length" class="quiet-note desk-pad">No strategy versions exist yet. Draft the first parameter set on the left; candles arrive through broker reconciliation.</p>
        <div v-else class="strategy-list">
          <article v-for="strategy in strategies" :key="strategy.id" class="strategy-card">
            <header class="strategy-card-head">
              <div>
                <strong class="mono">{{ strategy.strategy_key }}</strong>
                <span class="mono strategy-version">{{ strategy.version }}</span>
              </div>
              <span :class="['strategy-chip', statusChipClass[strategy.status] ?? 'strategy-chip--draft']">{{ strategy.status }}</span>
            </header>
            <p class="mono strategy-params">{{ paramList(strategy) }}</p>
            <p class="strategy-meta">persisted {{ new Date(strategy.created_at).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) }} · validation {{ strategy.validation_summary && Object.keys(strategy.validation_summary).length ? 'recorded' : 'pending' }}</p>

            <div class="strategy-actions">
              <button class="mini-control" type="button" @click="toggleEval(strategy.id)">{{ evalOpenFor === strategy.id ? 'CLOSE EVALUATION' : 'EVALUATE' }}</button>
              <button class="mini-control" type="button" @click="toggleHistory(strategy.id)">{{ historyOpenFor === strategy.id ? 'HIDE HISTORY' : 'HISTORY' }}</button>
              <button v-if="strategy.status !== 'RETIRED'" class="mini-control mini-control--danger" type="button" :disabled="statusBusyFor === strategy.id" @click="changeStatus(strategy, 'RETIRED')">{{ statusBusyFor === strategy.id ? '…' : 'RETIRED' }}</button>
              <button v-if="strategy.status === 'RETIRED'" class="mini-control" type="button" :disabled="statusBusyFor === strategy.id" @click="changeStatus(strategy, 'DRAFT')">{{ statusBusyFor === strategy.id ? '…' : 'RE-DRAFT' }}</button>
            </div>

            <div v-if="evalOpenFor === strategy.id" class="strategy-eval">
              <div class="field-pair">
                <label>SYMBOL
                  <input v-model="evalForm.symbol" type="text" autocomplete="off" spellcheck="false">
                </label>
                <label>TIMEFRAME S
                  <input v-model.number="evalForm.timeframe_seconds" type="number" min="1" step="1">
                </label>
                <label>CENSOR GAP S
                  <input v-model.number="evalForm.censor_gap_seconds" type="number" min="1" step="1">
                </label>
              </div>
              <button class="mini-control" type="button" :disabled="evalBusyFor === strategy.id" @click="runEvaluation(strategy.id)">
                {{ evalBusyFor === strategy.id ? 'EVALUATING…' : 'RUN WALK-FORWARD' }}
              </button>
            </div>

            <div v-if="evaluations[strategy.id]?.length" class="strategy-eval-results">
              <div v-for="evaluation in evaluations[strategy.id]?.slice(0, 3) ?? []" :key="evaluation.id" class="eval-row">
                <span :class="['strategy-chip', evaluation.accepted ? 'strategy-chip--validated' : 'strategy-chip--draft']">{{ evaluation.accepted ? 'ACCEPTED' : 'REJECTED' }}</span>
                <span class="mono">{{ evaluation.metrics?.trades ?? 0 }} trades</span>
                <span class="mono">{{ ((evaluation.metrics?.win_rate ?? 0) * 100).toFixed(1) }}% win</span>
                <span class="mono" :class="(evaluation.metrics?.total_return ?? 0) > 0 ? 'pos' : 'neg'">{{ ((evaluation.metrics?.total_return ?? 0) * 100).toFixed(2) }}% return</span>
                <span class="mono">dd {{ ((evaluation.metrics?.max_drawdown ?? 0) * 100).toFixed(2) }}%</span>
              </div>
            </div>

            <p v-if="historyOpenFor === strategy.id && historyBusyFor === strategy.id" class="quiet-note desk-pad">Reading evaluation history…</p>
            <p v-else-if="historyOpenFor === strategy.id && !evaluations[strategy.id]?.length" class="quiet-note desk-pad">No evaluation has been recorded for this version yet.</p>
          </article>
        </div>
        <p v-if="evalError" class="error-note desk-error desk-pad">{{ evalError }}</p>
        <p v-if="statusError" class="error-note desk-error desk-pad">{{ statusError }}</p>
        <p class="quiet-note desk-contract">Retiring a version removes it from the loop on the next tick. Re-drafting returns it to DRAFT — it must be validated again before any new exposure.</p>
      </section>
    </div>
  </div>
</template>
