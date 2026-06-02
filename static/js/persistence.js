import { state } from './state.js';
import { CHAT_SAVE_DEBOUNCE_MS, CHAT_MAX_STORED, SETTINGS_STORAGE_KEYS } from './config.js';
import { esc, createAssistantRow } from './chat.js';

/* ---- 代码块 / Markdown 渲染辅助 ---- */

function createCodeBlock(content, language) {
  const row = document.createElement('div');
  row.className = 'msg-row assistant';

  const langLabel = language || 'code';
  const highlighted = typeof hljs !== 'undefined'
    ? hljs.highlight(content, { language: language || 'plaintext' }).value
    : esc(content);

  row.innerHTML =
    `<div class="msg-avatar" aria-hidden="true">🌸</div>` +
    `<div class="code-block">` +
      `<div class="code-header">` +
        `<span class="code-lang">${esc(langLabel)}</span>` +
        `<button class="code-copy" data-code="${esc(content).replace(/"/g, '&quot;')}">复制</button>` +
      `</div>` +
      `<pre><code class="language-${esc(langLabel)}">${highlighted}</code></pre>` +
    `</div>`;

  // 复制按钮事件
  const copyBtn = row.querySelector('.code-copy');
  copyBtn.onclick = async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(content);
      copyBtn.textContent = '已复制';
      copyBtn.classList.add('copied');
      setTimeout(() => {
        copyBtn.textContent = '复制';
        copyBtn.classList.remove('copied');
      }, 2000);
    } catch { /* clipboard denied */ }
  };

  return row;
}

function createMarkdownBubble(content) {
  const row = document.createElement('div');
  row.className = 'msg-row assistant';

  const html = typeof marked !== 'undefined'
    ? marked.parse(content)
    : `<pre>${esc(content)}</pre>`;

  // marked.parse() returns async promise in newer versions, handle both
  const render = (result) => {
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble markdown-body';
    bubble.innerHTML = result;
    row.innerHTML = '<div class="msg-avatar" aria-hidden="true">🌸</div>';
    row.appendChild(bubble);
  };

  if (html instanceof Promise) {
    html.then(render);
  } else {
    render(html);
  }

  return row;
}

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
    const parts = m.parts || [];
    const ct = m.content_type || {};

    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      if (!p.content) continue;

      if (m.role === 'user') {
        const row = document.createElement('div');
        row.className = 'msg-row user';
        row.innerHTML = `<div class="msg-bubble">${esc(p.content)}</div><div class="msg-avatar">😎</div>`;
        chat.appendChild(row);
        state.messages.push({ role: 'user', content: p.content });
      } else if (ct.code && ct.code.includes(i)) {
        state.lastRole = 'assistant';
        const row = createCodeBlock(p.content, p.language || '');
        chat.appendChild(row);
        state.messages.push({ role: 'assistant', content: p.content });
      } else if (ct.markdown && ct.markdown.includes(i)) {
        state.lastRole = 'assistant';
        const row = createMarkdownBubble(p.content);
        chat.appendChild(row);
        state.messages.push({ role: 'assistant', content: p.content });
      } else {
        state.lastRole = 'assistant';
        const { row, textSpan } = createAssistantRow();
        textSpan.textContent = p.content;
        chat.appendChild(row);
        state.messages.push({ role: 'assistant', content: p.content });
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
