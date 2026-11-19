/**
 * Singleton WebSocket connection shared across all components.
 * Pushes server-sent events to registered handlers.
 *
 * Event shape: { type: string; run_id: string }
 *   - "run_created"  — a new run was started
 *   - "run_updated"  — run status/notes changed
 *   - "metrics"      — new metric batch ingested for run_id
 *   - "system"       — new system snapshot ingested for run_id
 */

import { useEffect, useRef } from 'react'

export interface WsEvent {
  type: 'run_created' | 'run_updated' | 'metrics' | 'system' | string
  run_id: string
}

type Handler = (event: WsEvent) => void

// ── Singleton state ──────────────────────────────────────────────────────────

let _socket: WebSocket | null = null
let _reconnectTimer: ReturnType<typeof setTimeout> | null = null
const _handlers = new Set<Handler>()

function _wsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
}

function _connect() {
  if (_socket && _socket.readyState !== WebSocket.CLOSED) return

  const ws = new WebSocket(_wsUrl())

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data) as WsEvent
      _handlers.forEach((h) => h(msg))
    } catch {
      // ignore malformed frames
    }
  }

  ws.onclose = () => {
    _socket = null
    // Reconnect with 3 s back-off; don't bother if there are no listeners
    if (_handlers.size > 0) {
      _reconnectTimer = setTimeout(_connect, 3000)
    }
  }

  ws.onerror = () => {
    ws.close()
  }

  _socket = ws
}

function _ensureConnected() {
  if (_reconnectTimer !== null) {
    clearTimeout(_reconnectTimer)
    _reconnectTimer = null
  }
  _connect()
}

// ── Hook ─────────────────────────────────────────────────────────────────────

/**
 * Subscribe to WebSocket push events from the Pyrometer server.
 * The handler is called whenever the server broadcasts an event.
 * The hook maintains a stable identity via a ref, so the handler
 * can safely close over reactive state without stale-closure issues.
 */
export function useWebSocket(handler: Handler): void {
  const handlerRef = useRef<Handler>(handler)
  handlerRef.current = handler

  useEffect(() => {
    const fn: Handler = (msg) => handlerRef.current(msg)
    _handlers.add(fn)
    _ensureConnected()
    return () => {
      _handlers.delete(fn)
      // If no more listeners, let the socket close naturally on reconnect
    }
  }, [])
}
