# Frontend 模块化重构设计 — 2026-06-01

> 范围：`static/index.html`（Agent 聊天界面）
> 状态：待实施

---

## 背景

当前前端是单个 741 行的 `static/index.html`，HTML / CSS / JS 全部内联在一个文件中。随着 TTS 高亮同步、复制按钮、发送动画等功能持续迭代，代码中出现了：
- 重复逻辑（助理消息行创建写了两次）
- 全局变量散落（15+ 个 `let` 声明在顶层）
- 魔法数字散布（3秒重连、5分钟分割线、4096缓冲区等）
- 多个 bug-level 问题（JSON 解析无防护、tool card 键名冲突、内存泄漏等）

本次重构的目标：拆分模块 + 修复 bug + 提升健壮性。

---

## 目标文件结构

```
static/
├── index.html          # 纯 HTML 结构
├── debug.html           # 不变
├── css/
│   └── chat.css         # 全部样式
└── js/
    ├── config.js         # 常量集中定义
    ├── state.js          # AppState 封装，替代散落的全局变量
    ├── ws.js             # WebSocket 连接管理、退避重连、消息收发
    ├── chat.js           # 聊天显示：气泡、delta 追加、typing、tool card
    ├── audio.js          # TTS 音频缓冲、播放、气泡高亮
    ├── mic.js            # 麦克风采集（保留 AudioWorklet 迁移作为后续任务）
    ├── settings.js       # 设置面板交互
    ├── persistence.js    # localStorage 读写
    └── main.js           # 入口：初始化、事件绑定、模块装配
```

---

## 各模块职责

### `config.js` — 常量

所有魔法数字集中在此：

```js
export const WS_RECONNECT_BASE_MS = 1000;
export const WS_RECONNECT_MAX_MS = 30000;
export const WS_RECONNECT_JITTER = 0.2;
export const TIME_DIVIDER_GAP_MS = 300000;  // 5 分钟
export const MIC_BUFFER_SIZE = 4096;
export const MIC_SAMPLE_RATE = 16000;
export const CHAT_SAVE_DEBOUNCE_MS = 500;
export const CHAT_MAX_STORED = 100;
export const INPUT_MAX_HEIGHT = 120;
export const INPUT_INITIAL_HEIGHT = 42;
export const SCROLL_FOLLOW_THRESHOLD = 60;
export const STREAM_BUBBLE_TTL_MS = 30000;
export const SEND_COOLDOWN_MS = 800;
export const COPY_FEEDBACK_MS = 2000;
export const SETTINGS_STORAGE_KEYS = {
  CHAT: 'elysia_chat',
  TTS: 'elysia_tts',
  VOL: 'elysia_vol',
};
```

### `state.js` — 状态封装

```js
import { STREAM_BUBBLE_TTL_MS } from './config.js';

class AppState {
  constructor() {
    this.ws = null;
    this.audioCtx = null;
    this.scriptNode = null;
    this.micStream = null;
    this.audioPlaying = false;
    this.audioQueue = [];
    this.ttsEnabled = true;
    this.volume = 80;
    this.currentDelta = null;
    this.typingEl = null;
    this.lastMsgTime = 0;
    this.lastRole = '';
    this.userScrolledUp = false;
    this.nextBubbleQueue = [];
    this.toolCards = {};       // id → DOM element
    this.playingBubble = null;
    this.streamBubbles = {};   // stream_id → bubble DOM
    this.saveTimer = null;
    this.audioChunks = {};     // stream_id → [Uint8Array]  (替代 window._ac)
  }

  // 每次重连时调用
  resetConnection() {
    this.currentDelta = null;
    this.typingEl = null;
    this.nextBubbleQueue = [];
    this.toolCards = {};
    this.playingBubble = null;
    this.streamBubbles = {};
    this.audioQueue = [];
    this.audioPlaying = false;
    this.audioChunks = {};
  }
}

export const state = new AppState();
```

### `ws.js` — WebSocket 管理

- `connect()` — 自动检测 `wss://` / `ws://`，创建连接
- `reconnect()` — 指数退避 + jitter，重连前调 `state.resetConnection()`
- `send(data)` — 统一发送接口
- `onMessage` 回调注册
- JSON.parse 必须 try-catch，错误记录到 console 并忽略

### `chat.js` — 聊天显示

暴露函数：

| 函数 | 说明 |
|------|------|
| `appendDelta(text, streamId)` | 流式文本追加（复用当前气泡或创建新气泡） |
| `finalizeDelta()` | 结束当前 delta |
| `addUserMsg(text)` | 用户消息气泡 |
| `addStatusMsg(text)` | 状态提示（灰色斜体） |
| `addToolCard(name, args)` | 工具执行卡片，ID 用 `${name}-${Date.now()}` 防冲突 |
| `updateToolCard(id, success)` | 更新工具卡片状态 |
| `showTyping()` / `hideTyping()` | 打字指示器 |
| `setMood(mood, intensity)` | 角色心情 |
| `setState(state)` | FSM 状态指示 |
| `scrollDown()` | 滚动到底部 |
| `createAssistantRow()` | **统一**创建助理消息行（消除重复代码） |

**关键修复：**
- `_toolCards` key 改为唯一 ID（`${name}-${counter}`），彻底解决同名工具冲突
- `createAssistantRow()` 作为唯一的消息行工厂，消除两处重复代码

### `audio.js` — TTS 音频

暴露函数：

| 函数 | 说明 |
|------|------|
| `bufferAudioChunk(streamId, seq, b64)` | 缓冲 base64 音频块 |
| `finalizeStream(streamId)` | 合并音频块，加入播放队列 |
| `playNext()` | 播放队列中的下一个 |
| `highlightPlaying(bubble)` | 高亮播放中的气泡 |
| `clearPlayingHighlight()` | 取消高亮 |

**关键修复：**
- `window._ac` 替换为 `state.audioChunks`，重连时 `resetConnection()` 清空
- `_streamBubbles` 加 30 秒 TTL 超时清理（防止永不到达的 audio 泄漏）
- `playNext()` 错误处理加 console.warn 日志

### `mic.js` — 麦克风

- `toggleMic()` / `stopMic()` 
- 本阶段保留 `createScriptProcessor`（AudioWorklet 迁移作为后续独立任务）

### `settings.js` — 设置面板

- 面板开关
- TTS 开关 + 音量滑块
- 清空对话（**加确认对话框**）
- 关闭服务（保持不变）
- **面板外点击关闭**
- 连接状态显示

### `persistence.js` — 持久化

- `saveChat()` — **增量保存**：不再每次全量扫描 DOM，改为在 `addUserMsg` 和 `finalizeDelta` 时直接更新内存中的消息数组，保存时直接序列化
- `restoreChat()` — **直接构建 DOM**：不再走 `addUserMsg()`（避免副作用），使用 `chat.createAssistantRow()` 或直接操作 DOM
- `debounceSave()` — 防抖写入 localStorage

### `main.js` — 入口

- 导入所有模块
- 绑定 DOM 事件（按钮点击、键盘快捷键）
- 调用 `ws.connect()` 启动
- 调用 `persistence.restoreChat()` 恢复历史
- 全局键盘快捷键：**Escape → 发送 WS interrupt 消息**

---

## Bug 修复汇总

| # | 严重度 | 问题 | 修复方式 |
|---|--------|------|---------|
| 1 | 高 | JSON.parse 无 try-catch | `ws.js` onMessage 加 try-catch |
| 2 | 高 | `_toolCards` 键名冲突 | 改用唯一 ID（`name-counter`） |
| 3 | 中 | `_streamBubbles` 内存泄漏 | 30s TTL 超时清理 |
| 4 | 中 | `window._ac` 跨重连残留 | 移入 `state.audioChunks`，重连时清空 |
| 5 | 中 | `restoreChat()` 走 `addUserMsg()` | 独立构建 DOM 路径 |
| 6 | 低 | `saveChat()` 全量 DOM 扫描 | 改为增量：内存数组 + 序列化 |
| 7 | 中 | Live2D iframe sandbox 失效 | 移除 `allow-same-origin`，只保留 `allow-scripts` |
| 8 | 中 | ws:// 明文连接 | 自动检测 `location.protocol` |
| 9 | 中 | 重连无退避 | 指数退避 1s→2s→4s→8s→max 30s + ±20% jitter |
| 10 | 低 | 设置面板无外部点击关闭 | document click 事件委托 |
| 11 | 低 | 清空对话无确认 | `confirm()` 对话框 |
| 12 | 低 | 重复代码 | `createAssistantRow()` 统一工厂 |

---

## UX 增强

| # | 说明 |
|---|------|
| 13 | 全局键盘快捷键：Escape → 发送 interrupt |
| 14 | CSS `prefers-reduced-motion` 媒体查询 |
| 15 | 消息气泡添加基本 ARIA 标签 (`role="article"`, `aria-label`) |
| 16 | 重连时显示"重连中..."状态提示 |

---

## 不纳入本次范围

以下问题记录在案但不在此次重构中处理：

- `createScriptProcessor` → AudioWorkletNode 迁移（需要独立测试 + worker 文件，风险较高）
- 虚拟滚动（对话量级暂不需要，等遇到性能瓶颈再上）
- 暗色/亮色主题切换
- 对话导出/导入
- 消息编辑/删除/重试

---

## 验证计划

1. **单元/集成测试**（后端无变化，现有 708 个测试应全部通过）
   ```bash
   python -m pytest Module/ core/ -v
   ```

2. **前端手工验证**（启动服务后浏览器测试）
   - 发送消息 → delta 追加正常，气泡显示正常
   - 复制按钮 hover 显示、点击复制
   - 发送按钮动画
   - TTS 播放时气泡高亮
   - 麦克风开关
   - 设置面板：TTS 开关、音量、清空（确认对话框）、关闭服务
   - 断开网络 → 指数退避重连 → 恢复网络 → 自动重连
   - 刷新页面 → 历史消息恢复
   - 移动端布局（窄屏）

---

## 参考

- 现有代码: `static/index.html`
- 现有 review: `docs/review-2026-06-01.md`
- 相关修复计划: `docs/superpowers/plans/2026-06-01-review-fixes-phase-3.md` (Task 3.5, 3.8)
