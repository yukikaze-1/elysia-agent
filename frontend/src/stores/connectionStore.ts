import { defineStore } from 'pinia'
import { ref } from 'vue'

export const WS_RECONNECT_BASE_MS = 1000
export const WS_RECONNECT_MAX_MS = 30000
export const WS_RECONNECT_JITTER = 0.2

function backoff(attempt: number): number {
  const base = Math.min(WS_RECONNECT_BASE_MS * Math.pow(2, attempt), WS_RECONNECT_MAX_MS)
  const jitter = base * WS_RECONNECT_JITTER * (Math.random() * 2 - 1)
  return Math.round(base + jitter)
}

/**
 * WebSocket connection store with exponential backoff reconnection.
 *
 * - `connect(url, onMessage?)`: establish (or replace) a WebSocket connection.
 * - `send(data)` / `sendBinary(data)`: transmit text/JSON or binary frames.
 * - `disconnect()`: tear down connection and cancel pending reconnect.
 * - `resetConnection()`: zero the retry counter and connected flag without
 *   touching the WebSocket reference or clearing chat messages (used before
 *   reconnect attempts).
 *
 * Connection state is exposed via reactive refs (`connected`, `connStatusText`)
 * consumed by components like SettingsPanel and InputBar.
 */
export const useConnectionStore = defineStore('connection', () => {
  const ws = ref<WebSocket | null>(null)
  const connected = ref(false)
  const retryCount = ref(0)
  const connStatusText = ref('连接中...')

  let _lastUrl: string | null = null
  let _onMessage: ((msg: unknown) => void) | null = null
  let _reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let _interruptHandler: (() => void) | null = null

  // ---- Private helpers ----

  function scheduleReconnect(): void {
    if (_reconnectTimer !== null) return
    const delay = backoff(retryCount.value)
    retryCount.value++
    console.log(`WS: ${delay}ms 后重连 (第 ${retryCount.value} 次)`)
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null
      if (_lastUrl) {
        resetConnection()
        connect(_lastUrl)
      }
    }, delay)
  }

  // ---- Public actions ----

  /**
   * Open a WebSocket connection to `url`. The optional `onMessage` callback
   * is stored and reused across automatic reconnects. If `onMessage` is
   * omitted during a reconnect, the previously registered handler is kept.
   */
  function connect(url: string, onMessage?: (msg: unknown) => void): void {
    _lastUrl = url
    if (onMessage !== undefined) {
      _onMessage = onMessage
    }

    // Cancel any pending reconnect timer
    if (_reconnectTimer !== null) {
      clearTimeout(_reconnectTimer)
      _reconnectTimer = null
    }

    // Tear down existing connection cleanly
    if (ws.value) {
      ws.value.onopen = null
      ws.value.onclose = null
      ws.value.onerror = null
      ws.value.onmessage = null
      ws.value.close()
      ws.value = null
    }

    const socket = new WebSocket(url)
    ws.value = socket

    socket.onopen = () => {
      retryCount.value = 0
      connected.value = true
      connStatusText.value = '已连接'
    }

    socket.onerror = () => {
      console.warn('WS: 传输层错误')
    }

    socket.onclose = () => {
      connected.value = false
      connStatusText.value = '断开 — 重连中...'
      scheduleReconnect()
    }

    socket.onmessage = (e: MessageEvent) => {
      let msg: unknown
      try {
        msg = JSON.parse(e.data)
      } catch (err) {
        console.warn('WS: 无法解析消息', err)
        return
      }
      _onMessage?.(msg)
    }

    // Replace any existing beforeunload guard
    if (_interruptHandler) {
      window.removeEventListener('beforeunload', _interruptHandler)
    }
    _interruptHandler = () => {
      if (ws.value?.readyState === WebSocket.OPEN) {
        ws.value.send(JSON.stringify({ type: 'interrupt' }))
      }
    }
    window.addEventListener('beforeunload', _interruptHandler)
  }

  /**
   * Send a text or JSON-serialisable message.
   * Returns `true` when the frame was queued, `false` if the socket is not open.
   */
  function send(data: string | Record<string, unknown>): boolean {
    if (!ws.value || ws.value.readyState !== WebSocket.OPEN) {
      const preview = typeof data === 'string' ? data.slice(0, 50) : typeof data
      console.warn('WS: 未连接，消息丢弃', preview)
      return false
    }
    ws.value.send(typeof data === 'string' ? data : JSON.stringify(data))
    return true
  }

  /**
   * Send a binary frame (ArrayBuffer or typed-array view).
   * Returns `true` when the frame was queued, `false` if the socket is not open.
   */
  function sendBinary(data: ArrayBuffer | ArrayBufferView<ArrayBuffer>): boolean {
    if (!ws.value || ws.value.readyState !== WebSocket.OPEN) {
      console.warn('WS: 未连接，二进制消息丢弃')
      return false
    }
    ws.value.send(data)
    return true
  }

  /** Close the connection and cancel any pending reconnection. */
  function disconnect(): void {
    if (_reconnectTimer !== null) {
      clearTimeout(_reconnectTimer)
      _reconnectTimer = null
    }
    if (_interruptHandler) {
      window.removeEventListener('beforeunload', _interruptHandler)
      _interruptHandler = null
    }
    if (ws.value) {
      ws.value.onopen = null
      ws.value.onclose = null
      ws.value.onerror = null
      ws.value.onmessage = null
      ws.value.close()
      ws.value = null
    }
    connected.value = false
    connStatusText.value = '未连接'
  }

  /**
   * Reset `retryCount` and `connected` to their initial values.
   * Does NOT close the WebSocket or clear chat messages — intended for use
   * immediately before a reconnect attempt.
   */
  function resetConnection(): void {
    retryCount.value = 0
    connected.value = false
    connStatusText.value = '连接中...'
  }

  return {
    // State
    ws,
    connected,
    retryCount,
    connStatusText,
    // Actions
    connect,
    send,
    sendBinary,
    disconnect,
    resetConnection,
  }
})
