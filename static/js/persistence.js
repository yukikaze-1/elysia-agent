import { state } from './state.js';
import { CHAT_SAVE_DEBOUNCE_MS, CHAT_MAX_STORED, SETTINGS_STORAGE_KEYS } from './config.js';
import { esc, createAssistantRow } from './chat.js';

/** 防抖保存 */
export function debounceSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveChat, CHAT_SAVE_DEBOUNCE_MS);
}

/** 增量保存：从 state.messages 序列化 */
export function saveChat() {
  const msgs = state.messages.slice(-CHAT_MAX_STORED);
  localStorage.setItem(SETTINGS_STORAGE_KEYS.CHAT, JSON.stringify(msgs));
}

/** 直接构建 DOM 恢复历史（不走 addUserMsg 避免副作用） */
export function restoreChat() {
  const raw = localStorage.getItem(SETTINGS_STORAGE_KEYS.CHAT);
  if (!raw) return;
  let msgs;
  try {
    msgs = JSON.parse(raw);
  } catch {
    return;
  }
  if (!Array.isArray(msgs)) return;

  const chat = document.getElementById('chat');
  for (const m of msgs) {
    if (!m.content) continue;
    if (m.role === 'user') {
      const row = document.createElement('div');
      row.className = 'msg-row user';
      row.innerHTML = `<div class="msg-bubble">${esc(m.content)}</div><div class="msg-avatar">😎</div>`;
      chat.appendChild(row);
      state.messages.push({ role: 'user', content: m.content });
    } else {
      state.lastRole = 'assistant';
      const { row, textSpan } = createAssistantRow();
      textSpan.textContent = m.content;
      chat.appendChild(row);
      state.messages.push({ role: 'assistant', content: m.content });
    }
  }
  // 恢复后滚动到底部
  const wrap = document.getElementById('chat-wrap');
  requestAnimationFrame(() => { wrap.scrollTop = wrap.scrollHeight; });
}

/** 清空对话 */
export function clearChat() {
  const chat = document.getElementById('chat');
  chat.innerHTML = '';
  state.toolCards = {};
  state.messages = [];
  state.lastRole = '';
  state.lastMsgTime = 0;
  localStorage.removeItem(SETTINGS_STORAGE_KEYS.CHAT);
}
