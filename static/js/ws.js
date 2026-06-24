import { state } from './state.js';
import {
  WS_RECONNECT_BASE_MS,
  WS_RECONNECT_MAX_MS,
  WS_RECONNECT_JITTER,
} from './config.js';

let retryCount = 0;
let reconnectTimer = null;
/** @type {(msg: object) => void} */
let onMessage = null;

/** 计算退避延迟: base * 2^attempt, capped at max, ± jitter */
function backoff(attempt) {
  const base = Math.min(WS_RECONNECT_BASE_MS * Math.pow(2, attempt), WS_RECONNECT_MAX_MS);
  const jitter = base * WS_RECONNECT_JITTER * (Math.random() * 2 - 1);
  return Math.round(base + jitter);
}

/** 建立 WebSocket 连接，自动选择 ws:// 或 wss:// */
export function connect(handler) {
  onMessage = handler;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${protocol}//${location.host}/ws`;

  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  state.ws = new WebSocket(url);

  state.ws.onopen = () => {
    retryCount = 0;
    setConnStatus('connected', '已连接');
    const sendBtn = document.getElementById('send-btn');
    const textInput = document.getElementById('text-input');
    if (sendBtn) sendBtn.disabled = false;
    if (textInput) textInput.placeholder = '输入消息...';
  };

  state.ws.onerror = () => {
    console.warn('WS: 传输层错误');
  };

  state.ws.onclose = () => {
    setConnStatus('disconnected', '断开 — 重连中...');
    const sendBtn = document.getElementById('send-btn');
    const textInput = document.getElementById('text-input');
    if (sendBtn) sendBtn.disabled = true;
    if (textInput) textInput.placeholder = '连接断开，重连中...';
    scheduleReconnect();
  };

  state.ws.onmessage = (e) => {
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch (err) {
      console.warn('WS: 无法解析消息', err);
      return;
    }
    if (onMessage) onMessage(msg);
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = backoff(retryCount);
  retryCount++;
  console.log(`WS: ${delay}ms 后重连 (第 ${retryCount} 次)`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    state.resetConnection();
    connect(onMessage);
  }, delay);
}

/** 发送消息（自动选择 TEXT 或 BINARY 帧） */
export function send(data) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    console.warn('WS: 未连接，消息丢弃', typeof data === 'string' ? data.slice(0, 50) : typeof data);
    return false;
  }
  if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) {
    state.ws.send(data);
  } else {
    state.ws.send(typeof data === 'string' ? data : JSON.stringify(data));
  }
}

/** 更新连接状态指示器 */
function setConnStatus(cls, text) {
  const el = document.getElementById('conn-status');
  if (el) {
    el.className = cls;
    el.textContent = text;
  }
}
