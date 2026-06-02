import { state } from './state.js';
import {
  TIME_DIVIDER_GAP_MS,
  SCROLL_FOLLOW_THRESHOLD,
  COPY_FEEDBACK_MS,
  INPUT_MAX_HEIGHT,
  INPUT_INITIAL_HEIGHT,
} from './config.js';

/* ---- 工具函数 ---- */
export function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

/* ---- 统一工厂: 创建助理消息行 ---- */
export function createAssistantRow() {
  const row = document.createElement('div');
  row.className = 'msg-row assistant';
  row.setAttribute('role', 'article');
  row.setAttribute('aria-label', '助理消息');
  row.innerHTML = '<div class="msg-avatar" aria-hidden="true">🌸</div>';

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';

  const textSpan = document.createElement('span');
  textSpan.className = 'msg-text';
  bubble.appendChild(textSpan);

  const copyBtn = document.createElement('span');
  copyBtn.className = 'copy-btn';
  copyBtn.textContent = '复制';
  copyBtn.onclick = async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(textSpan.textContent.trim());
      copyBtn.textContent = '已复制';
      copyBtn.classList.add('copied');
      setTimeout(() => {
        copyBtn.textContent = '复制';
        copyBtn.classList.remove('copied');
      }, COPY_FEEDBACK_MS);
    } catch { /* clipboard denied */ }
  };
  bubble.appendChild(copyBtn);
  row.appendChild(bubble);

  return { row, bubble, textSpan };
}

/* ---- Delta (流式追加) ---- */
export function appendDelta(text, streamId) {
  if (!state.currentDelta) {
    const now = Date.now();
    if (now - state.lastMsgTime > TIME_DIVIDER_GAP_MS && state.lastMsgTime > 0) {
      const div = document.createElement('div');
      div.className = 'time-divider';
      div.textContent = new Date(now).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
      document.getElementById('chat').appendChild(div);
    }
    state.lastMsgTime = now;

    if (state.lastRole === 'assistant') {
      const rows = document.querySelectorAll('.msg-row.assistant');
      const last = rows[rows.length - 1];
      if (last) {
        const textSpan = last.querySelector('.msg-text');
        if (textSpan) {
          state.currentDelta = textSpan;
          state.currentDelta.textContent += text;
          return;
        }
      }
    }

    state.lastRole = 'assistant';
    const { row, bubble, textSpan } = createAssistantRow();

    if (streamId != null) state.streamBubbles[streamId] = bubble;

    if (state.audioPlaying) {
      row.style.animation = 'none';
      row.style.opacity = '0';
      row.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
      row.style.transform = 'translateY(8px)';
      state.nextBubbleQueue.push(row);
    } else {
      document.getElementById('chat').appendChild(row);
    }
    state.currentDelta = textSpan;
  }
  state.currentDelta.textContent += text;
  scrollDown();
  debounceSave();
}

export function finalizeDelta() {
  if (state.currentDelta) {
    state.currentDelta = null;
  }
  state.lastRole = null;
}

/* ---- 用户消息 ---- */
export function addUserMsg(text) {
  finalizeDelta();
  state.nextBubbleQueue = [];
  state.streamBubbles = {};
  if (state.playingBubble) {
    state.playingBubble.classList.remove('playing', 'pulse');
    state.playingBubble = null;
  }
  state.lastRole = 'user';
  const row = document.createElement('div');
  row.className = 'msg-row user';
  row.setAttribute('role', 'article');
  row.setAttribute('aria-label', '用户消息');
  row.innerHTML = `<div class="msg-bubble">${esc(text)}</div><div class="msg-avatar" aria-hidden="true">😎</div>`;
  document.getElementById('chat').appendChild(row);
  scrollDown();

  state.messages.push({ role: 'user', content: text });
  saveChat();
}

/* ---- 状态消息 ---- */
export function addStatusMsg(text) {
  finalizeDelta();
  const row = document.createElement('div');
  row.className = 'msg-row assistant';
  row.innerHTML = `<div class="msg-avatar"></div><div class="msg-status">${esc(text)}</div>`;
  document.getElementById('chat').appendChild(row);
  scrollDown();
}

/* ---- Tool Cards ---- */

export function addToolCard(name, callId, args) {
  finalizeDelta();
  const id = callId || `${name}-${Date.now()}`;
  const argsStr = args ? Object.entries(args).map(([k, v]) => `${k}: ${v}`).join(', ') : '';
  const card = document.createElement('div');
  card.className = 'tool-card executing';
  card.id = `tool-${id}`;
  card.innerHTML = `
    <div class="tool-card-header" onclick="this.parentElement.classList.toggle('expanded')">
      <span class="tool-icon">🔧</span>
      <span class="tool-name">${esc(name)}</span>
      <span class="tool-status"><span class="tool-spinner"></span> 执行中</span>
      <span class="tool-card-chevron">▾</span>
    </div>
    <div class="tool-card-body">
      <div class="tool-card-args">${esc(argsStr)}</div>
    </div>`;
  document.getElementById('chat').appendChild(card);
  state.toolCards[id] = card;
  scrollDown();
  return id;
}

export function updateToolCard(id, success) {
  const card = state.toolCards[id];
  if (!card) return;
  card.classList.remove('executing');
  card.classList.add(success ? 'done' : 'fail');
  const status = card.querySelector('.tool-status');
  const icon = card.querySelector('.tool-icon');
  if (success) {
    status.innerHTML = '<span style="color:var(--idle)">✓</span> 完成';
    icon.textContent = '🔧';
  } else {
    status.innerHTML = '<span style="color:var(--accent)">✗</span> 失败';
    icon.textContent = '⚠️';
  }
}

/* ---- 打字指示器 ---- */
export function showTyping() {
  if (state.typingEl) return;
  state.typingEl = document.createElement('div');
  state.typingEl.className = 'msg-row assistant';
  state.typingEl.innerHTML = '<div class="msg-avatar">🌸</div><div class="msg-bubble typing-dots"><span></span><span></span><span></span></div>';
  document.getElementById('chat').appendChild(state.typingEl);
  scrollDown();
}

export function hideTyping() {
  if (state.typingEl) {
    state.typingEl.remove();
    state.typingEl = null;
  }
}

/* ---- 心情 / 状态 ---- */
export function setMood(mood, intensity) {
  const tag = document.getElementById('mood-tag');
  if (tag) {
    tag.textContent = mood ? `${moodEmoji(mood)} ${mood} ${Math.round(intensity * 100)}%` : '';
    tag.style.color = moodColor(mood);
  }
}

export function setState(stateName) {
  const el = document.getElementById('char-state');
  if (el) el.textContent = stateName;
  const dot = document.getElementById('state-dot');
  if (dot) dot.className = stateName;
}

const MOOD_EMOJI = { happy: '😊', sad: '😢', excited: '🤩', curious: '🤔', neutral: '😐' };
const MOOD_COLOR = { happy: 'var(--happy)', sad: 'var(--sad)', excited: 'var(--excited)', neutral: 'var(--muted)' };

function moodEmoji(m) { return MOOD_EMOJI[m] || ''; }
function moodColor(m) { return MOOD_COLOR[m] || 'var(--muted)'; }

/* ---- 滚动 ---- */
export function scrollDown() {
  if (state.userScrolledUp) { showNewMsgIndicator(); return; }
  const wrap = document.getElementById('chat-wrap');
  requestAnimationFrame(() => { wrap.scrollTop = wrap.scrollHeight; });
}

function showNewMsgIndicator() {
  let el = document.getElementById('new-msg-indicator');
  if (!el) {
    el = document.createElement('div');
    el.id = 'new-msg-indicator';
    el.textContent = '↓ 新消息';
    el.onclick = () => {
      state.userScrolledUp = false;
      scrollDown();
    };
    document.getElementById('chat-wrap').appendChild(el);
  }
  el.classList.add('visible');
}

export function hideNewMsgIndicator() {
  const el = document.getElementById('new-msg-indicator');
  if (el) el.classList.remove('visible');
}

/* ---- 输入框 ---- */
export function setupInput() {
  const textInput = document.getElementById('text-input');
  textInput.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      document.getElementById('send-btn').click();
    }
    setTimeout(() => {
      textInput.style.height = 'auto';
      textInput.style.height = Math.min(textInput.scrollHeight, INPUT_MAX_HEIGHT) + 'px';
    }, 0);
  };
}

export function resetInput() {
  const textInput = document.getElementById('text-input');
  textInput.value = '';
  textInput.style.height = INPUT_INITIAL_HEIGHT + 'px';
}

/* ---- 气泡排队弹出 ---- */
export function revealNextQueuedBubble() {
  if (!state.nextBubbleQueue.length) return;
  const row = state.nextBubbleQueue.shift();
  document.getElementById('chat').appendChild(row);
  requestAnimationFrame(() => {
    row.style.opacity = '1';
    row.style.transform = 'translateY(0)';
  });
  const bubble = row.querySelector('.msg-bubble');
  if (bubble && (state.audioPlaying || state.audioQueue.length > 0)) {
    highlightPlaying(bubble);
  }
  scrollDown();
  debounceSave();
}

/* ---- TTS 高亮 (供 audio.js 调用) ---- */
export function highlightPlaying(bubble) {
  if (state.playingBubble) {
    state.playingBubble.classList.remove('playing', 'pulse');
  }
  state.playingBubble = bubble;
  bubble.classList.add('playing');
  bubble.classList.add('pulse');
}

export function clearPlayingHighlight() {
  if (state.playingBubble) {
    state.playingBubble.classList.remove('playing', 'pulse');
    state.playingBubble = null;
  }
}

// 从 persistence.js 导入 (底部导入解决循环依赖)
import { debounceSave, saveChat } from './persistence.js';
