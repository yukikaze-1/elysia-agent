# Frontend 模块化重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `static/index.html` (741行单文件) 拆分为 10 个 JS 模块 + 1 个 CSS 文件，同时修复 12 个 bug 和 4 个 UX 增强。

**Architecture:** ES modules 通过 `<script type="module">` 加载。每个模块单一职责，通过共享的 `AppState` 单例 (`state.js`) 通信。模块间不允许直接引用其他模块的内部变量，只能通过 `state` 或导出函数交互。

**Tech Stack:** Vanilla HTML/CSS/JS (ES modules), WebSocket, Web Audio API, MediaDevices API

---

## 文件结构

```
static/
├── index.html          # 改写: 纯 HTML 结构 + <script type="module">
├── debug.html           # 不变
├── css/
│   └── chat.css         # 新建: 全部样式从 index.html 提取
└── js/
    ├── config.js         # 新建: 常量
    ├── state.js          # 新建: AppState 单例
    ├── ws.js             # 新建: WebSocket 管理
    ├── chat.js           # 新建: 聊天显示
    ├── audio.js          # 新建: TTS 音频
    ├── mic.js            # 新建: 麦克风
    ├── settings.js       # 新建: 设置面板
    ├── persistence.js    # 新建: localStorage
    └── main.js           # 新建: 入口+装配
```

**依赖关系:**
```
config.js           (无依赖)
state.js            → config.js
ws.js               → state.js
chat.js             → state.js
audio.js            → state.js, chat.js
mic.js              → state.js
settings.js         → state.js, persistence.js
persistence.js      → state.js, chat.js
main.js             → 所有模块
```

---

### Task 1: 创建 `static/css/chat.css`

**Files:**
- Create: `static/css/chat.css`
- Modify: `static/index.html` (仅删除 `<style>` 块内容，添加 `<link>`)

- [ ] **Step 1: 创建 CSS 文件**

将 `static/index.html` 中 `<style>` 标签内的全部 CSS（第 7-200 行）提取到 `static/css/chat.css`。

注意：`index.html` 中第 7 行到第 200 行是 `<style>` 块。提取时去除 `<style>` 和 `</style>` 标签本身，保留所有 CSS 规则。

创建文件 `static/css/chat.css`:

```css
:root {
  --bg: #1a1a2e; --surface: #16213e; --border: #0f3460;
  --text: #e0e0e0; --muted: #888; --accent: #e94560; --accent2: #533483;
  --user-bg: #1a3a5c; --assistant-bg: #1e2a3a;
  --happy: #f0c040; --sad: #6090d0; --excited: #e060c0; --neutral: #808080;
  --idle: #4caf50; --thinking: #ff9800; --speaking: #2196f3; --executing: #e040fb;
  --radius: 16px; --radius-sm: 8px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text);
  height: 100vh; display: flex; overflow: hidden;
}

/* ---- Left: Character Panel ---- */
#char-panel {
  width: 35vw; max-width: 500px; min-width: 220px; flex-shrink: 0;
  background: var(--surface); display: flex;
  flex-direction: column; align-items: center; padding: 20px 16px;
  border-right: 1px solid var(--border);
}
#avatar-wrap {
  position: relative; flex: 1; width: 100%; min-height: 0; margin-bottom: 12px;
  overflow: hidden; border-radius: var(--radius);
  background: var(--surface);
  display: flex; align-items: center; justify-content: center;
}
#avatar-wrap::before {
  content: '🌸'; font-size: 60px; position: absolute;
}
#live2d-frame { width: 100%; height: 100%; border: none; position: relative; z-index: 1; }
#char-info { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
#state-dot {
  display: inline-block; width: 10px; height: 10px;
  border-radius: 50%; background: var(--idle);
  transition: background .3s; vertical-align: middle;
}
#state-dot.thinking { background: var(--thinking); animation: pulse-dot 1s infinite; }
#state-dot.speaking { background: var(--speaking); }
#state-dot.executing { background: var(--executing); animation: pulse-dot .5s infinite; }
@keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:.3} }
#char-name { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
#char-state { font-size: 12px; color: var(--muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
#mood-tag { font-size: 13px; color: var(--muted); text-align: center; min-height: 20px; transition: color .3s; }

/* ---- Center: Chat ---- */
#main { flex:1; display:flex; flex-direction:column; min-width: 0; }
#chat-wrap { flex:1; overflow-y:auto; padding: 16px 20px; scroll-behavior: smooth; }
#chat { display: flex; flex-direction: column; gap: 4px; }
.time-divider {
  text-align: center; font-size: 11px; color: var(--muted); padding: 8px 0;
  border-bottom: 1px solid var(--border); margin-bottom: 8px;
}
.msg-row { display: flex; gap: 10px; max-width: 85%; animation: fadeIn .2s ease; }
.msg-row.user { align-self: flex-end; flex-direction: row-reverse; }
.msg-row.assistant { align-self: flex-start; }
.msg-avatar { font-size: 28px; line-height: 1; flex-shrink: 0; margin-top: 4px; }
.msg-bubble {
  padding: 10px 14px; border-radius: var(--radius); line-height: 1.55;
  white-space: pre-wrap; word-break: break-word; font-size: 14px; position: relative;
}
.msg-row.assistant .msg-bubble { background: var(--assistant-bg); border-bottom-left-radius: 4px; }
.msg-row.user .msg-bubble { background: var(--user-bg); border-bottom-right-radius: 4px; }
.msg-bubble.playing {
  box-shadow: 0 0 14px rgba(124,179,255,0.35), inset 0 0 8px rgba(124,179,255,0.08);
  border-color: rgba(124,179,255,0.45);
  transition: box-shadow .3s ease, border-color .3s ease;
}
@keyframes bubble-pulse {
  0%,100% { box-shadow: 0 0 10px rgba(124,179,255,0.25), inset 0 0 4px rgba(124,179,255,0.04); }
  50%      { box-shadow: 0 0 20px rgba(124,179,255,0.50), inset 0 0 12px rgba(124,179,255,0.12); }
}
.msg-bubble.playing.pulse { animation: bubble-pulse 1.8s ease-in-out infinite; }
#new-msg-indicator {
  display: none; position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
  background: var(--accent); color: white; border: none; border-radius: 20px;
  padding: 8px 18px; font-size: 13px; cursor: pointer; z-index: 20; white-space: nowrap;
  box-shadow: 0 2px 12px rgba(233,69,96,0.4); transition: transform .15s ease;
}
#new-msg-indicator:hover { transform: translateX(-50%) scale(1.04); }
#new-msg-indicator.visible { display: block; }
.msg-mood { font-size: 12px; color: var(--muted); margin-left: 42px; margin-top: -2px; margin-bottom: 8px; }
.msg-row.user .msg-mood { display: none; }
.msg-status { font-size: 12px; color: var(--accent); margin-left: 42px; font-style: italic; }
@keyframes fadeIn { from{opacity:0} to{opacity:1} }

/* typing indicator */
.typing-dots { display: flex; gap: 4px; padding: 14px 14px; }
.typing-dots span { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); animation: bounce 1.4s infinite; }
.typing-dots span:nth-child(2) { animation-delay: .2s; }
.typing-dots span:nth-child(3) { animation-delay: .4s; }
@keyframes bounce { 0%,60%,100%{transform:translateY(0);opacity:.4} 30%{transform:translateY(-6px);opacity:1} }

/* ---- Right: Settings ---- */
#settings-btn {
  position: fixed; top: 12px; right: 16px; width: 32px; height: 32px; border-radius: 50%;
  background: transparent; border: 1px solid var(--border); color: var(--muted);
  font-size: 16px; cursor: pointer; display: flex; align-items: center; justify-content: center;
  z-index: 10; transition: color .2s;
}
#settings-btn:hover { color: var(--text); }
#settings-panel {
  position: fixed; top: 52px; right: 16px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; width: 220px; display: none; z-index: 10; box-shadow: 0 4px 20px rgba(0,0,0,.4);
}
#settings-panel.open { display: block; }
#settings-panel label { font-size: 12px; color: var(--muted); display: block; margin-bottom: 4px; }
#settings-panel .row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
#settings-panel input[type=range] { width: 100px; accent-color: var(--accent); }
#settings-panel button {
  width: 100%; padding: 8px; border-radius: var(--radius-sm); border: 1px solid var(--border);
  background: transparent; color: var(--text); cursor: pointer; font-size: 13px; margin-top: 4px;
}
#settings-panel button:hover { background: var(--border); }
#shutdown-btn:hover { background: #5c1a1a !important; border-color: #a33 !important; color: #f88 !important; }
.toggle { width: 40px; height: 22px; border-radius: 11px; background: var(--border); position: relative; cursor: pointer; transition: background .3s; }
.toggle.on { background: var(--accent); }
.toggle::after { content: ''; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 50%; background: white; transition: left .3s; }
.toggle.on::after { left: 20px; }
#conn-status { font-size: 11px; text-align: center; margin-top: 8px; }
#conn-status.connected { color: var(--idle); }
#conn-status.disconnected { color: var(--accent); }

/* ---- Bottom: Input ---- */
#input-bar { padding: 10px 16px; background: var(--surface); border-top: 1px solid var(--border); display: flex; gap: 8px; align-items: flex-end; }
#input-bar textarea {
  flex:1; padding: 10px 14px; border-radius: 20px; border: 1px solid var(--border);
  background: var(--bg); color: var(--text); font-size: 14px; outline: none; resize: none;
  height: 42px; max-height: 120px; line-height: 1.4; font-family: inherit;
}
#input-bar textarea:focus { border-color: var(--accent); }
#input-bar button {
  width: 42px; height: 42px; border-radius: 50%; border: none; cursor: pointer; font-size: 18px;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: background .2s;
}
#send-btn { background: var(--accent); color: white; position: relative; transition: all 0.2s; }
#send-btn:hover { filter: brightness(1.1); }
#send-btn:disabled { background: var(--border); color: var(--muted); cursor: not-allowed; filter: none; }
#send-btn.sent { animation: send-flash 0.4s ease; }
#send-btn.sent::after { content: '✓'; position: absolute; top: -6px; right: -6px; background: #4ade80; color: #000; border-radius: 50%; width: 16px; height: 16px; font-size: 0.6rem; display: flex; align-items: center; justify-content: center; animation: check-pop 0.3s ease 0.1s both; }
@keyframes send-flash { 0% { transform: scale(1); } 30% { transform: scale(0.85); } 60% { transform: scale(1.08); } 100% { transform: scale(1); } }
@keyframes check-pop { 0% { transform: scale(0); } 100% { transform: scale(1); } }
.copy-btn { display: none; position: absolute; top: 6px; right: 6px; background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.15); border-radius: 6px; color: #aaa; padding: 2px 8px; font-size: 0.75rem; cursor: pointer; backdrop-filter: blur(4px); transition: all 0.15s; }
.copy-btn:hover { background: rgba(255,255,255,0.25); color: #fff; }
.copy-btn.copied { color: #4ade80; border-color: #4ade80; }
.msg-bubble { position: relative; }
.msg-bubble:hover .copy-btn { display: block; }
#mic-btn { background: var(--border); color: var(--text); }
#mic-btn.active { background: var(--accent); animation: mic-pulse 1.5s infinite; }
@keyframes mic-pulse { 0%,100%{box-shadow:0 0 0 0 rgba(233,69,96,.4)} 50%{box-shadow:0 0 0 8px rgba(233,69,96,0)} }

/* Tool call cards */
.tool-card {
  margin: 4px 0; border-radius: var(--radius-sm);
  background: var(--surface); border-left: 3px solid var(--thinking);
  font-size: 13px; overflow: hidden; animation: fadeIn .2s ease;
}
.tool-card.executing { border-left-color: var(--thinking); }
.tool-card.done { border-left-color: var(--idle); }
.tool-card.fail { border-left-color: var(--accent); }
.tool-card-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; cursor: pointer; user-select: none;
}
.tool-card-header:hover { background: rgba(255,255,255,.03); }
.tool-card-header .tool-icon { font-size: 15px; flex-shrink: 0; }
.tool-card-header .tool-name { font-weight: 600; }
.tool-card-header .tool-status {
  margin-left: auto; font-size: 11px; color: var(--muted);
  display: flex; align-items: center; gap: 4px;
}
.tool-spinner {
  display: inline-block; width: 12px; height: 12px;
  border: 2px solid var(--border); border-top-color: var(--thinking);
  border-radius: 50%; animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.tool-card-body { padding: 0 12px 8px; display: none; }
.tool-card.expanded .tool-card-body { display: block; }
.tool-card-args { font-size: 12px; color: var(--muted); margin-bottom: 6px; word-break: break-all; }
.tool-card-result { font-size: 12px; padding: 6px 8px; border-radius: 4px; background: rgba(0,0,0,.2); max-height: 120px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
.tool-card-result.success { border-left: 2px solid var(--idle); }
.tool-card-result.fail { border-left: 2px solid var(--accent); }
.tool-card-chevron { font-size: 12px; color: var(--muted); transition: transform .2s; }
.tool-card.expanded .tool-card-chevron { transform: rotate(180deg); }

/* UX enhancement: prefers-reduced-motion */
@media (prefers-reduced-motion: reduce) {
  .msg-row { animation: none; }
  .msg-bubble.playing.pulse { animation: none; }
  #state-dot.thinking { animation: none; }
  #state-dot.executing { animation: none; }
  #send-btn.sent { animation: none; }
  #send-btn.sent::after { animation: none; }
  #mic-btn.active { animation: none; }
  .tool-card { animation: none; }
  .tool-spinner { animation: none; }
  .typing-dots span { animation: none; }
}

/* Responsive: collapse side panels on narrow windows */
@media (max-width: 600px) {
  #char-panel { width: 60px; padding: 16px 8px; min-width: 0; }
  #avatar-wrap { flex: 0 0 72px; }
  #char-name, #char-state, #mood-tag, #char-info { display: none; }
}
```

- [ ] **Step 2: 修改 `static/index.html`，删除 `<style>` 块，添加 CSS 引用**

在 `index.html` 的 `<head>` 中，用 `<link>` 替换 `<style>...</style>` 全部内容：

```html
<link rel="stylesheet" href="/static/css/chat.css">
```

注意：`<style>` 在第 7 行开始，第 200 行结束（`</style>`）。将第 7-200 行替换为上面的 `<link>` 行。

- [ ] **Step 3: 浏览器验证样式加载**

启动服务，打开浏览器开发者工具 Network 标签，确认 `chat.css` 正常加载（200），页面外观与重构前一致。

- [ ] **Step 4: Commit**

```bash
git add static/css/chat.css static/index.html
git commit -m "refactor: extract CSS to static/css/chat.css"
```

---

### Task 2: 创建 `static/js/config.js`

**Files:**
- Create: `static/js/config.js`

- [ ] **Step 1: 创建常量模块**

创建 `static/js/config.js`:

```js
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
```

- [ ] **Step 2: Commit**

```bash
git add static/js/config.js
git commit -m "feat: add config.js with named constants"
```

---

### Task 3: 创建 `static/js/state.js`

**Files:**
- Create: `static/js/state.js`

- [ ] **Step 1: 创建 AppState 类**

创建 `static/js/state.js`:

```js
import { STREAM_BUBBLE_TTL_MS, SETTINGS_STORAGE_KEYS } from './config.js';

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
    // ws 由 ws.js 的 connect() 重新赋值
  }
}

export const state = new AppState();
```

- [ ] **Step 2: Commit**

```bash
git add static/js/state.js
git commit -m "feat: add state.js with encapsulated AppState singleton"
```

---

### Task 4: 创建 `static/js/ws.js`

**Files:**
- Create: `static/js/ws.js`

- [ ] **Step 1: 创建 WebSocket 模块**

创建 `static/js/ws.js`:

```js
import { state } from './state.js';
import {
  WS_RECONNECT_BASE_MS,
  WS_RECONNECT_MAX_MS,
  WS_RECONNECT_JITTER,
} from './config.js';

let retryCount = 0;
let reconnectTimer = null;
/** @type {(msg: object) => void} */
let onMessage = null;

/** 计算退避延迟: base * 2^attempt, capped at max, ± jitter */
function backoff(attempt) {
  const base = Math.min(WS_RECONNECT_BASE_MS * Math.pow(2, attempt), WS_RECONNECT_MAX_MS);
  const jitter = base * WS_RECONNECT_JITTER * (Math.random() * 2 - 1);
  return Math.round(base + jitter);
}

/** 建立 WebSocket 连接，自动选择 ws:// 或 wss:// */
export function connect(handler) {
  onMessage = handler;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${protocol}//${location.host}/ws`;

  // 清理上次连接的 timer
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  state.ws = new WebSocket(url);

  state.ws.onopen = () => {
    retryCount = 0;
    setConnStatus('connected', '已连接');
    const sendBtn = document.getElementById('send-btn');
    const textInput = document.getElementById('text-input');
    if (sendBtn) sendBtn.disabled = false;
    if (textInput) textInput.placeholder = '输入消息...';
  };

  state.ws.onclose = () => {
    setConnStatus('disconnected', '断开 — 重连中...');
    const sendBtn = document.getElementById('send-btn');
    const textInput = document.getElementById('text-input');
    if (sendBtn) sendBtn.disabled = true;
    if (textInput) textInput.placeholder = '连接断开，重连中...';
    scheduleReconnect();
  };

  state.ws.onmessage = (e) => {
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch (err) {
      console.warn('WS: 无法解析消息', err);
      return;
    }
    if (onMessage) onMessage(msg);
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = backoff(retryCount);
  retryCount++;
  console.log(`WS: ${delay}ms 后重连 (第 ${retryCount} 次)`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    state.resetConnection();
    // 注意: onMessage handler 保持不变，由 main.js 在首次 connect() 时设置
    connect(onMessage);
  }, delay);
}

/** 发送 JSON 消息 */
export function send(data) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(typeof data === 'string' ? data : JSON.stringify(data));
  }
}

/** 更新连接状态指示器 */
function setConnStatus(cls, text) {
  const el = document.getElementById('conn-status');
  if (el) {
    el.className = cls;
    el.textContent = text;
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add static/js/ws.js
git commit -m "feat: add ws.js with backoff reconnect and JSON parse guard"
```

---

### Task 5: 创建 `static/js/chat.js`

**Files:**
- Create: `static/js/chat.js`

- [ ] **Step 1: 创建聊天显示模块**

创建 `static/js/chat.js`:

```js
import { state } from './state.js';
import {
  TIME_DIVIDER_GAP_MS,
  SCROLL_FOLLOW_THRESHOLD,
  COPY_FEEDBACK_MS,
  INPUT_MAX_HEIGHT,
  INPUT_INITIAL_HEIGHT,
} from './config.js';
import { debounceSave } from './persistence.js';

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
    // saveChat 由 debounceSave 或 addUserMsg 触发
  }
  state.lastRole = null;
}

/* ---- 用户消息 ---- */
export function addUserMsg(text) {
  finalizeDelta();
  state.nextBubbleQueue = [];
  state.streamBubbles = {};
  // 取消当前 TTS 高亮
  if (state.playingBubble) {
    state.playingBubble.classList.remove('playing', 'pulse');
    state.playingBubble = null;
  }
  state.lastRole = 'user';
  const row = document.createElement('div');
  row.className = 'msg-row user';
  row.innerHTML = `<div class="msg-bubble">${esc(text)}</div><div class="msg-avatar">😎</div>`;
  document.getElementById('chat').appendChild(row);
  scrollDown();

  // 增量保存：添加到内存数组
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
let _toolCounter = 0;

export function addToolCard(name, args) {
  finalizeDelta();
  const id = `${name}-${++_toolCounter}`;
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

// 依赖 persistence.js (底部导入以避免循环)
import { saveChat } from './persistence.js';
```

- [ ] **Step 2: Commit**

```bash
git add static/js/chat.js
git commit -m "feat: add chat.js with unified createAssistantRow factory"
```

---

### Task 6: 创建 `static/js/audio.js`

**Files:**
- Create: `static/js/audio.js`

- [ ] **Step 1: 创建 TTS 音频模块**

创建 `static/js/audio.js`:

```js
import { state } from './state.js';
import { STREAM_BUBBLE_TTL_MS } from './config.js';
import {
  highlightPlaying,
  clearPlayingHighlight,
  revealNextQueuedBubble,
} from './chat.js';

/** Base64 解码为 Uint8Array */
function decodeBase64(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
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
```

- [ ] **Step 2: Commit**

```bash
git add static/js/audio.js
git commit -m "feat: add audio.js with TTS buffer/playback and stream bubble TTL cleanup"
```

---

### Task 7: 创建 `static/js/mic.js`

**Files:**
- Create: `static/js/mic.js`

- [ ] **Step 1: 创建麦克风模块**

创建 `static/js/mic.js`:

```js
import { state } from './state.js';
import { MIC_BUFFER_SIZE, MIC_SAMPLE_RATE } from './config.js';
import { send } from './ws.js';
import { addStatusMsg } from './chat.js';

export async function toggleMic() {
  if (state.micStream) { stopMic(); return; }
  try {
    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: MIC_SAMPLE_RATE,
        channelCount: 1,
        echoCancellation: true,
      },
    });
    state.audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: MIC_SAMPLE_RATE });
    const source = state.audioCtx.createMediaStreamSource(state.micStream);
    state.scriptNode = state.audioCtx.createScriptProcessor(MIC_BUFFER_SIZE, 1, 1);
    state.scriptNode.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      const int16 = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        int16[i] = Math.max(-32768, Math.min(32767, input[i] * 32768));
      }
      send(int16.buffer);
    };
    source.connect(state.scriptNode);
    state.scriptNode.connect(state.audioCtx.destination);
    document.getElementById('mic-btn').classList.add('active');
    addStatusMsg('麦克风已开启');
  } catch (err) {
    let hint = '无法访问麦克风。';
    if (err.name === 'NotAllowedError') hint = '请允许浏览器使用麦克风权限。';
    else if (err.name === 'NotFoundError') hint = '未检测到麦克风设备。';
    addStatusMsg(hint);
    stopMic();
  }
}

export function stopMic() {
  if (state.scriptNode) { state.scriptNode.disconnect(); state.scriptNode = null; }
  if (state.audioCtx) { state.audioCtx.close(); state.audioCtx = null; }
  if (state.micStream) { state.micStream.getTracks().forEach((t) => t.stop()); state.micStream = null; }
  document.getElementById('mic-btn').classList.remove('active');
}
```

- [ ] **Step 2: Commit**

```bash
git add static/js/mic.js
git commit -m "feat: add mic.js for microphone capture"
```

---

### Task 8: 创建 `static/js/persistence.js`

**Files:**
- Create: `static/js/persistence.js`

- [ ] **Step 1: 创建持久化模块**

创建 `static/js/persistence.js`:

```js
import { state } from './state.js';
import { CHAT_SAVE_DEBOUNCE_MS, CHAT_MAX_STORED, SETTINGS_STORAGE_KEYS } from './config.js';
import { esc, createAssistantRow, addUserMsg } from './chat.js';

/** 防抖保存 */
export function debounceSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveChat, CHAT_SAVE_DEBOUNCE_MS);
}

/** 增量保存：从 state.messages 序列化 */
export function saveChat() {
  const msgs = state.messages.slice(-CHAT_MAX_STORED);
  localStorage.setItem(SETTINGS_STORAGE_KEYS.CHAT, JSON.stringify(msgs));
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
    if (!m.content) continue;
    if (m.role === 'user') {
      const row = document.createElement('div');
      row.className = 'msg-row user';
      row.innerHTML = `<div class="msg-bubble">${esc(m.content)}</div><div class="msg-avatar">😎</div>`;
      chat.appendChild(row);
      state.messages.push({ role: 'user', content: m.content });
    } else {
      state.lastRole = 'assistant';
      const { row, textSpan } = createAssistantRow();
      textSpan.textContent = m.content;
      chat.appendChild(row);
      state.messages.push({ role: 'assistant', content: m.content });
    }
  }
  // 恢复后滚动到底部
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
```

- [ ] **Step 2: Commit**

```bash
git add static/js/persistence.js
git commit -m "feat: add persistence.js with incremental save and side-effect-free restore"
```

---

### Task 9: 创建 `static/js/settings.js`

**Files:**
- Create: `static/js/settings.js`

- [ ] **Step 1: 创建设置面板模块**

创建 `static/js/settings.js`:

```js
import { state } from './state.js';
import { SETTINGS_STORAGE_KEYS } from './config.js';
import { clearChat } from './persistence.js';
import { addStatusMsg } from './chat.js';
import { connect } from './ws.js';  // 重连用 (供回调)

/** 初始化设置面板事件 */
export function setupSettings() {
  // 恢复设置状态
  document.getElementById('vol-slider').value = state.volume;
  if (!state.ttsEnabled) document.getElementById('tts-toggle').classList.remove('on');

  // 设置按钮
  document.getElementById('settings-btn').onclick = () => {
    document.getElementById('settings-panel').classList.toggle('open');
  };

  // 面板外点击关闭
  document.addEventListener('click', (e) => {
    const panel = document.getElementById('settings-panel');
    const btn = document.getElementById('settings-btn');
    if (!panel.classList.contains('open')) return;
    if (!panel.contains(e.target) && e.target !== btn) {
      panel.classList.remove('open');
    }
  });

  // TTS 开关
  document.getElementById('tts-toggle').onclick = function () {
    this.classList.toggle('on');
    state.ttsEnabled = this.classList.contains('on');
    localStorage.setItem(SETTINGS_STORAGE_KEYS.TTS, state.ttsEnabled ? 'on' : 'off');
    if (!state.ttsEnabled) {
      state.audioChunks = {};
      state.audioQueue.length = 0;
    }
  };

  // 音量
  document.getElementById('vol-slider').oninput = function () {
    state.volume = parseInt(this.value, 10);
    localStorage.setItem(SETTINGS_STORAGE_KEYS.VOL, state.volume);
  };

  // 关闭服务
  document.getElementById('shutdown-btn').onclick = async () => {
    if (!confirm('确定要关闭 Elysia 服务吗？')) return;
    try {
      const res = await fetch('/shutdown', { method: 'POST' });
      if (res.ok) {
        addStatusMsg('服务已关闭');
        setTimeout(() => {
          document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-size:18px;">Elysia 已关闭 🫧</div>';
        }, 500);
      } else {
        addStatusMsg('关闭失败');
      }
    } catch (e) {
      addStatusMsg('服务已关闭');
      setTimeout(() => {
        document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-size:18px;">Elysia 已关闭 🫧</div>';
      }, 500);
    }
  };

  // 清空对话 (加确认)
  document.getElementById('clear-btn').onclick = () => {
    if (!confirm('确定要清空所有对话记录吗？此操作不可撤销。')) return;
    clearChat();
  };
}
```

- [ ] **Step 2: Commit**

```bash
git add static/js/settings.js
git commit -m "feat: add settings.js with outside-click close and clear confirmation"
```

---

### Task 10: 创建 `static/js/main.js` + 改写 `static/index.html`

**Files:**
- Create: `static/js/main.js`
- Modify: `static/index.html` (删除内联 JS，改为 `<script type="module">`)

- [ ] **Step 1: 创建主入口模块**

创建 `static/js/main.js`:

```js
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
  // 恢复历史
  restoreChat();

  // 连接 WebSocket
  connect(handleMessage);

  // 设置输入框
  setupInput();

  // 设置面板
  setupSettings();

  // 发送按钮
  document.getElementById('send-btn').onclick = sendText;

  // 麦克风按钮
  document.getElementById('mic-btn').onclick = toggleMic;

  // 滚动检测
  document.getElementById('chat-wrap').addEventListener('scroll', function () {
    const w = this;
    const wasUp = state.userScrolledUp;
    state.userScrolledUp = w.scrollTop + w.clientHeight < w.scrollHeight - 60;
    if (!state.userScrolledUp && wasUp) hideNewMsgIndicator();
  });

  // 键盘快捷键
  document.addEventListener('keydown', (e) => {
    // Escape → 打断
    if (e.key === 'Escape' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      send({ type: 'interrupt' });
      stopMic();
    }
  });
}

// 启动
init();
```

- [ ] **Step 2: 改写 `static/index.html`**

将 `index.html` 中的 `<script>...</script>` 块（第 238-739 行）替换为：

```html
<script type="module" src="/static/js/main.js"></script>
```

同时修复 Live2D iframe sandbox（移除 `allow-same-origin`）：

```html
<iframe id="live2d-frame" src="/static/live2d-demo/index.html" sandbox="allow-scripts"></iframe>
```

- [ ] **Step 3: 启动服务进行手工验证**

```bash
conda activate Elysia && python app.py --server
```

浏览器打开 `http://localhost:<port>`，验证：
1. CSS 样式正常加载
2. 页面加载时历史消息恢复
3. 发送消息 → delta 追加 → 气泡正常
4. 复制按钮 hover 显示、点击复制
5. 发送按钮动画
6. TTS 播放时气泡高亮
7. 麦克风开关
8. 设置面板：TTS 开关、音量、清空（确认对话框）
9. Escape 键打断
10. WebSocket 断开后指数退避重连、重连后状态清理
11. 移动端布局（窄屏）
12. `prefers-reduced-motion` 动画禁用

- [ ] **Step 4: 运行后端测试确保无回归**

```bash
python -m pytest Module/ core/ -v
```

- [ ] **Step 5: Commit**

```bash
git add static/js/main.js static/index.html
git commit -m "refactor: add main.js entry point, rewrite index.html as module host"
```

---

## Verification

```bash
# 全量后端测试（应全通过，无变化）
python -m pytest Module/ core/ -v

# 启动服务手工测试
python app.py --server
# 浏览器验证全部交互功能
```

---

## 已知的循环依赖说明

`chat.js` 底部 `import { saveChat } from './persistence.js'` 而 `persistence.js` 顶部 `import { ... } from './chat.js'`。这是因为 `debounceSave`（在 `chat.js` 的 `appendDelta` 中调用）需要 `saveChat`，而 `saveChat` 只需序列化 `state.messages`（不依赖 chat.js 的 DOM 函数）。`persistence.js` 需要 `createAssistantRow` 和 `addUserMsg` 用于恢复/清空。

解决方案：`chat.js` 不在顶层导入 `persistence.js`，而是在 `appendDelta` 和 `revealNextQueuedBubble` 中动态 `import()`。实际上，`debounceSave` 和 `saveChat` 的调用频率很低——更好的做法是将 `debounceSave` 移到 `persistence.js` 中，`chat.js` 只调用它。

**最终方案**：`debounceSave` 定义在 `persistence.js` 中，`chat.js` 导入使用。`persistence.js` 导入 `chat.js` 的 `createAssistantRow`、`addUserMsg`、`esc`（正常导入，无循环）。因为 `chat.js` → `persistence.js` (import `debounceSave`) 和 `persistence.js` → `chat.js` (import `createAssistantRow` 等) 在 ES module 中是被允许的（live binding，只要不触发 TDZ）。在模块初始化阶段两者都不会立即调用对方。
