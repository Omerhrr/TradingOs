<!-- Design: The Instrument Room — the door is quiet, labeled, and rate limited. -->
<script setup lang="ts">
const auth = useAuth()
const route = useRoute()

const token = ref('')
const reveal = ref(false)
const submitting = ref(false)
const errorMessage = ref<string | null>(null)
const notice = ref<string | null>(null)
const redirectTarget = computed(() => {
  const target = route.query.redirect
  return typeof target === 'string' && target.startsWith('/') && !target.startsWith('//') ? target : '/'
})

async function signIn() {
  if (!token.value.trim()) {
    errorMessage.value = 'The local admin token is required to open a session.'
    return
  }
  submitting.value = true
  errorMessage.value = null
  notice.value = null
  try {
    await auth.login(token.value.trim())
    token.value = ''
    restartLoopSocket() // the live channel may have been refused before the session opened
    await navigateTo(redirectTarget.value)
  } catch (error) {
    const detail = error instanceof Error ? error.message : ''
    if (detail.includes('429') || detail.toLowerCase().includes('too many')) {
      errorMessage.value = 'Too many failed sign-ins from this address. Wait for the lockout window to pass and try again.'
    } else if (detail.includes('401') || detail.toLowerCase().includes('invalid')) {
      errorMessage.value = 'That admin token is invalid. The attempt has been recorded in the audit ledger.'
    } else if (detail.includes('503')) {
      errorMessage.value = 'The backend has no TRADINGOS_LOCAL_ADMIN_TOKEN configured, so sign-ins are unavailable.'
    } else {
      errorMessage.value = 'The sign-in could not be confirmed by the local API.'
    }
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  if (auth.session.value === null) await auth.bootstrap()
  if (auth.session.value?.authenticated) {
    notice.value = 'A session is already open. Returning to the control plane…'
    await navigateTo(redirectTarget.value)
  }
})

useHead({ title: 'TradingOS · Sign in' })
</script>

<template>
  <div class="setup-shell">
    <header class="setup-topbar">
      <p class="mono micro">REMOTE ACCESS / SESSION CONTROL</p>
      <NuxtLink class="return-link" to="/">&larr; CONTROL PLANE</NuxtLink>
    </header>

    <section class="setup-hero setup-hero--tight">
      <p class="mono eyebrow">AUTH-GATED ENTRY</p>
      <h1>Present the <em>admin token</em> once.</h1>
      <p>Remote access is gated: the control plane only answers browsers and scripts that hold the local admin token or a signed session. Signing in exchanges the token for an HttpOnly session cookie — the token itself is never stored by this interface.</p>
    </section>

    <section class="setup-card setup-card--form login-card">
      <div class="setup-heading login-heading">
        <div>
          <p class="mono micro">SESSION EXCHANGE</p>
          <h2>Sign in</h2>
        </div>
        <span :class="['live-chip', auth.isRemoteGated.value ? 'live-chip--on' : 'live-chip--off']">{{ auth.isRemoteGated.value ? 'GATE ACTIVE' : 'GATE OFF · LOCAL' }}</span>
      </div>
      <form class="desk-form" @submit.prevent="signIn">
        <label class="field-token">
          <span>LOCAL ADMIN TOKEN</span>
          <div class="secret-input">
            <input
              v-model="token"
              :type="reveal ? 'text' : 'password'"
              name="tradingos-admin-token"
              autocomplete="current-password"
              placeholder="TRADINGOS_LOCAL_ADMIN_TOKEN value"
            >
            <button type="button" @click="reveal = !reveal">{{ reveal ? 'HIDE' : 'SHOW' }}</button>
          </div>
          <small>Found in the backend environment as TRADINGOS_LOCAL_ADMIN_TOKEN. Failed attempts are rate limited per address and recorded in the evidence log.</small>
        </label>
        <p v-if="errorMessage" class="setup-error">{{ errorMessage }}</p>
        <p v-if="notice" class="setup-notice">{{ notice }}</p>
        <button class="setup-action" type="submit" :disabled="submitting">
          {{ submitting ? 'VERIFYING…' : 'OPEN SESSION' }}
        </button>
      </form>
    </section>

    <section class="setup-card setup-card--protocol">
      <div class="setup-heading">
        <div>
          <p class="mono micro">ACCESS PROTOCOL</p>
          <h2>How the gate behaves</h2>
        </div>
      </div>
      <ol class="setup-steps">
        <li :class="{ 'is-complete': auth.isAuthenticated.value }">
          <span>01</span>
          <div>
            <strong>Present credentials on every request</strong>
            <p>With the gate on, the API answers only requests carrying the admin token, a signed session, or the session cookie. Health and sign-in endpoints stay open for probes.</p>
          </div>
        </li>
        <li :class="{ 'is-complete': auth.isAuthenticated.value }">
          <span>02</span>
          <div>
            <strong>Sessions expire on their own</strong>
            <p>A session is an HMAC-signed, time-boxed token in an HttpOnly cookie. It cannot be read by scripts in the page, and it dies quietly at expiry or on sign-out.</p>
          </div>
        </li>
        <li class="is-complete">
          <span>03</span>
          <div>
            <strong>Failures are evidence too</strong>
            <p>Five invalid attempts inside the window lock the source address out until the window drains. Every attempt lands in the append-only audit ledger.</p>
          </div>
        </li>
      </ol>
    </section>
  </div>
</template>
