import { state } from './state.js';
import { esc, createAssistantRow, addToolCard, updateToolCard, finalizeDelta, attachTokenLabel } from './chat.js';

// 过滤 TTS 内部控制标签（不显示在前端）
const PAUSE_TAG_RE = /<#\d+\.?\d*#>/g;
const CN_ACTION_RE = /[(（][^)）]*[一-鿿][^)）]*[)）]/;

/** 处理历史消息文本：去停顿标签，句首舞台指示单独一行 */
function formatHistoryText(text) {
  return text.replace(PAUSE_TAG_RE, '');
}

/** 如果文本以中文舞台指示开头，返回 {action, rest}，否则 null */
function splitAction(text) {
  const m = text.match(CN_ACTION_RE);
  if (m && text.startsWith(m[0])) {
    const action = m[0];
    const rest = text.slice(action.length).trim();
    return { action, rest: rest || '' };
  }
  return null;
}

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

// 消息缓存已移除 — 每次页面加载从服务端拉取重建。保留 no-op 兼容旧调用。
export function debounceSave() {}
export function saveChat() {}

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

/** 从服务端 session 数据重建整个聊天 */
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
          row.innerHTML = `<div class="msg-bubble">${esc(p.content.replace(PAUSE_TAG_RE, ''))}</div><div class="msg-avatar">😎</div>`;
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
          const formatted = formatHistoryText(p.content);
          const split = splitAction(formatted);
          if (split && split.rest) {
            // 舞台指示单独一行，对话另起一行
            const { row: actionRow, textSpan: actionSpan } = createAssistantRow();
            actionSpan.textContent = split.action;
            chat.appendChild(actionRow);
            const { row, textSpan } = createAssistantRow();
            textSpan.textContent = split.rest;
            chat.appendChild(row);
            state.messages.push({ role: 'assistant', content: p.content });
          } else {
            const { row, textSpan } = createAssistantRow();
            textSpan.textContent = formatted;
            chat.appendChild(row);
            state.messages.push({ role: 'assistant', content: p.content });
          }
        }
      }
      // 如果该消息附带了 token_usage，渲染 token 标签
      if (m.token_usage && m.token_usage.rounds && m.token_usage.rounds.length) {
        attachTokenLabel(m.token_usage.rounds, m.token_usage.turn_total);
      }
    }
  }

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
}
