import { state } from './state.js';
import { CHAT_SAVE_DEBOUNCE_MS, CHAT_MAX_STORED, SETTINGS_STORAGE_KEYS } from './config.js';
import { esc, createAssistantRow, addToolCard, updateToolCard, finalizeDelta, attachTokenLabel } from './chat.js';

/* ---- 代码块 / Markdown 渲染辅助 ---- */

export function createCodeBlock(content, language) {
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

export function createMarkdownBubble(content) {
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

/** 从 DOM 读取消息并保存到 localStorage（增量保存，含工具卡片） */
export function saveChat() {
  const rows = document.querySelectorAll('#chat .msg-row');
  const msgs = [];
  for (const row of rows) {
    if (row.classList.contains('user')) {
      const bubble = row.querySelector('.msg-bubble');
      if (!bubble) continue;
      // 检查是否有图片
      const images = bubble.querySelectorAll('.msg-image');
      if (images.length > 0) {
        const content = [];
        for (const img of images) {
          content.push({ type: 'image', source: img.src, mime: 'image/png' });
        }
        // 提取文本（排除图片 alt 文本）
        const text = bubble.textContent.trim();
        if (text) content.push({ type: 'text', content: text });
        msgs.push({ role: 'user', content });
      } else {
        msgs.push({ role: 'user', content: bubble.textContent });
      }
    } else if (row.classList.contains('assistant')) {
      const textSpan = row.querySelector('.msg-text');
      if (textSpan && textSpan.textContent.trim()) {
        msgs.push({ role: 'assistant', content: textSpan.textContent });
      }
    }
  }

  // 采集工具卡片数据
  const toolCards = document.querySelectorAll('#chat .tool-card');
  for (const card of toolCards) {
    const id = card.id.replace('tool-', '');
    const name = card.querySelector('.tool-name')?.textContent || '';
    // 从 data 属性恢复结构化参数（避免字符串解析的逗号问题）
    let args = {};
    try {
      const raw = card.dataset.toolArgs;
      if (raw) args = JSON.parse(raw);
    } catch (_) {
      // 兼容旧格式：回退到文本解析
      const argsEl = card.querySelector('.tool-card-args');
      const argsText = argsEl?.textContent || '';
      if (argsText) {
        argsText.split(', ').forEach(pair => {
          const colon = pair.indexOf(': ');
          if (colon > 0) args[pair.slice(0, colon)] = pair.slice(colon + 2);
        });
      }
    }
    const resultContent = card.querySelector('.tool-result-content')?.textContent || '';
    const success = card.classList.contains('done');
    const fail = card.classList.contains('fail');
    const executing = card.classList.contains('executing');
    msgs.push({
      role: 'tool_card',
      call_id: id,
      name: name,
      args: args,
      content: resultContent,
      success: executing ? undefined : success,
      fail: fail || undefined,
      executing: executing || undefined,
    });
  }

  state.messages = msgs;
  try {
    localStorage.setItem(SETTINGS_STORAGE_KEYS.CHAT, JSON.stringify(msgs.slice(-CHAT_MAX_STORED)));
  } catch (e) {
    if (e.name === 'QuotaExceededError') {
      console.warn('localStorage 配额已满，丢弃最旧消息后重试');
      try {
        localStorage.setItem(SETTINGS_STORAGE_KEYS.CHAT, JSON.stringify(msgs.slice(-Math.floor(CHAT_MAX_STORED / 2))));
      } catch (_) { /* 静默降级 */ }
    }
  }
}

/** 从数组 content 构建用户气泡 DOM */
function buildUserBubble(content) {
  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  if (Array.isArray(content)) {
    for (const part of content) {
      if (part.type === 'image' && part.source) {
        const img = document.createElement('img');
        img.className = 'msg-image';
        img.src = part.source;
        img.loading = 'lazy';
        img.onclick = () => {
          // 动态导入 lightbox（避免循环依赖）
          import('./chat.js').then(m => m.openLightbox(part.source));
        };
        bubble.appendChild(img);
      }
    }
    const texts = content.filter(p => p.type === 'text').map(p => p.content).join('');
    if (texts) {
      const span = document.createElement('span');
      span.textContent = texts;
      bubble.appendChild(span);
    }
  } else {
    bubble.textContent = content;
  }
  return bubble;
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
    if (!m.content && m.role !== 'tool_card') continue;
    if (m.role === 'user') {
      const row = document.createElement('div');
      row.className = 'msg-row user';
      row.appendChild(buildUserBubble(m.content));
      const avatar = document.createElement('div');
      avatar.className = 'msg-avatar';
      avatar.textContent = '😎';
      row.appendChild(avatar);
      chat.appendChild(row);
      state.messages.push({ role: 'user', content: m.content });
    } else if (m.role === 'tool_card') {
      // 重建工具卡片
      const cardId = addToolCard(m.name, m.call_id, m.args || {});
      if (m.executing) {
        // 仍在执行中 — 保持 executing 状态
      } else {
        updateToolCard(cardId, {
          success: m.success !== false,
          content: m.content || '',
          error: m.fail ? m.content : undefined,
          duration_ms: 0,
          truncated: false,
        });
      }
    } else {
      state.lastRole = 'assistant';
      const { row, textSpan } = createAssistantRow();
      textSpan.textContent = typeof m.content === 'string' ? m.content : '';
      chat.appendChild(row);
      state.messages.push({ role: 'assistant', content: m.content });
    }
  }
  // 恢复后重置流式状态，防止下次 delta 合并到历史气泡
  finalizeDelta();
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
    // 工具结果消息 — 更新对应工具卡片
    if (m.role === 'tool') {
      updateToolCard(m.tool_call_id, {
        success: m.success !== false,
        content: m.content || '',
        error: m.error,
        error_type: m.error_type,
        duration_ms: m.duration_ms || 0,
        truncated: m.truncated || false,
      });
      continue;
    }

    // assistant 消息可能带有 tool_calls — 先创建工具卡片占位
    const toolCalls = m.tool_calls || [];
    for (const tc of toolCalls) {
      let args = {};
      try {
        args = JSON.parse(tc.function?.arguments || '{}');
      } catch { /* ignore parse errors */ }
      addToolCard(tc.function?.name || 'unknown', tc.id, args);
    }

    const parts = m.parts || [];
    const ct = m.content_type || {};

    if (m.role === 'user') {
      const fullContent = m.content || [];
      if (Array.isArray(fullContent) && fullContent.some(p => p.type === 'image')) {
        // 多模态用户消息 — 使用完整 content 渲染图片
        const row = document.createElement('div');
        row.className = 'msg-row user';
        row.appendChild(buildUserBubble(fullContent));
        const avatar = document.createElement('div');
        avatar.className = 'msg-avatar';
        avatar.textContent = '😎';
        row.appendChild(avatar);
        chat.appendChild(row);
        state.messages.push({ role: 'user', content: fullContent });
      } else {
        for (let i = 0; i < parts.length; i++) {
          const p = parts[i];
          if (!p.content) continue;
          const row = document.createElement('div');
          row.className = 'msg-row user';
          row.innerHTML = `<div class="msg-bubble">${esc(p.content)}</div><div class="msg-avatar">😎</div>`;
          chat.appendChild(row);
          state.messages.push({ role: 'user', content: p.content });
        }
      }
    } else {
      // 渲染 inner_voice（如果存在）——在公开回复之前
      if (m.inner_voice && state.innerVoiceEnabled) {
        const thinkBubble = document.createElement('div');
        thinkBubble.className = 'inner-voice-bubble';
        thinkBubble.textContent = m.inner_voice;
        chat.appendChild(thinkBubble);
      }
      for (let i = 0; i < parts.length; i++) {
        const p = parts[i];
        if (!p.content) continue;
        if (ct.code && ct.code.includes(i)) {
          const row = createCodeBlock(p.content, p.language || '');
          chat.appendChild(row);
          state.messages.push({ role: 'assistant', content: p.content });
        } else if (ct.markdown && ct.markdown.includes(i)) {
          const row = createMarkdownBubble(p.content);
          chat.appendChild(row);
          state.messages.push({ role: 'assistant', content: p.content });
        } else {
          // 每个 text part 创建独立气泡（保持分句显示）
          const { row, textSpan } = createAssistantRow();
          textSpan.textContent = p.content;
          chat.appendChild(row);
          state.messages.push({ role: 'assistant', content: p.content });
        }
      }
      // 如果该消息附带了 token_usage，渲染 token 标签
      if (m.token_usage && m.token_usage.rounds && m.token_usage.rounds.length) {
        attachTokenLabel(m.token_usage.rounds, m.token_usage.turn_total);
      }
    }
  }

  // 同步到 localStorage 作为缓存
  saveChat();
  // 重置流式状态，防止下次 delta 合并到历史气泡
  finalizeDelta();

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
