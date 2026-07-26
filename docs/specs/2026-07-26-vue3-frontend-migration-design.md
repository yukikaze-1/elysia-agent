# Vue 3 前端重构设计

## 概述

将现有的 Vanilla JS 前端（`static/`，~2100 行 JS + 544 行 CSS = ~2800 行，11 个 ES module）重构为 Vue 3 + Vite + Pinia + TypeScript 架构。保留现有后端服务不变（aiohttp serve `dist/` 目录），开发时通过 Vite 代理到后端。

## 目标

- 消除全局单例状态（`AppState` 25+ 字段混在一起）
- 消除手动 DOM 操作（`createElement` + `appendChild` + `innerHTML`）
- 消除巨型 switch 消息路由（20+ case 全在 `handleMessage`）
- 消除没有构建工具的限制（不能写 TS、不能 npm 包、无热更新）
- 保持所有现有功能不变（流式渲染、TTS、语音、Live2D、图片上传、工具卡片、审批弹窗、debug 页面）

## 非目标

- 不改变后端核心逻辑（WebSocket 协议、API 端点）
- 不修改 `debug.html` 的功能逻辑（仅迁移为 Vue 组件）
- 不引入服务端渲染（SSR）或全栈框架（Next/Nuxt）
- 不改动后端构建流程
- **不修改后端任何代码**（见 Debug 页面章节：Vite 多入口平铺方案实现零后端改动）

## 目录结构

```
elysia-agent/
├── frontend/                       ← 新建的 Vue 3 项目根目录
│   ├── index.html                  ← Vite 入口
│   ├── vite.config.ts              ← 代理配置
│   ├── tsconfig.json
│   ├── package.json
│   ├── src/
│   │   ├── main.ts                 ← createApp + mount
│   │   ├── App.vue                 ← 三栏布局根组件
│   │   ├── types/
│   │   │   ├── protocol.ts         ← WebSocket 消息类型（20+ 消息的 union type）
│   │   │   └── stores.ts           ← Store 接口类型
│   │   ├── stores/
│   │   │   ├── connectionStore.ts  ← WebSocket 连接生命周期
│   │   │   ├── chatStore.ts        ← 消息列表 + delta 流式追加
│   │   │   ├── audioStore.ts       ← TTS 缓冲/播放 + 麦克风
│   │   │   ├── settingsStore.ts    ← 设置项 + localStorage 同步
│   │   │   └── toolsStore.ts       ← 工具插件列表
│   │   ├── composables/
│   │   │   ├── useWebSocket.ts     ← WS 连接 + 消息路由
│   │   │   ├── useAudio.ts         ← TTS 播放控制
│   │   │   └── useMic.ts           ← 麦克风采集封装
│   │   ├── components/
│   │   │   ├── layout/
│   │   │   │   └── AppLayout.vue
│   │   │   ├── character/
│   │   │   │   ├── CharacterPanel.vue  ← 左侧角色面板
│   │   │   │   ├── AvatarDisplay.vue   ← Live2D / 静态图切换
│   │   │   │   └── CharStatus.vue      ← 状态点 + 角色名 + 心情 + 模型名
│   │   │   ├── chat/
│   │   │   │   ├── MessageList.vue     ← 消息列表（滚动 + 无限加载 + 新消息指示器）
│   │   │   │   ├── ChatMessage.vue     ← 单条消息（用户/助理 + 气泡）
│   │   │   │   ├── ChatBubble.vue      ← 气泡文本内容
│   │   │   │   ├── ToolCard.vue        ← 工具调用卡片（executing/done/fail）
│   │   │   │   ├── InnerVoice.vue      ← 内心独白气泡
│   │   │   │   ├── TokenLabel.vue      ← Token 统计标签
│   │   │   │   ├── TimeDivider.vue     ← 时间分隔符
│   │   │   │   └── TypingIndicator.vue ← 打字指示器
│   │   │   ├── input/
│   │   │   │   ├── InputBar.vue        ← 附件 · 麦克风 · 输入框 · 发送
│   │   │   │   └── PreviewBar.vue      ← 图片预览条
│   │   │   ├── panels/
│   │   │   │   ├── SettingsPanel.vue   ← 设置面板（Toggle + 模型选择 + 自定义端点）
│   │   │   │   └── ToolsPanel.vue      ← 工具管理面板
│   │   │   ├── ui/
│   │   │   │   ├── ToggleSwitch.vue    ← 开关组件
│   │   │   │   ├── ApprovalDialog.vue  ← HITL 审批弹窗
│   │   │   │   └── ModelSelector.vue   ← 模型选择下拉框
│   │   │   └── shared/
│   │   │       ├── Lightbox.vue        ← 图片灯箱
│   │   │       └── CodeBlock.vue       ← 代码块（高亮 + 复制）
│   │   └── styles/
│   │       └── variables.css           ← 全局 CSS 变量
│   ├── debug.html                      ← Debug 页面入口（平铺在根目录，零后端改动）
│   └── src/debug/
│       ├── main.ts                     ← debug app 入口
│       ├── App.vue                     ← debug 根组件
│       └── components/                 ← debug 面板组件（16 个面板）
│           ├── SystemPanel.vue
│           ├── MemoryPanel.vue
│           ├── LatencyPanel.vue
│           ├── TokenPanel.vue
│           ├── ...
│   └── dist/                           ← Vite 构建输出（.gitignore，复制到 static/）
│
├── static/                              ← 保留原有文件，迁移完成后替换
│   ├── (旧文件，迁移过程中删除)
│   └── new/                             ← 构建产物软链或复制到此处
```

## Pinia Store 设计

### connectionStore

```ts
// stores/connectionStore.ts

interface ConnectionState {
  ws: WebSocket | null
  connected: boolean
  retryCount: number
  connStatusText: string
}

actions: {
  connect(url: string): void
  send(data: string | Record<string, unknown>): boolean
  sendBinary(data: ArrayBuffer | ArrayBufferView): boolean
  disconnect(): void
  scheduleReconnect(): void
  resetConnection(): void    // 只清运行时状态，不清 messages
}
```

- WebSocket `onopen`/`onclose`/`onmessage` 在此 store 中注册
- 不处理业务消息，`onmessage` 直接转发到 `useWebSocket().handleMessage`
- 重连策略与现有一致：指数退避（1s 起，最大 30s，±20% jitter）
- `resetConnection()` 清空 TTS 缓冲、工具卡片、delta 状态，但**保留 `messages` 数组**（重连后通过 `restoreFromServer` 恢复）

### chatStore

```ts
// stores/chatStore.ts
interface ChatMessage {
  id: string                    // 唯一 ID（用于 :key 和跟踪）
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string | ContentPart[]
  contentType?: ContentType     // 'text' | 'markdown' | 'code'
  language?: string
  timestamp: number
  toolCalls?: ToolCallData[]    // 工具调用卡片
  toolCallResults?: Record<string, ToolCallResult>
  innerVoice?: string
  tokenUsage?: TokenUsage
  pending?: boolean             // delta 追加中
  queued?: boolean              // TTS 播放中暂缓显示
}

interface ChatState {
  messages: ChatMessage[]
  lastRole: string
  lastMsgTime: number
  userScrolledUp: boolean
  hasMoreMessages: boolean
  earliestMessageId: string | null
  typing: boolean
  mood: string
  moodIntensity: number
  fsmState: string

  // TTS 气泡排队
  nextBubbleQueue: string[]          // 排队气泡的 message id，等待 TTS 完成后显示
  playingMessageId: string | null    // 当前高亮播放的气泡 id
}
```

**关键行为 — delta 流式追加：**

`appendDelta(text, streamId?)` 的 Vue 实现对比现在：

| 现在（Vanilla JS） | 重构后（Vue） |
|---|---|
| `state.currentDelta = textSpan` | 不需要 `currentDelta` |
| `textSpan.textContent += text` | `messages[last].content += text` |
| 手动判断是否复用上一个气泡 | `lastRole === 'assistant'` → 复用 `messages[last]` |
| `scrollDown()` 手动调用 | `watchEffect` 自动触发 |
| `debounceSave()` 手动调用 | 不再需要（消息从服务端恢复） |

`appendDelta` 简化为。注意 `streamId` 参数会在创建消息时存入 `audioStore.streamBubbles[streamId] = messageId`，用于 TTS 播放时的高亮追踪：

```ts
appendDelta(text: string, streamId?: number) {
  if (this.messages.length === 0 || this.messages[last].role !== 'assistant') {
    const msg = this.newAssistantMessage(text, streamId)
    this.messages.push(msg)
    if (streamId != null) {
      audioStore.streamBubbles[streamId] = msg.id  // TTS 高亮映射
    }
  } else {
    this.messages[last].content += text
  }
  // 滚动交由 MessageList 的 watch 处理
}
```

**loadMoreMessages（滚动加载历史）：**

函数签名与现有一致，通过 `fetch('/api/messages?limit=50&before=...')` 加载。加载完成后 `prependMessages` 变成 `messages.unshift(...)`，配合 `nextTick` 修正滚动位置。

### audioStore（由 settingsStore 提供 ttsEnabled/volume）

```ts
// stores/audioStore.ts
// ttsEnabled 和 volume 不从本 store 读取，而是引用 settingsStore
interface AudioState {
  audioQueue: AudioQueueItem[]
  audioPlaying: boolean
  audioChunks: Record<number, Uint8Array[]>
  streamBubbles: Record<number, string>   // stream_id → message id
  isMicActive: boolean
}
```

- `bufferChunk` / `finalizeStream` / `playNext` 逻辑与现有 `audio.js` 一致
- `AudioContext` 和 `MediaStream` 存储在 composable（`useAudio` / `useMic`）中，不在 store 中
- `volume` 通过 `<audio>` 元素的 `volume` 属性控制

### settingsStore

```ts
// stores/settingsStore.ts
interface SettingsState {
  ttsEnabled: boolean
  volume: number
  wakeEnabled: boolean
  live2dEnabled: boolean
  innerVoiceEnabled: boolean
  llmModel: string
  sttModel: string
  ttsModel: string
  customProviders: CustomProvider[]
  sttAvailable: boolean
  ttsAvailable: boolean
  llmModelName: string
  live2dStaticImage: string
}

// 所有 toggle 通过 watch 自动同步到 localStorage
// 模型切换通过 send() 发送 switch_model 消息
```

**localStorage 同步模式：**

```ts
// 在 settingsStore 中
watch(() => settings.ttsEnabled, (val) => {
  localStorage.setItem('elysia_tts', val ? 'on' : 'off')
}, { immediate: true })
```

### toolsStore

```ts
// stores/toolsStore.ts
interface ToolsState {
  plugins: PluginInfo[]
  loading: boolean
}

actions: {
  async loadPlugins(): Promise<void>   // fetch('/tools')
  async reloadPlugin(name: string): Promise<void>  // fetch('/tools/reload', POST)
}
```

## WebSocket 消息路由

### `src/composables/useWebSocket.ts`

```ts
// 消息类型 — union type
type ServerMessage =
  | SessionMessage | CapabilitiesMessage | DeltaMessage
  | MessageMessage | TokenUsageMessage | ExpressionMessage
  | StateMessage | AudioMessage | AudioEndMessage
  | TypingMessage | TranscriptionMessage | ActionMessage
  | CommandMessage | WakeMessage | SwitchModelResultMessage
  | HeartbeatAckMessage | ErrorMessage | UnknownMessage

function handleMessage(msg: ServerMessage) {
  switch (msg.type) {
    case 'delta':         chat.appendDelta(msg.content, msg.stream_id); break
    case 'message':       chat.handleCompleteMessage(msg); break
    case 'audio':         audio.bufferChunk(msg.stream_id, msg.seq, msg.data); break
    case 'audio_end':     audio.finalizeStream(msg.stream_id, msg.format); break
    case 'action':        handleAction(msg); break
    case 'command':       handleCommand(msg); break
    case 'session':       chat.restoreFromServer(msg.messages); break
    case 'capabilities':  settings.$patch({ ...msg }); break
    case 'typing':        chat.typing = msg.typing; break
    case 'transcription': chat.addUserMessage('🎤 ' + msg.text); break
    case 'expression':    chat.mood = msg.expression; chat.moodIntensity = msg.intensity; break
    case 'state':         chat.fsmState = msg.state; break
    case 'token_usage':   chat.updateTokenUsage(msg); break
    case 'error':         console.warn('Server:', msg.message); break
    case 'wake':          audio.flashWakeIndicator(); break
    case 'switch_model_result': handleSwitchResult(msg); break
    default:              console.warn('Unknown:', msg.type); break
  }
}
```

- `action` 和 `command` 类型涉及多个 store 操作，抽取为 `handleAction` / `handleCommand` 函数
- 其余类型每个 case 一行，直接调用 Pinia action

### TypeScript 消息类型

```ts
// types/protocol.ts
// 映射自 core/models/Protocol.cs 的 20+ 消息类型

interface DeltaMessage {
  type: 'delta'
  content: string
  stream_id?: number
}

interface AudioMessage {
  type: 'audio'
  stream_id: number
  seq: number
  data: string           // base64
}

interface AudioEndMessage {
  type: 'audio_end'
  stream_id: number
  format?: 'wav' | 'mp3'
}

interface ActionMessage {
  type: 'action'
  action: 'tool_executing' | 'tool_done' | string
  params: Record<string, unknown>
}

interface CommandMessage {
  type: 'command'
  command: 'tool_approval' | 'block' | 'inner_voice'
  params?: Record<string, unknown>
}

// ... 其余 15+ 类型定义相同模式
```

## 组件接口定义

### App.vue（根组件）

```
┌─────────────────────────────────────────────┐
│  <CharacterPanel>    <MessageList>           │
│  ┌───────────┐     ┌──────────────────┐      │
│  │ Avatar    │     │ TimeDivider      │      │
│  │ Display   │     │ ChatMessage      │      │
│  │           │     │ ChatMessage      │      │
│  │ Status    │     │ ToolCard         │      │
│  │           │     │ ChatMessage      │      │
│  │ Token Bar │     │ ...              │      │
│  │ Mood      │     │ TypingIndicator  │      │
│  └───────────┘     └──────────────────┘      │
│                 ┌────────────────┐           │
│                 │ PreviewBar     │           │
│                 │ InputBar       │           │
│                 └────────────────┘           │
├─────────────────────────────────────────────┤
│  <SettingsPanel>  (浮层)                     │
│  <ToolsPanel>     (浮层)                     │
│  <ApprovalDialog> (Teleport to body)         │
│  <Lightbox>       (Teleport to body)         │
└─────────────────────────────────────────────┘
```

### MessageList 滚动策略

```ts
// watch messages 变化
watch(() => chatStore.messages.length, () => {
  if (!chatStore.userScrolledUp) {
    nextTick(() => scrollToBottom())
  }
})

// 用户滚动检测
function onScroll(event: Event) {
  const el = event.target as HTMLElement
  const threshold = 60
  chatStore.userScrolledUp = el.scrollTop + el.clientHeight < el.scrollHeight - threshold
}
```

### ToolCard 状态机

```
executing ──success──→ done
executing ──failed───→ fail
done / fail: 点击展开/折叠详细内容（参数 + 结果）
```

**与现有差异：** 当前 `state.toolCards[id]` 缓存 DOM 引用，用 ID 匹配 tool_done 消息。Vue 中 ToolCard 通过 props 接收数据，status 由 chatStore 管理，不再需要 DOM 引用缓存。

### ApprovalDialog 弹窗

```
<Teleport to="body">
  <div class="approval-overlay" @click.self="...">
    <div class="approval-dialog">
      <div class="approval-icon">⚠️</div>
      <div class="approval-title">工具操作确认</div>
      <div class="approval-tool">工具: {{ tool }}</div>
      <div class="approval-detail">{{ params }}</div>
      <div class="approval-buttons">
        <button @click="deny">拒绝</button>
        <button @click="approve">允许</button>
        <button @click="alwaysAllow">总是允许</button>
      </div>
    </div>
  </div>
</Teleport>
```

### Debug 页面（Vite 多页面入口，零后端改动）

独立于主聊天页面的 Vue 应用，使用 Vite 的多页面模式（multi-page entry）。
HTML 入口平铺在 `frontend/` 根目录，构建产物直接生成 `dist/debug.html`，与后端现有代码期望的 `static/debug.html` 路径完全一致，**无需修改后端代码**。

```
frontend/
├── index.html                  ← Chat 页面入口（默认）
├── debug.html                  ← Debug 页面入口（平铺，构建后为 dist/debug.html）
└── src/
    ├── main.ts                 ← Chat 应用入口
    └── debug/
        ├── main.ts             ← Debug 应用入口（独立 createApp）
        ├── App.vue             ← Debug 根组件
        └── components/         ← 16 个面板组件
```

**关键区别：**
- Debug 应用不从 Pinia store 共享状态，所有数据通过 `fetch('/debug/api')` 轮询获取（与现有逻辑一致）
- Chat 应用和 Debug 应用各自调用 `createApp`，互不干扰
- Vite 配置通过 `build.rollupOptions.input` 指定多入口

**Vite 配置：**

```ts
import { resolve } from 'path'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: 'http://localhost:8765', ws: true },
      '/api': 'http://localhost:8765',
      '/health': 'http://localhost:8765',
      '/ready': 'http://localhost:8765',
      '/shutdown': 'http://localhost:8765',
      '/restart': 'http://localhost:8765',
      '/tools': 'http://localhost:8765',
      '/uploads': 'http://localhost:8765',
      '/live2d': 'http://localhost:8765',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        debug: resolve(__dirname, 'debug.html'),
      },
    },
  },
})
```

**开发模式：** `http://localhost:5173/` 访问 Chat，`http://localhost:5173/debug.html` 访问 Debug。两者都由 Vite dev server 直接 serve，不经过后端代理。

**构建产物：**
```
dist/
├── index.html         ← Chat 页面
├── debug.html         ← Debug 页面（扁平，与后端期望路径一致）
└── assets/
    ├── main-xxx.js    ← Chat 应用 bundle
    └── debug-xxx.js   ← Debug 应用 bundle
```

部署时 `cp -r frontend/dist/* static/` 将 `debug.html` 直接复制到 `static/debug.html`，后端现有 `web.FileResponse(debug_path)` 无需修改。

## 样式方案

- 保留现有 CSS 变量（`:root` 中的 `--bg`, `--surface`, `--text`, `--accent` 等）
- 迁移到 `variables.css`，在 `main.ts` 中全局引入
- 每个 Vue 组件的样式使用 `<style scoped>`，避免全局污染
- 不使用 Tailwind 或 CSS-in-JS — 保持简单，只做结构迁移

## 构建与部署

### 开发环境

```bash
cd frontend
npm install
npm run dev          # Vite dev server :5173 → 代理到 :8765
```

`vite.config.ts` 完整配置详见 **[Debug 页面章节](#debug-页面vite-多页面入口零后端改动)** 的 Vite 配置块，此处不再重复。

### 生产构建

```bash
# 构建前端
cd frontend && npm run build

# 构建输出到 frontend/dist/（包含了 chat 和 debug 两个入口）
# 部署方式：将 dist/ 内容复制到 static/ 目录
cp -r frontend/dist/* static/

# 或者直接软链（开发阶段快速迭代用）：
# ln -sf ../frontend/dist/index.html static/index.html
# ln -sf ../frontend/dist/debug.html static/debug.html
# ln -sf ../frontend/dist/assets static/assets

# 原有的静态文件路径保持不变：
# GET / → static/index.html（由 aiohttp FileResponse 服务）
# GET /static/* → 由 aiohttp add_static 服务
# GET /uploads/*、/live2d/* → 不变
```

后端 `server.py` **零改动**。`serve_static=True` 时 `index_handler` 返回 `dist/index.html`（由 `FileResponse(index_path)` 服务）。

### 开发模式下的 debug 页面

`debug.html` 作为 Vite 多页面入口，开发模式下由 Vite dev server 直接 serve。访问 `http://localhost:5173/debug.html` 即可，**不经过后端代理**。

多页配置见 `rollupOptions.input` 中的 `debug` 入口（上一节完整配置中已包含）。

## 与现有后端的关系

| 端点 | 处理方式 |
|------|---------|
| `GET /` | Vue 应用入口（原 index.html 替换） |
| `GET /debug` | Vue Debug 应用入口（Vite 多页平铺，路径零改动） |
| `WS /ws` | 不变，代理到后端 |
| `GET /api/*` | 不变，代理到后端 |
| `GET /health /ready` | 不变，代理到后端 |
| `POST /shutdown /restart` | 不变，代理到后端 |
| `POST /tools /tools/reload` | 不变，代理到后端 |
| `GET /static/*` | 不变，由后端 serve |
| `GET /uploads/*` | 不变，由后端 serve |
| `GET /live2d/*` | 不变，由后端 serve |

后端**零改动**。

## 迁移步骤

共 4 个阶段，预计 3-5 天：

### 第 1 阶段：搭建脚手架（0.5 天）

1. 在 `elysia-agent/` 下创建 `frontend/` 目录，`npm create vite@latest` 初始化 Vue 3 + TS 项目
2. 配置 `vite.config.ts` 代理规则
3. 创建 `src/types/protocol.ts` — 20+ 消息类型定义（从后端 Protocol.cs 翻译）
4. 创建 5 个 Pinia store 骨架
5. 创建 `src/styles/variables.css` — 现有 CSS 变量迁移
6. 验证：Vite dev server 能启动，`/ws` 代理到后端

### 第 2 阶段：核心聊天组件（2 天）

1. `App.vue` + `AppLayout.vue` — 三栏布局
2. `CharacterPanel` + `AvatarDisplay` + `CharStatus`
3. `MessageList` + `ChatMessage` + `ChatBubble`
4. `ToolCard` + `TokenLabel` + `TimeDivider` + `InnerVoice` + `TypingIndicator`
5. `InputBar` + `PreviewBar`
6. `Lightbox`
7. 集成 `useWebSocket` composable 和 WebSocket 消息路由
8. 验证：能连接后端、收发消息、流式渲染

### 第 3 阶段：面板组件 + 其余功能（1 天）

1. `SettingsPanel` + `ToggleSwitch` + `ModelSelector` + `CustomModal`
2. `ToolsPanel`
3. `ApprovalDialog`
4. 麦克风集成（`useMic` composable）
5. TTS 音频集成（`useAudio` composable + audioStore）
6. 代码块渲染（`CodeBlock` + highlight.js）
7. Markdown 渲染（marked npm 包）
8. 验证：所有功能与现有前端一致

### 第 4 阶段：Debug 页面 + 收尾（0.5 天）

1. 创建 `debug.html` 入口文件（平铺在 `frontend/` 根目录）
2. 实现 16 个 debug 面板组件
3. 配置 Vite 多页面入口（`rollupOptions.input` 增加 `debug`）
4. 构建验证、复制到 `static/`
5. 删除旧前端文件

## Live2D 集成方案

Live2D 使用 imperative canvas API（Cubism SDK via WASM），与 Vue 的声明式模型有根本差异。以下是迁移方案。

### 当前行为

```
AvatarDisplay
  ├── settingsStore.live2dEnabled === true  → <iframe>（指向 /static/live2d-demo/index.html）
  └── settingsStore.live2dEnabled === false → <img>（static-img, src 由 WS capabilities 更新）
```

### Vue 组件设计

```vue
<!-- AvatarDisplay.vue -->
<template>
  <div class="avatar-wrap">
    <iframe v-if="live2dEnabled" ref="live2dFrame"
            src="/static/live2d-demo/index.html"
            sandbox="allow-scripts allow-same-origin" />
    <img v-else :src="staticImageUrl" class="static-img" />
  </div>
</template>
```

- `live2dEnabled` 和 `staticImageUrl` 来自 `settingsStore`
- iframe 通过 `ref="live2dFrame"` 获取引用，不做 props/events 通信（iframe 内部 canvas 与 Vue 隔离）
- 切换时 Vue 的 `v-if` 自动销毁/重建 iframe 或 img 元素

### Canvas / WASM 生命周期

Live2D 渲染（WASM canvas）完全在 iframe 内运行，Vue 组件对其没有直接干预：

| 操作 | 现有前端 | Vue 方案 |
|------|---------|----------|
| 显示/隐藏 | 手动 `element.remove()` / `document.createElement('iframe')` | `v-if="live2dEnabled"` |
| 表情切换 | 通过 WS 消息 `expression` → iframe postMessage | 不变（仍在 ws_handler 中转发） |
| 动作播放 | 通过 WS 消息 `action` → iframe postMessage | 不变 |
| 静态图更新 | WS `capabilities` → `document.getElementById('live2d-static-img').src = ...` | `settingsStore.live2dStaticImage = ...`，Vue 响应式更新 `<img :src>` |

**核心原则：Live2D canvas 的生命周期不进入 Pinia store。** iframe 保持独立沙箱，Vue 只控制其挂载/卸载。

### 静态图降级路径（不变）

```
WS capabilities → live2d_static_image URL → settingsStore.live2dStaticImage
                                           → AvatarDisplay 渲染 <img>
```

与现有代码完全一致，只是 DOM 操作替换为 Vue 模板绑定。

## TTS 气泡排队同步

当前 `audio.js` 中 `playNext()` 调用 `revealNextQueuedBubble()` 来实现 TTS 播放时给气泡排队。迁移后需要跨 store 协调。

### 现状

```js
// audio.js playNext():
revealNextQueuedBubble();          // 播放开始时显示排队的气泡
// 在 chat.js:
revealNextQueuedBubble() {
  const row = state.nextBubbleQueue.shift()
  document.getElementById('chat').appendChild(row)
}
highlightPlaying(bubble);           // 高亮正在播放的气泡
```

### Vue 方案

```vue
<!-- MessageList.vue -->
<template>
  <div>
    <template v-for="msg in visibleMessages">
      <ChatMessage :message="msg" ... />
    </template>
  </div>
</template>
```

**排队机制通过 chatStore 的可响应字段实现：** `nextBubbleQueue` 和 `playingMessageId` 已在 chatStore 的 `ChatState` 中声明。

**时序：**

```
1. delta 开始流式追加 → chatStore.appendDelta(text)
   → 如果 audioStore.audioPlaying === true，不立即显示，将 message id 加入 nextBubbleQueue
   → 否则正常显示

2. audio 流结束时 → audioStore.finalizeStream() → playNext()
   → 播放开始前调用 chatStore.revealNextQueued()

3. chatStore.revealNextQueued()
   → 从 nextBubbleQueue 取出 message id
   → 设置 playingMessageId = id（触发高亮）
   → 更新 visibleMessages（从 pending 转为 visible）

4. 播放结束后 → audio 触发 onended
   → clearPlayingHighlight()
   → 如果还有排队气泡，重复步骤 2-3
```

在 Vue 中，消息是否显示由 `visibleMessages` computed 控制：

```ts
const visibleMessages = computed(() => {
  // 正在播放期间，pending 状态的助理消息推迟显示
  // 直到 audioStore 确认可以显示下一个
  return chatStore.messages.filter(m => !m.queued || m.id === chatStore.playingMessageId)
})
```

### 高亮同步

`<ChatMessage>` 组件根据 prop `isPlaying` 决定是否添加 `playing` 和 `pulse` CSS class，不再通过 DOM 引用操作。

### 气泡入场动画

当前排队气泡出场时有一个 `opacity: 0 → 1` + `translateY(8px) → 0` 的过渡动画。Vue 中使用 `<TransitionGroup>` 包裹 `<MessageList>` 内的消息行实现：

```vue
<TransitionGroup name="bubble">
  <ChatMessage v-for="msg in visibleMessages" :key="msg.id" ... />
</TransitionGroup>

<style>
.bubble-enter-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}
.bubble-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
</style>
```

当 `queued` 消息被 `revealNextQueued()` 移入 `visibleMessages` 时，`<TransitionGroup>` 自动播放入场动画，无需手动 `requestAnimationFrame`。

## ContentPart 类型定义

```ts
// types/protocol.ts
type ContentPart =
  | { type: 'text'; content: string }
  | { type: 'image'; src: string; mime?: string }
```

消息的 `content` 字段：

```ts
interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string | ContentPart[]
  // ...
}
```

当前后端发送的用户消息可能是 `{type: 'text', content: "hello"}` 或 `{type: 'image', src: '/uploads/...'}` 数组。ContentPart 覆盖这两种情况。

## XSS 防护说明

现有前端通过 `esc.js` 手动转义 HTML 特殊字符，用于 `innerHTML` 字符串拼接。迁移后：

- **Vue 模板中**（`.vue` 文件的 `<template>`）：`{{ expression }}` 自动转义 HTML，无需手动调用 `esc()`
- **非模板上下文**：仍有少数场景需要手动转义（如 `ToolCard` 中显示 args JSON 时），保留一个轻量的 `escapeHtml` 工具函数
- **`innerHTML` 清零计划**：除了 `ChatBubble.markdown` 使用 `v-html`（内容来自 marked.js 的 parse 结果），其余所有拼接都将使用 Vue 模板，不再需要 `element.innerHTML = ...`

```ts
// src/utils/escape.ts — 需要手动转义的边缘场景
export function escapeHtml(str: string): string {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
```

## 错误边界

现有前端没有错误处理，一个 JS 异常可能导致白屏。引入边界：

- 每个 `ChatBubble.vue` 组件 `onErrorCaptured` 捕获渲染异常，降级显示纯文本
- `useWebSocket` composable 中 JSON 解析失败仅 `console.warn`，不崩溃
- 图片加载失败显示 fallback 图标（现有 `lightbox` 已有类似处理）
- AudioContext 创建失败时（浏览器限制），麦克风按钮禁用并显示提示

## 未涵盖的场景（后续迭代）

- PWA / Service Worker（离线缓存）
- 单元测试（组件测试 + store 测试）
- i18n 国际化
- 主题切换器
- 消息编辑
