// TradingOS follows The Instrument Room: guarded, low-key, evidence-first operational design.
//
// Singleton live channel for the loop panel. One WebSocket per browser tab:
//   - a hello snapshot arrives first (loop status + last run),
//   - every loop/execution/system event is appended to a bounded ring buffer,
//   - reconnects use capped backoff so a dead backend cannot spin the tab,
//   - a client "ping" every 25s keeps proxies from idling the socket out.
import { computed, ref } from 'vue'
import type { LoopSocketEvent } from '~/types/trading'

export type LoopSocketStatus = 'connecting' | 'live' | 'offline'

const EVENT_BUFFER_LIMIT = 40
const RECONNECT_BASE_MS = 1_000
const RECONNECT_MAX_MS = 15_000
const HEARTBEAT_MS = 25_000

const status = ref<LoopSocketStatus>('offline')
const events = ref<LoopSocketEvent[]>([])
let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let heartbeatTimer: ReturnType<typeof setInterval> | null = null
let attempts = 0
let started = false
// A 4401 rejection is final until the operator signs in again: retrying
// without new credentials only burns the battery, so reconnects stop.
let authBlocked = false

function socketUrl(): string {
  const config = useRuntimeConfig()
  return `${config.public.apiBaseUrl.replace(/^http/, 'ws')}/ws/loop`
}

function clearTimers() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null }
}

function record(event: LoopSocketEvent) {
  events.value = [event, ...events.value].slice(0, EVENT_BUFFER_LIMIT)
}

function scheduleReconnect() {
  if (authBlocked || reconnectTimer) return
  const delay = Math.min(RECONNECT_BASE_MS * 2 ** attempts, RECONNECT_MAX_MS)
  attempts += 1
  status.value = 'offline'
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    open()
  }, delay)
}

function open() {
  if (typeof window === 'undefined' || socket || authBlocked) return
  status.value = 'connecting'
  let instance: WebSocket
  try {
    instance = new WebSocket(socketUrl())
  } catch {
    scheduleReconnect()
    return
  }
  socket = instance

  instance.onopen = () => {
    attempts = 0
    status.value = 'live'
    heartbeatTimer = setInterval(() => {
      if (instance.readyState === WebSocket.OPEN) instance.send('ping')
    }, HEARTBEAT_MS)
  }

  instance.onmessage = (message) => {
    try {
      const parsed = JSON.parse(message.data as string) as LoopSocketEvent
      if (parsed && typeof parsed.type === 'string') {
        record(parsed)
        if (parsed.type === 'error' && (parsed.payload as { code?: string } | null)?.code === 'unauthorized') {
          authBlocked = true
          clearTimers()
          status.value = 'offline'
        }
      }
    } catch {
      // Non-JSON frame: ignore, the channel only speaks JSON.
    }
  }

  instance.onclose = () => {
    if (socket === instance) socket = null
    clearTimers()
    scheduleReconnect()
  }

  instance.onerror = () => {
    // onclose always follows onerror; reconnect logic lives there.
  }
}

export function restartLoopSocket() {
  if (typeof window === 'undefined') return
  authBlocked = false
  clearTimers()
  if (socket) {
    const closing = socket
    socket = null
    closing.close()
  }
  open()
}

export function useLoopSocket() {
  if (import.meta.client && !started) {
    started = true
    open()
    window.addEventListener('beforeunload', () => {
      clearTimers()
      socket?.close()
      socket = null
    })
  }

  const lastEvent = computed(() => events.value[0] ?? null)
  const isConnected = computed(() => status.value === 'live')

  return { status, events, lastEvent, isConnected }
}
