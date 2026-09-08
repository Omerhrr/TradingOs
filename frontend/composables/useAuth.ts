// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
//
// Auth state for the remote-access gate. The backend owns the credential:
// signing in exchanges the admin token for an HttpOnly session cookie, so
// nothing secret is ever written to localStorage. When the gate is off
// (local-first mode) this composable stays inert and pages never redirect.
import { computed } from 'vue'
import type { AuthLogin, AuthSession } from '~/types/trading'

export function useAuth() {
  const session = useState<AuthSession | null>('tradingos-auth-session', () => null)
  const config = useRuntimeConfig()
  const apiBaseUrl = config.public.apiBaseUrl

  const isRemoteGated = computed(() => session.value?.remote_access ?? false)
  const isAuthenticated = computed(() => session.value?.authenticated ?? false)

  async function bootstrap(): Promise<AuthSession> {
    try {
      session.value = await $fetch<AuthSession>(`${apiBaseUrl}/auth/session`, { credentials: 'include' })
    } catch {
      // An unreachable backend must not lock the UI into a login loop.
      session.value = { authenticated: false, remote_access: false, expires_at: null }
    }
    return session.value
  }

  async function login(token: string, totpCode?: string): Promise<AuthLogin> {
    const result = await $fetch<AuthLogin>(`${apiBaseUrl}/auth/login`, {
      method: 'POST',
      body: { token, ...(totpCode ? { totp_code: totpCode } : {}) },
      credentials: 'include',
    })
    session.value = { authenticated: true, remote_access: true, expires_at: result.expires_at, totp_required: session.value?.totp_required ?? false }
    return result
  }

  async function logout(): Promise<void> {
    try {
      await $fetch(`${apiBaseUrl}/auth/logout`, { method: 'POST', credentials: 'include' })
    } finally {
      session.value = { authenticated: false, remote_access: session.value?.remote_access ?? false, expires_at: null }
      if (import.meta.client) navigateTo('/login')
    }
  }

  function handleUnauthorized(): void {
    // Only the remote gate turns API 401s into a sign-out; local-mode 401s
    // mean a missing admin token and are surfaced inline instead.
    if (!session.value?.remote_access) return
    if (import.meta.client && window.location.pathname === '/login') return
    session.value = { authenticated: false, remote_access: true, expires_at: null }
    if (import.meta.client) navigateTo('/login')
  }

  return { session, isRemoteGated, isAuthenticated, bootstrap, login, logout, handleUnauthorized }
}
