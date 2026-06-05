import { SETTINGS_STORAGE_KEYS } from './config.js';

class AppState {
  constructor() {
    /** @type {WebSocket|null} */
    this.ws = null;
    /** @type {AudioContext|null} */
    this.audioCtx = null;
    /** @type {ScriptProcessorNode|null} */
    this.scriptNode = null;
    /** @type {MediaStream|null} */
    this.micStream = null;

    // 音频播放
    this.audioPlaying = false;
    this.audioQueue = [];

    // 设置 (从 localStorage 恢复)
    this.ttsEnabled = localStorage.getItem(SETTINGS_STORAGE_KEYS.TTS) !== 'off';
    this.volume = parseInt(localStorage.getItem(SETTINGS_STORAGE_KEYS.VOL) || '80', 10);

    // 聊天状态
    this.currentDelta = null;       // 当前正在追加文本的 .msg-text span
    this.typingEl = null;           // 打字指示器 DOM
    this.lastMsgTime = 0;
    this.lastRole = '';
    this.userScrolledUp = false;
    this.nextBubbleQueue = [];     // TTS 排队中的气泡
    this.toolCards = {};           // toolId → card DOM
    this.playingBubble = null;     // 当前高亮的气泡
    this.streamBubbles = {};       // stream_id → bubble DOM
    this.saveTimer = null;         // debounce timer ID

    // 音频缓冲 (替代原 window._ac)
    this.audioChunks = {};         // stream_id → [Uint8Array]

    // 内存消息数组 (增量保存用)
    this.messages = [];

    // 图片预览队列 [{url: "/uploads/images/abc.png", mime: "image/png"}]
    this.previewImages = [];

    // 唤醒词 (默认关闭)
    this.wakeEnabled = localStorage.getItem(SETTINGS_STORAGE_KEYS.WAKE) === 'on';

    // Live2D（默认开启）
    this.live2dEnabled = localStorage.getItem(SETTINGS_STORAGE_KEYS.LIVE2D) !== 'off';
    this.live2dStaticImage = '';  // 由 WS capabilities 消息更新

    // 服务能力状态 (由 WS capabilities 消息更新)
    this.sttAvailable = true;   // 乐观默认 true，收到 capabilities 后更新
    this.ttsAvailable = true;
    this.llmModel = '';         // 当前 LLM 模型名

    // Token 统计
    this.sessionTokens = { total: 0, byModel: {} };
    this.currentTurnTokens = [];        // 当前轮各 round 的 usage
    this.activeTurnTotal = 0;          // 当前轮总 token
  }

  /** WebSocket 重连时重置所有运行时状态 */
  resetConnection() {
    if (this.typingEl) {
      this.typingEl.remove();
      this.typingEl = null;
    }
    this.currentDelta = null;
    this.nextBubbleQueue = [];
    this.toolCards = {};
    this.playingBubble = null;
    this.streamBubbles = {};
    this.audioQueue = [];
    this.audioPlaying = false;
    this.audioChunks = {};
    this.previewImages = [];
    this.sessionTokens = { total: 0, byModel: {} };
    this.currentTurnTokens = [];
    this.activeTurnTotal = 0;
  }
}

export const state = new AppState();
