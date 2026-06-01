import { state } from './state.js';
import { connect, send } from './ws.js';
import {
  appendDelta, finalizeDelta, addUserMsg, addStatusMsg,
  addToolCard, updateToolCard, showTyping, hideTyping,
  setMood, setState, esc, createAssistantRow, setupInput,
  resetInput, scrollDown, hideNewMsgIndicator,
} from './chat.js';
import { bufferAudioChunk, finalizeStream } from './audio.js';
import { toggleMic, stopMic } from './mic.js';
import { setupSettings } from './settings.js';
import { restoreChat } from './persistence.js';
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
        addToolCard(msg.params.tool, msg.params.args);
      } else if (msg.action === 'tool_done') {
        updateToolCard(msg.params.tool, msg.params.success);
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

init();
