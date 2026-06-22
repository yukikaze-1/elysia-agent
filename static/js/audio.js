import { state } from './state.js';
import { STREAM_BUBBLE_TTL_MS } from './config.js';
import {
  highlightPlaying,
  clearPlayingHighlight,
  revealNextQueuedBubble,
} from './chat.js';

/** Base64 解码为 Uint8Array */
function decodeBase64(b64) {
  try {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } catch (e) {
    console.warn('Audio: 无效 base64 数据', e.message);
    return new Uint8Array(0);
  }
}

/** 合并 Uint8Array 数组 */
function concatChunks(chunks) {
  let total = 0;
  for (const c of chunks) total += c.length;
  const buf = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) { buf.set(c, off); off += c.length; }
  return buf;
}

export function bufferAudioChunk(streamId, seq, b64) {
  if (!state.audioChunks[streamId]) state.audioChunks[streamId] = [];
  state.audioChunks[streamId].push(decodeBase64(b64));
}

export function finalizeStream(streamId) {
  if (!state.ttsEnabled) return;
  const chunks = state.audioChunks[streamId];
  if (!chunks || chunks.length === 0) return;
  delete state.audioChunks[streamId];

  const buf = concatChunks(chunks);
  const url = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
  state.audioQueue.push({ url, streamId });

  // 设置超时清理：如果这个 stream 的 bubble 映射在 TTL 内未被消费，清除它
  if (streamId != null) {
    setTimeout(() => {
      if (state.streamBubbles[streamId]) {
        delete state.streamBubbles[streamId];
      }
    }, STREAM_BUBBLE_TTL_MS);
  }

  playNext();
}

export function playNext() {
  if (state.audioPlaying || state.audioQueue.length === 0) return;
  state.audioPlaying = true;
  const item = state.audioQueue.shift();
  const a = new Audio(item.url);
  a.volume = state.volume / 100;

  a.onended = () => {
    URL.revokeObjectURL(item.url);
    if (item.streamId != null) delete state.streamBubbles[item.streamId];
    state.audioPlaying = false;
    if (!state.audioQueue.length) revealNextQueuedBubble();
    if (!state.audioQueue.length) clearPlayingHighlight();
    playNext();
  };

  a.play().then(() => {}).catch((e) => {
    console.warn('TTS 播放失败:', e);
    URL.revokeObjectURL(item.url);
    if (item.streamId != null) delete state.streamBubbles[item.streamId];
    state.audioPlaying = false;
    playNext();
  });

  revealNextQueuedBubble();
  if (item.streamId != null) {
    const bubble = state.streamBubbles[item.streamId];
    if (bubble) highlightPlaying(bubble);
  }
}
