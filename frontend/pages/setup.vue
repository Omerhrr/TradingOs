<!-- Design: The Instrument Room — a local-only, high-trust setup console where safety and evidence are more prominent than action. -->
<script setup lang="ts">
import type { BrokerConnection, ReconciliationRun } from '~/types/trading'

const api = useTradingApi()
const adminToken = ref('')
const email = ref('')
const password = ref('')
const showPassword = ref(false)
const busy = ref<'store' | 'connect' | 'reconcile' | null>(null)
const errorMessage = ref<string | null>(null)
const notice = ref<string | null>(null)
const credentialStored = ref(false)
const connection = ref<BrokerConnection | null>(null)
const reconciliation = ref<ReconciliationRun | null>(null)

function messageFor(error: unknown, fallback: string) {
  if (error && typeof error === 'object' && 'data' in error) {
    const detail = (error as { data?: { detail?: string } }).data?.detail
    if (detail) return detail
  }
  return error instanceof Error ? error.message : fallback
}

onMounted(() => {
  adminToken.value = window.sessionStorage.getItem('tradingos-local-admin-token') ?? ''
})

async function storeCredentials() {
  errorMessage.value = null
  notice.value = null
  busy.value = 'store'
  try {
    const result = await api.storeBrokerCredentials(adminToken.value, { email: email.value, password: password.value })
    window.sessionStorage.setItem('tradingos-local-admin-token', adminToken.value)
    password.value = ''
    credentialStored.value = true
    connection.value = result
    notice.value = 'Credentials are encrypted in the local backend. No broker connection has been opened.'
  } catch (error) {
    errorMessage.value = messageFor(error, 'Credentials could not be stored. Check the local API and token.')
  } finally {
    busy.value = null
  }
}

async function connectPractice() {
  errorMessage.value = null
  notice.value = null
  busy.value = 'connect'
  try {
    const result = await api.connectPracticeBroker(adminToken.value)
    if (result.account_mode !== 'PRACTICE') throw new Error('Broker mode was not confirmed as PRACTICE. The connection is not accepted.')
    connection.value = result
    notice.value = `PRACTICE connection verified${result.balance !== null ? ` · ${result.currency ?? ''} ${result.balance.toFixed(2)}` : ''}. No order was submitted.`
  } catch (error) {
    errorMessage.value = messageFor(error, 'PRACTICE connection could not be verified. The system remains paused.')
  } finally {
    busy.value = null
  }
}

async function runReconciliation() {
  errorMessage.value = null
  notice.value = null
  busy.value = 'reconcile'
  try {
    reconciliation.value = await api.reconcilePracticeBroker(adminToken.value)
    notice.value = 'Practice reconciliation completed. Review persisted observations on the control plane before enabling any background worker.'
  } catch (error) {
    errorMessage.value = messageFor(error, 'Reconciliation could not be completed. New exposure remains paused.')
  } finally {
    busy.value = null
  }
}

const isPracticeConnected = computed(() => connection.value?.state === 'CONNECTED' && connection.value.account_mode === 'PRACTICE')
</script>

<template>
  <main class="setup-shell">
    <header class="setup-topbar">
      <NuxtLink class="return-link" to="/">← CONTROL PLANE</NuxtLink>
      <p class="mono micro">LOCAL DEVICE / PRACTICE ONBOARDING</p>
    </header>

    <section class="setup-hero">
      <p class="mono eyebrow">CREDENTIAL VAULT</p>
      <h1>Authenticate locally.<br><em>Connect deliberately.</em></h1>
      <p>This page talks only to your FastAPI service on the local machine. The admin token stays in this browser session; the IQ Option password is cleared from the page after encrypted storage.</p>
    </section>

    <section class="setup-grid" aria-label="Broker setup workflow">
      <form class="setup-card setup-card--form" @submit.prevent="storeCredentials">
        <div class="setup-heading">
          <div><p class="mono eyebrow">01 / LOCAL AUTHORIZATION</p><h2>Secure credential intake</h2></div>
          <span class="setup-index">PRACTICE ONLY</span>
        </div>

        <label>
          <span>Local admin token</span>
          <input v-model="adminToken" type="password" autocomplete="off" required placeholder="TRADINGOS_LOCAL_ADMIN_TOKEN">
          <small>Used only as the `X-TradingOS-Token` header for protected local controls. Kept in session storage, not in the database.</small>
        </label>
        <label>
          <span>IQ Option email</span>
          <input v-model.trim="email" type="email" autocomplete="off" required placeholder="you@example.com">
        </label>
        <label>
          <span>IQ Option password</span>
          <div class="secret-input"><input v-model="password" :type="showPassword ? 'text' : 'password'" autocomplete="off" required placeholder="Password is never saved in the browser"><button type="button" @click="showPassword = !showPassword">{{ showPassword ? 'HIDE' : 'SHOW' }}</button></div>
          <small>Sent once to the local API and encrypted using `TRADINGOS_CREDENTIAL_ENCRYPTION_KEY`. It is then cleared from this page.</small>
        </label>
        <button class="setup-action" type="submit" :disabled="busy !== null || !adminToken || !email || !password">{{ busy === 'store' ? 'ENCRYPTING…' : 'STORE ENCRYPTED CREDENTIALS' }}</button>
      </form>

      <aside class="setup-card setup-card--protocol">
        <div class="setup-heading"><div><p class="mono eyebrow">CONTROL CONTRACT</p><h2>One-way safety gates</h2></div><span class="setup-index">S-01</span></div>
        <ol class="setup-steps">
          <li :class="{ 'is-complete': credentialStored }"><span>01</span><div><strong>Encrypt locally</strong><p>Store broker credentials without opening a connection.</p></div></li>
          <li :class="{ 'is-complete': isPracticeConnected }"><span>02</span><div><strong>Verify PRACTICE</strong><p>Reject connection unless the broker explicitly reports practice mode.</p></div></li>
          <li :class="{ 'is-complete': reconciliation }"><span>03</span><div><strong>Reconcile evidence</strong><p>Persist account, asset, candle, and position observations before resuming.</p></div></li>
        </ol>
        <div class="setup-warning"><span>!</span><p>Real execution is hard-disabled. This page cannot change that setting or submit an order.</p></div>
      </aside>
    </section>

    <section class="setup-card setup-card--actions">
      <div class="setup-heading"><div><p class="mono eyebrow">02 / BROKER VERIFICATION</p><h2>Explicit next actions</h2></div><span :class="['connection-chip', { 'connection-chip--ready': isPracticeConnected }]">{{ isPracticeConnected ? 'PRACTICE CONFIRMED' : connection?.state ?? 'DISCONNECTED' }}</span></div>
      <p v-if="notice" class="setup-notice">{{ notice }}</p>
      <p v-if="errorMessage" class="setup-error">{{ errorMessage }}</p>
      <div class="setup-actions">
        <button class="setup-action setup-action--quiet" type="button" :disabled="busy !== null || !credentialStored" @click="connectPractice">{{ busy === 'connect' ? 'VERIFYING…' : 'CONNECT TO PRACTICE' }}</button>
        <button class="setup-action setup-action--quiet" type="button" :disabled="busy !== null || !isPracticeConnected" @click="runReconciliation">{{ busy === 'reconcile' ? 'RECONCILING…' : 'RUN RECONCILIATION' }}</button>
        <NuxtLink class="setup-return" to="/">REVIEW CONTROL PLANE →</NuxtLink>
      </div>
      <p v-if="reconciliation" class="setup-summary">Latest run: <strong>{{ reconciliation.state }}</strong> · {{ reconciliation.summary?.candles_ingested ?? 0 }} candles ingested · {{ reconciliation.summary?.positions_observed ?? 0 }} positions observed.</p>
    </section>
  </main>
</template>
