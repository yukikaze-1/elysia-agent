// WebSocket 重连
export const WS_RECONNECT_BASE_MS = 1000;
export const WS_RECONNECT_MAX_MS = 30000;
export const WS_RECONNECT_JITTER = 0.2;

// 聊天
export const TIME_DIVIDER_GAP_MS = 300000;  // 5 分钟
export const CHAT_SAVE_DEBOUNCE_MS = 500;
export const CHAT_MAX_STORED = 100;
export const INPUT_MAX_HEIGHT = 120;
export const INPUT_INITIAL_HEIGHT = 42;
export const SCROLL_FOLLOW_THRESHOLD = 60;

// 音频
export const MIC_BUFFER_SIZE = 4096;
export const MIC_SAMPLE_RATE = 16000;
export const STREAM_BUBBLE_TTL_MS = 30000;

// UI
export const SEND_COOLDOWN_MS = 800;
export const COPY_FEEDBACK_MS = 2000;

// localStorage keys
export const SETTINGS_STORAGE_KEYS = {
  CHAT: 'elysia_chat',
  TTS: 'elysia_tts',
  VOL: 'elysia_vol',
};
