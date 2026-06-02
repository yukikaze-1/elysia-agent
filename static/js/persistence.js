import { state } from './state.js';
import { CHAT_SAVE_DEBOUNCE_MS, CHAT_MAX_STORED, SETTINGS_STORAGE_KEYS } from './config.js';
import { esc, createAssistantRow } from './chat.js';

/** 防抖保存 */
export function debounceSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveChat, CHAT_SAVE_DEBOUNCE_MS);
}

/** 从 DOM 读取消息并保存到 localStorage（增量保存） */
export function saveChat() {
  const rows = document.querySelectorAll('#chat .msg-row');
  const msgs = [];
  for (const row of rows) {
    if (row.classList.contains('user')) {
      const bubble = row.querySelector('.msg-bubble');
      if (bubble) msgs.push({ role: 'user', content: bubble.textContent });
    } else if (row.classList.contains('assistant')) {
      const textSpan = row.querySelector('.msg-text');
      if (textSpan && textSpan.textContent.trim()) {
        msgs.push({ role: 'assistant', content: textSpan.textContent });
      }
    }
  }
  state.messages = msgs;
  localStorage.setItem(SETTINGS_STORAGE_KEYS.CHAT, JSON.stringify(msgs.slice(-CHAT_MAX_STORED)));
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

/** 从服务端 session 数据重建整个聊天（替换 localStorage 缓存） */
export function restoreFromServer(messages) {
  const chat = document.getElementById('chat');
  chat.innerHTML = '';
  state.messages = [];
  state.toolCards = {};
  state.lastRole = '';
  state.lastMsgTime = 0;
  state.currentDelta = null;
  state.nextBubbleQueue = [];
  state.streamBubbles = {};
  state.playingBubble = null;
  state.typingEl = null;
  state.audioPlaying = false;
  state.audioQueue = [];
  state.audioChunks = {};
  clearTimeout(state.saveTimer);
  state.saveTimer = null;

  for (const m of messages) {
    if (m.role === 'user') {
      const row = document.createElement('div');
      row.className = 'msg-row user';
      row.innerHTML = `<div class="msg-bubble">${esc(m.content)}</div><div class="msg-avatar">😎</div>`;
      chat.appendChild(row);
      state.messages.push({ role: 'user', content: m.content });
    } else {
      // assistant: 优先使用 sentences 数组，否则 fallback 到完整 content
      const sentences = (Array.isArray(m.sentences) && m.sentences.length > 0)
        ? m.sentences
        : [m.content];
      for (const sentence of sentences) {
        if (!sentence) continue;
        state.lastRole = 'assistant';
        const { row, textSpan } = createAssistantRow();
        textSpan.textContent = sentence;
        chat.appendChild(row);
        state.messages.push({ role: 'assistant', content: sentence });
      }
    }
  }

  // 同步到 localStorage 作为缓存
  saveChat();

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
