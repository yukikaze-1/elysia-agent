import { state } from './state.js';
import { connect, send } from './ws.js';
import {
  appendDelta, finalizeDelta, addUserMsg, addStatusMsg,
  addToolCard, updateToolCard, showTyping, hideTyping,
  setMood, setState, esc, createAssistantRow, setupInput,
  resetInput, scrollDown, hideNewMsgIndicator,
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
      break;
    case 'capabilities':
      state.sttAvailable = msg.stt !== false;
      state.ttsAvailable = msg.tts !== false;
      state.llmModel = msg.llm_model || '';
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
    case 'expression': setMood(msg.expression, msg.intensity); break;
    case 'state':
      setState(msg.state);
      if (msg.state === 'executing') { state.lastRole = null; }
      break;
    case 'audio': bufferAudioChunk(msg.stream_id || 0, msg.seq || 0, msg.data); break;
    case 'audio_end': finalizeStream(msg.stream_id || 0); break;
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
        updateToolCard(msg.params.call_id, msg.params.success);
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
  }
}

/* ---- 发送文本 ---- */
function sendText() {
  const textInput = document.getElementById('text-input');
  const text = textInput.value.trim();
  if (!text || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  send({ type: 'text', content: text });
  addUserMsg(text);
  resetInput();
  const btn = document.getElementById('send-btn');
  btn.classList.add('sent');
  btn.disabled = true;
  setTimeout(() => { btn.classList.remove('sent'); btn.disabled = false; }, SEND_COOLDOWN_MS);
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

  document.getElementById('chat-wrap').addEventListener('scroll', function () {
    const w = this;
    const wasUp = state.userScrolledUp;
    state.userScrolledUp = w.scrollTop + w.clientHeight < w.scrollHeight - 60;
    if (!state.userScrolledUp && wasUp) hideNewMsgIndicator();
  });

  // 键盘快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      send({ type: 'interrupt' });
      stopMic();
    }
  });
}

/* ---- HITL 审批对话框 ---- */
function showApprovalDialog(params) {
  const { tool, params: toolParams, request_id } = params;

  const overlay = document.createElement('div');
  overlay.className = 'approval-overlay';
  overlay.id = 'approval-overlay';

  const dialog = document.createElement('div');
  dialog.className = 'approval-dialog';
  dialog.innerHTML =
    '<div class="approval-icon">&#9888;</div>' +
    '<div class="approval-title">工具操作确认</div>' +
    '<div class="approval-tool">工具: <strong>' + esc(tool) + '</strong></div>' +
    '<div class="approval-detail">参数: ' + esc(toolParams || '(无)') + '</div>' +
    '<div class="approval-buttons">' +
      '<button class="approval-approve" id="approval-approve">允许</button>' +
      '<button class="approval-deny" id="approval-deny">拒绝</button>' +
    '</div>';

  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  function respond(approved) {
    send({ type: 'tool_approval', request_id: request_id, approved: approved });
    document.body.removeChild(overlay);
  }

  document.getElementById('approval-approve').onclick = function() { respond(true); };
  document.getElementById('approval-deny').onclick = function() { respond(false); };

  function onKey(e) {
    if (e.key === 'Escape') {
      respond(false);
      document.removeEventListener('keydown', onKey);
    }
  }
  document.addEventListener('keydown', onKey);
}

init();
