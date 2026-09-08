// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
//
// Remote-access gate for every page. Local-first mode (gate off) is never
// interrupted; when the backend reports an active gate, unauthenticated
// navigation lands on /login with a return path.
export default defineNuxtRouteMiddleware(async (to) => {
  if (import.meta.server) return // the backend gate is the server-side authority
  const auth = useAuth()
  if (auth.session.value === null) await auth.bootstrap()
  if (!auth.session.value?.remote_access) return
  if (auth.session.value?.authenticated) return
  if (to.path === '/login') return
  return navigateTo({ path: '/login', query: { redirect: to.fullPath } })
})
