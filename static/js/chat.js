import { state } from './state.js';
import { esc } from './esc.js';
export { esc };  // 向后兼容：tools.js / persistence.js 等仍从 chat.js 导入 esc
import {
  TIME_DIVIDER_GAP_MS,
  SCROLL_FOLLOW_THRESHOLD,
  COPY_FEEDBACK_MS,
  INPUT_MAX_HEIGHT,
  INPUT_INITIAL_HEIGHT,
} from './config.js';

/** 渲染图片 content parts → HTML（替代 debug.html 中同名全局函数，ES module 作用域隔离） */
function _renderImages(content) {
  if (!content || !Array.isArray(content)) return '';
  const images = content.filter(p => p && p.type === 'image' && p.src);
  if (!images.length) return '';
  return '<div class="msg-images">' + images.map(img =>
    `<img class="msg-image" src="${esc(img.src)}" loading="lazy"
          onclick="event.stopPropagation(); openLightbox('${esc(img.src)}')"
          title="点击放大">`
  ).join('') + '</div>';
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

/* ---- 图片预览管理 ---- */
export function addImageToPreview(url, mime) {
  state.previewImages.push({ url, mime });
  const bar = document.getElementById('preview-bar');

  const item = document.createElement('div');
  item.className = 'preview-item';
  // 使用 encodeURI 而非 esc()——后者会把 & 转为 &amp; 损坏 URL
  item.innerHTML = `<img src="${url.replace(/"/g, '&quot;')}" alt="预览"><span class="preview-remove">×</span>`;

  item.querySelector('.preview-remove').onclick = (e) => {
    e.stopPropagation();
    const idx = state.previewImages.findIndex(p => p.url === url);
    if (idx >= 0) state.previewImages.splice(idx, 1);
    item.remove();
    if (state.previewImages.length === 0) {
      bar.style.display = 'none';
    }
  };

  bar.appendChild(item);
  bar.style.display = 'flex';
}

export function clearPreview() {
  state.previewImages = [];
  const bar = document.getElementById('preview-bar');
  bar.innerHTML = '';
  bar.style.display = 'none';
}

/* ---- 图片气泡渲染 ---- */
export function createImageBubble(source) {
  const img = document.createElement('img');
  img.className = 'msg-image';
  img.src = source;
  img.loading = 'lazy';
  img.onclick = () => openLightbox(source);
  return img;
}

/* ---- Lightbox ---- */
export function openLightbox(url) {
  document.getElementById('lightbox-img').src = url;
  document.getElementById('lightbox').classList.remove('lightbox-hidden');
}

export function closeLightbox() {
  document.getElementById('lightbox').classList.add('lightbox-hidden');
  document.getElementById('lightbox-img').src = '';
}

/* ---- 用户消息（含图片）---- */
export function renderUserMsg(content) {
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

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';

  if (Array.isArray(content)) {
    for (const part of content) {
      if (part.type === 'image' && part.src) {
        bubble.appendChild(createImageBubble(part.src));
      }
    }
    // 文本部分
    const texts = content.filter(p => p.type === 'text').map(p => p.content).join('');
    if (texts) {
      const textEl = document.createElement('span');
      textEl.textContent = texts;
      bubble.appendChild(textEl);
    }
    // 持久化
    state.messages.push({ role: 'user', content });
  } else {
    bubble.textContent = content;
    state.messages.push({ role: 'user', content });
  }

  row.appendChild(bubble);
  const avatar = document.createElement('div');
  avatar.className = 'msg-avatar';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = '😎';
  row.appendChild(avatar);
  document.getElementById('chat').appendChild(row);
  scrollDown();
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
  const argsJson = args ? JSON.stringify(args, null, 2) : '{}';
  const argsSummary = args ? Object.entries(args).map(([k, v]) => `${k}: ${v}`).join(', ') : '';
  const card = document.createElement('div');
  card.className = 'tool-card executing';
  card.id = `tool-${id}`;
  card.dataset.toolArgs = args ? JSON.stringify(args) : '{}';
  card.innerHTML = `
    <div class="tool-card-header" onclick="this.parentElement.classList.toggle('expanded')">
      <span class="tool-icon">🔧</span>
      <span class="tool-name">${esc(name)}</span>
      <span class="tool-status"><span class="tool-spinner"></span> 执行中</span>
      <span class="tool-card-chevron">▾</span>
    </div>
    <div class="tool-card-body">
      <div class="tool-card-args">${esc(argsSummary)}</div>
      <div class="tool-card-section">
        <div class="label">参数</div>
        <div class="value mono">${esc(argsJson)}</div>
      </div>
      <div class="tool-card-section tool-result-section" style="display:none">
        <div class="label">结果</div>
        <div class="value tool-result-content"></div>
      </div>
      <div class="tool-card-error" style="display:none"></div>
    </div>`;
  document.getElementById('chat').appendChild(card);
  state.toolCards[id] = card;
  scrollDown();
  return id;
}

export function updateToolCard(id, data) {
  // data: { success, content, error, error_type, duration_ms, truncated }
  const card = state.toolCards[id];
  if (!card) return;
  card.classList.remove('executing');
  const success = data.success !== false;
  card.classList.add(success ? 'done' : 'fail');

  const status = card.querySelector('.tool-status');
  const icon = card.querySelector('.tool-icon');
  const durationStr = data.duration_ms ? ` ${(data.duration_ms / 1000).toFixed(1)}s` : '';

  if (success) {
    status.innerHTML = `<span style="color:var(--idle)">✓</span> 完成${durationStr}`;
    icon.textContent = '🔧';
    // Show result content if present
    if (data.content) {
      const resultSection = card.querySelector('.tool-result-section');
      const resultContent = card.querySelector('.tool-result-content');
      if (resultSection && resultContent) {
        resultSection.style.display = '';
        const truncated = data.truncated ? ' (已截断)' : '';
        resultContent.textContent = data.content + truncated;
      }
    }
  } else {
    status.innerHTML = `<span style="color:var(--accent)">✗</span> 失败${durationStr}`;
    icon.textContent = '⚠️';
    // Show error details
    const errorEl = card.querySelector('.tool-card-error');
    if (errorEl && data.error) {
      errorEl.style.display = '';
      const et = data.error_type ? `[${data.error_type}] ` : '';
      errorEl.textContent = `❌ ${et}${data.error}`;
    }
    // Auto-expand on failure
    card.classList.add('expanded');
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
    tag.textContent = mood ? `${mood} (${Math.round(intensity * 100)}%)` : '';
    tag.style.color = 'var(--muted)';
  }
}

export function setState(stateName) {
  const el = document.getElementById('char-state');
  if (el) el.textContent = stateName;
  const dot = document.getElementById('state-dot');
  if (dot) dot.className = stateName;
}


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

/* ---- Token 标签 ---- */

/**
 * 将 token_usage 数据附加到最近的 AI 气泡。
 * 每次收到 token_usage 消息时调用，会替换之前的标签。
 */
export function attachTokenLabel(rounds, turnTotal) {
  const rows = document.querySelectorAll('.msg-row.assistant');
  if (!rows.length) return;
  const lastRow = rows[rows.length - 1];
  const bubble = lastRow.querySelector('.msg-bubble');
  if (!bubble) return;

  // 移除旧标签
  const existing = bubble.querySelector('.token-label');
  if (existing) existing.remove();

  const r = rounds || state.currentTurnTokens;
  if (!r || !r.length) return;

  const tt = turnTotal !== undefined ? turnTotal : state.activeTurnTotal;
  const totalStr = tt >= 1000 ? (tt / 1000).toFixed(1) + 'k' : String(tt);

  const label = document.createElement('div');
  label.className = 'token-label';

  // 紧凑摘要
  const summary = document.createElement('span');
  summary.className = 'token-summary';
  summary.textContent = '\u{1FA99} ' + totalStr + ' tokens ▼';
  label.appendChild(summary);

  // 展开明细
  const detail = document.createElement('div');
  detail.className = 'token-detail';
  const lines = r.map(rd =>
    `<div class="token-round">
      <span class="token-round-num">第${rd.round_num + 1}轮</span>
      <span class="token-round-item">prompt: ${formatTokens(rd.prompt_tokens)}</span>
      <span class="token-round-item">completion: ${formatTokens(rd.completion_tokens)}</span>
      <span class="token-round-total">= ${formatTokens(rd.total_tokens)}</span>
    </div>`
  ).join('');
  detail.innerHTML = lines;
  label.appendChild(detail);

  // 点击展开/收起
  label.onclick = (e) => {
    e.stopPropagation();
    label.classList.toggle('expanded');
  };

  bubble.appendChild(label);
}

function formatTokens(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

/**
 * 更新顶栏 session 累计 token 显示。
 */
export function updateSessionTokenDisplay() {
  const bar = document.getElementById('session-token-bar');
  if (!bar) return;
  const st = state.sessionTokens;
  if (!st || !st.total) {
    bar.textContent = '';
    bar.style.display = 'none';
    return;
  }
  const models = Object.entries(st.byModel || {});
  const parts = models.map(([m, t]) => {
    const short = m.split('/').pop();
    return short + ': ' + formatTokens(t);
  });
  bar.textContent = '\u{1FA99} 总计 ' + formatTokens(st.total) + (parts.length ? ' (' + parts.join(', ') + ')' : '');
  bar.style.display = '';
}

// ---- 滚动加载历史 ----
let isLoadingMore = false;
let hasMoreMessages = true;
let earliestMessageId = null;

export function setupScrollLoader() {
  const wrap = document.getElementById('chat-wrap');
  if (!wrap) return;

  wrap.addEventListener('scroll', () => {
    if (wrap.scrollTop <= 80 && !isLoadingMore && hasMoreMessages) {
      loadMoreMessages();
    }
  });
}

export function resetScrollLoader() {
  isLoadingMore = false;
  hasMoreMessages = true;
  earliestMessageId = null;
}

async function loadMoreMessages() {
  isLoadingMore = true;
  const wrap = document.getElementById('chat-wrap');
  const indicator = showLoadingIndicator(wrap);

  try {
    let url = '/api/messages?limit=50';
    if (earliestMessageId) {
      url += '&before=' + encodeURIComponent(earliestMessageId);
    }
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();

    hasMoreMessages = data.has_more;
    const messages = data.messages || [];

    if (messages.length > 0) {
      // 记录当前高度，渲染后修正位置
      const oldHeight = wrap.scrollHeight;

      // messages 是降序（最新在前），翻转后从旧到新插入 DOM 顶部
      prependMessages(messages.slice().reverse());

      // 修正滚动位置
      const newHeight = wrap.scrollHeight;
      wrap.scrollTop = newHeight - oldHeight;

      // 更新最早消息 ID（原列表末尾 = 最早的）
      const earliest = messages[messages.length - 1];
      if (earliest && earliest._id) {
        earliestMessageId = earliest._id;
      }
    }
  } catch (e) {
    const el = document.createElement('div');
    el.style.cssText = 'text-align:center;padding:8px;color:#f44336;font-size:12px;cursor:pointer;';
    el.textContent = '加载失败，点击重试';
    el.onclick = () => { el.remove(); loadMoreMessages(); };
    wrap.insertBefore(el, wrap.firstChild);
  } finally {
    isLoadingMore = false;
    indicator.remove();
  }
}

function showLoadingIndicator(wrap) {
  const el = document.createElement('div');
  el.id = 'load-more-indicator';
  el.style.cssText = 'text-align:center;padding:8px;color:#888;font-size:12px;';
  el.textContent = '加载历史消息...';
  wrap.insertBefore(el, wrap.firstChild);
  return el;
}

function prependMessages(messages) {
  const chat = document.getElementById('chat');
  for (const m of messages) {
    const div = renderArchiveMessage(m);
    if (div) {
      chat.insertBefore(div, chat.firstChild);
    }
  }
}

function renderArchiveMessage(m) {
  const role = m.role;
  if (role === 'tool') {
    const div = document.createElement('div');
    div.className = 'msg-row tool';
    const bubble = document.createElement('div');
    bubble.className = 'bubble tool';
    const parts = m.parts || [];
    bubble.innerHTML = `<span class="tool-name">${esc(m.name || 'tool')}</span>
      ${esc(parts.map(p => p.content).join('').slice(0, 80))}`;
    div.appendChild(bubble);
    return div;
  }

  const div = document.createElement('div');
  div.className = 'msg-row ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble ' + role;

  // inner_voice
  if (m.inner_voice) {
    const iv = document.createElement('div');
    iv.className = 'inner-voice';
    iv.textContent = m.inner_voice;
    bubble.appendChild(iv);
  }

  // parts（API 已统一转换）
  const parts = m.parts || [];
  for (const p of parts) {
    const span = document.createElement('span');
    span.textContent = p.content || '';
    bubble.appendChild(span);
  }

  // 图片
  if (m.content && Array.isArray(m.content)) {
    const imgHtml = _renderImages(m.content);
    if (imgHtml) {
      const imgWrap = document.createElement('div');
      imgWrap.innerHTML = imgHtml;
      bubble.appendChild(imgWrap);
    }
  }

  div.appendChild(bubble);
  return div;
}

// 从 persistence.js 导入 (底部导入解决循环依赖)
import { debounceSave, saveChat } from './persistence.js';
