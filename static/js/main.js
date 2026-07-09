import { state } from './state.js';
import { connect, send } from './ws.js';
import {
  appendDelta, finalizeDelta, addUserMsg, addStatusMsg,
  addToolCard, updateToolCard, showTyping, hideTyping,
  setMood, setState, esc, createAssistantRow, setupInput,
  resetInput, scrollDown, hideNewMsgIndicator,
  addImageToPreview, clearPreview, renderUserMsg, openLightbox, closeLightbox,
  attachTokenLabel, updateSessionTokenDisplay,
} from './chat.js';
import { bufferAudioChunk, finalizeStream } from './audio.js';
import { toggleMic, stopMic, flashWakeIndicator } from './mic.js';
import { setupSettings, updateCapabilityIndicators, setModelTag } from './settings.js';
import { setupTools } from './tools.js';
import { restoreChat, restoreFromServer, createCodeBlock, createMarkdownBubble } from './persistence.js';
import { SEND_COOLDOWN_MS } from './config.js';

/* ---- STT emoji mappings (与服务端同步) ---- */
const EMOTION_EMOJI = {
  HAPPY: '😊', SAD: '😔', ANGRY: '😡', NEUTRAL: '',
  FEARFUL: '😰', DISGUSTED: '🤢', SURPRISED: '😮',
};
const EVENT_EMOJI = {
  BGM: '🎼', Applause: '👏', Laughter: '😀', Cry: '😭',
  Cough: '😷', Sneeze: '🤧',
};
function getEmotionEmoji(tag) { return EMOTION_EMOJI[tag] || ''; }
function getEventEmoji(evt) { return EVENT_EMOJI[evt] || ''; }

/* ---- WebSocket 消息处理 ---- */
function handleMessage(msg) {
  switch (msg.type) {
    case 'session':
      restoreFromServer(msg.messages);
      if (msg.token_stats) {
        state.sessionTokens = msg.token_stats;
        updateSessionTokenDisplay();
      }
      break;
    case 'capabilities':
      state.sttAvailable = msg.stt !== false;
      state.ttsAvailable = msg.tts !== false;
      state.llmModel = msg.llm_model || '';
      state.live2dStaticImage = msg.live2d_static_image || '';
      // 如果 Live2D 已关闭且有静态图片元素，更新 src
      const staticImg = document.getElementById('live2d-static-img');
      if (staticImg) {
        staticImg.src = state.live2dStaticImage || '';
        staticImg.style.display = state.live2dStaticImage ? '' : 'none';
      }
      updateCapabilityIndicators();
      break;
    case 'delta':
      appendDelta(msg.content, msg.stream_id);
      break;
    case 'message':
      if (msg.content) {
        hideTyping();
        state.lastRole = 'assistant';
        const { row, textSpan } = createAssistantRow();
        textSpan.textContent = msg.content;
        document.getElementById('chat').appendChild(row);
        if (msg.mood) {
          const moodEl = document.createElement('div'); moodEl.className = 'msg-mood';
          moodEl.textContent = `心情: ${msg.mood}`;
          document.getElementById('chat').appendChild(moodEl);
        }
        scrollDown();
        state.messages.push({ role: 'assistant', content: msg.content });
      }
      finalizeDelta();
      break;
    case 'token_usage':
      state.currentTurnTokens = msg.turn ? msg.turn.rounds || [] : [];
      state.activeTurnTotal = msg.turn ? msg.turn.turn_total || 0 : 0;
      if (msg.session) {
        state.sessionTokens = msg.session;
      }
      updateSessionTokenDisplay();
      // attachTokenLabel() 延迟到 state=idle 时调用，确保标签挂在最后一句话上
      break;
    case 'expression': setMood(msg.expression, msg.intensity); break;
    case 'state':
      setState(msg.state);
      if (msg.state === 'executing') { state.lastRole = null; }
      if (msg.state === 'idle') { attachTokenLabel(); }  // 回复完全结束后，把 token 标签移到最后一句话
      break;
    case 'audio': bufferAudioChunk(msg.stream_id || 0, msg.seq || 0, msg.data); break;
    case 'audio_end': finalizeStream(msg.stream_id || 0, msg.format); break;
    case 'typing':
      if (msg.typing) { showTyping(); }
      else { hideTyping(); }
      break;
    case 'transcription': {
      let display = msg.text;
      const emotionEmoji = getEmotionEmoji(msg.emotion);
      if (emotionEmoji) display += ' ' + emotionEmoji;
      if (msg.events && msg.events.length > 0) {
        for (const evt of msg.events) {
          const evtEmoji = getEventEmoji(evt);
          if (evtEmoji) display += ' ' + evtEmoji;
        }
      }
      addUserMsg('🎤 ' + display);
      break;
    }
    case 'action':
      if (msg.action === 'tool_executing') {
        addToolCard(msg.params.tool, msg.params.call_id, msg.params.args);
      } else if (msg.action === 'tool_done') {
        updateToolCard(msg.params.call_id, msg.params);
      }
      break;
    case 'command':
      if (msg.command === 'tool_approval') {
        showApprovalDialog(msg.params);
      } else if (msg.command === 'block') {
        const p = msg.params || {};
        const chat = document.getElementById('chat');
        if (p.block_type === 'code') {
          chat.appendChild(createCodeBlock(p.content, p.language || ''));
        } else if (p.block_type === 'markdown') {
          chat.appendChild(createMarkdownBubble(p.content));
        }
        scrollDown();
      } else if (msg.command === 'inner_voice') {
        showInnerVoice(msg.params.content);
      }
      break;
    case 'wake':
      if (state.wakeEnabled) {
        flashWakeIndicator();
      }
      break;
    case 'switch_model_result':
      if (msg.ok) {
        addStatusMsg(msg.service.toUpperCase() + ' 已切换至 ' + msg.model);
        if (msg.service === 'llm') {
          state.llmModel = msg.model;
          setModelTag(msg.model);
        }
      } else {
        addStatusMsg(msg.service.toUpperCase() + ' 切换失败: ' + (msg.error || '未知错误'));
      }
      break;
    case 'error': console.warn('Server:', msg.message); break;
    default: console.warn('Unknown message type:', msg.type, msg); break;
  }
}

/* ---- 发送文本（支持多模态图片）---- */
function sendText() {
  const textInput = document.getElementById('text-input');
  const text = textInput.value.trim();
  const hasImages = state.previewImages.length > 0;
  if (!text && !hasImages) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;

  if (hasImages) {
    const content = [];
    for (const img of state.previewImages) {
      content.push({ type: 'image', source: img.url, mime: img.mime });
    }
    if (text) content.push({ type: 'text', content: text });
    send({ type: 'text', content });
    renderUserMsg(content);
  } else {
    send({ type: 'text', content: text });
    addUserMsg(text);
  }

  clearPreview();
  resetInput();
  const btn = document.getElementById('send-btn');
  btn.classList.add('sent');
  btn.disabled = true;
  setTimeout(() => { btn.classList.remove('sent'); btn.disabled = false; }, SEND_COOLDOWN_MS);
}

/* ---- 图片上传 ---- */
async function uploadImage(file) {
  const form = new FormData();
  form.append('image', file);
  try {
    const resp = await fetch('/api/images', { method: 'POST', body: form });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      addStatusMsg('图片上传失败: ' + (err.error || resp.status));
      return;
    }
    const { url, mime } = await resp.json();
    addImageToPreview(url, mime);
  } catch (e) {
    addStatusMsg('图片上传失败: ' + e.message);
  }
}

/* ---- 初始化 ---- */
function init() {
  restoreChat();

  connect(handleMessage);

  setupInput();

  setupSettings();
  setupTools();

  document.getElementById('send-btn').onclick = sendText;

  document.getElementById('mic-btn').onclick = toggleMic;

  // 附件按钮 → 触发文件选择
  document.getElementById('attach-btn').onclick = () => {
    document.getElementById('file-input').click();
  };

  // 文件选择 → 上传
  document.getElementById('file-input').onchange = (e) => {
    const file = e.target.files[0];
    if (file) uploadImage(file);
    e.target.value = '';  // 允许重复选同一文件
  };

  // 粘贴图片
  document.addEventListener('paste', (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        uploadImage(item.getAsFile());
        break;
      }
    }
  });

  // 拖拽图片到聊天区
  const chatWrap = document.getElementById('chat-wrap');
  chatWrap.addEventListener('dragover', (e) => {
    e.preventDefault();
    chatWrap.classList.add('drag-over');
  });
  chatWrap.addEventListener('dragleave', () => {
    chatWrap.classList.remove('drag-over');
  });
  chatWrap.addEventListener('drop', (e) => {
    e.preventDefault();
    chatWrap.classList.remove('drag-over');
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (file && file.type.startsWith('image/')) uploadImage(file);
  });

  // Lightbox 关闭
  document.getElementById('lightbox').onclick = (e) => {
    if (e.target !== document.getElementById('lightbox-img')) closeLightbox();
  };

  chatWrap.addEventListener('scroll', function () {
    const w = this;
    const wasUp = state.userScrolledUp;
    state.userScrolledUp = w.scrollTop + w.clientHeight < w.scrollHeight - 60;
    if (!state.userScrolledUp && wasUp) hideNewMsgIndicator();
  });

  // 键盘快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // 如果 lightbox 打开，先关闭
      const lb = document.getElementById('lightbox');
      if (!lb.classList.contains('lightbox-hidden')) {
        closeLightbox();
        return;
      }
      send({ type: 'interrupt' });
      stopMic();
    }
  });
}

/* ---- HITL 审批对话框 ---- */
let _approvalSeq = 0;

function showInnerVoice(content) {
  if (!state.innerVoiceEnabled) return;
  if (!content) return;
  const chat = document.getElementById('chat');
  const bubble = document.createElement('div');
  bubble.className = 'inner-voice-bubble';
  bubble.textContent = content;
  chat.appendChild(bubble);
  scrollDown();
}

function showApprovalDialog(params) {
  const { tool, params: toolParams, request_id } = params;
  const seq = ++_approvalSeq;
  const approveId = 'approval-approve-' + seq;
  const denyId = 'approval-deny-' + seq;

  const overlay = document.createElement('div');
  overlay.className = 'approval-overlay';
  overlay.id = 'approval-overlay-' + seq;

  const dialog = document.createElement('div');
  dialog.className = 'approval-dialog';
  dialog.innerHTML =
    '<div class="approval-icon">&#9888;</div>' +
    '<div class="approval-title">工具操作确认</div>' +
    '<div class="approval-tool">工具: <strong>' + esc(tool) + '</strong></div>' +
    '<div class="approval-detail">参数: ' + esc(toolParams || '(无)') + '</div>' +
    '<div class="approval-buttons">' +
      '<button class="approval-approve" id="' + approveId + '">允许</button>' +
      '<button class="approval-deny" id="' + denyId + '">拒绝</button>' +
    '</div>';

  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  function respond(approved) {
    send({ type: 'tool_approval', request_id: request_id, approved: approved });
    document.body.removeChild(overlay);
  }

  document.getElementById(approveId).onclick = function() { respond(true); };
  document.getElementById(denyId).onclick = function() { respond(false); };

  function onKey(e) {
    if (e.key === 'Escape') {
      respond(false);
    }
  }
  document.addEventListener('keydown', onKey);
  // 弹窗关闭后自动清理监听器（respond 内部已 remove overlay）
  const origRespond = respond;
  respond = function(v) {
    document.removeEventListener('keydown', onKey);
    origRespond(v);
  };
}

init();
