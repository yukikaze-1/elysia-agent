# Elysia Agent 前端接入 API

> 供第三方前端（Web、.NET WPF、移动端等）对接 Elysia Agent 后端。

## 连接信息

| 项目 | 值 |
|------|-----|
| 传输 | WebSocket（主要）+ HTTP REST（辅助） |
| WS 端点 | `ws://{host}:{port}/ws` |
| 消息格式 | JSON 文本帧 |
| 编码 | UTF-8 |

## 连接流程

```
客户端 ── WS 连接 ──→ /ws
         ←── { type: "session", messages: [...], token_stats: {...} }
         ←── { type: "capabilities", stt: bool, tts: bool, llm_model: "...", ... }
         ←── { type: "state", state: "idle" }
         
         ... 正常通信 ...
         
客户端 ←──→ 各种消息
```

### 重连行为

WS 断开后 session 保留在服务端。重连时：
- 再次收到完整的 `session`（全部历史消息）+ `capabilities` + `state`
- 前端应**全量替换**消息列表，避免重复渲染

### 辅助通道：Binary 帧（麦克风）

除 JSON 文本帧外，WS 还接受 **binary 帧** 用于语音输入。每帧为 Int16 PCM 音频（采样率取决于 STT 后端，通常 16000Hz/mono）。仅在服务端 `stt: true` 时有效。纯文本前端可忽略。

---

## WebSocket 消息协议

### 发送方向总览

```
Server ──→ Client (16 种 type，action/command 含子类型):
  session, capabilities, state
  delta, message, typing
  expression
  action  → tool_executing | tool_done | (Live2D)
  command → tool_approval | inner_voice | block
  audio, audio_end
  transcription, wake
  token_usage, switch_model_result, error

Client ──→ Server (5 种):
  text, interrupt, switch_model, tool_approval, wake_toggle
```

---

## Server → Client（后端推送）

### 1. `session` — 连接时发送会话历史

连接建立后第一条消息，用于恢复之前的聊天记录和 token 统计。

```json
{
  "type": "session",
  "messages": [
    {
      "role": "user",
      "parts": [{ "content": "你好" }],
      "content": [{ "type": "text", "content": "你好" }],
      "content_type": {}
    },
    {
      "role": "assistant",
      "parts": [{ "content": "你好！有什么可以帮你的？" }],
      "content": [{ "type": "text", "content": "你好！有什么可以帮你的？" }],
      "content_type": {},
      "mood": "happy",
      "tool_calls": []
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc123",
      "name": "web_search",
      "content": "搜索结果...",
      "success": true,
      "error": null,
      "error_type": null,
      "duration_ms": 1234,
      "truncated": false
    }
  ],
  "token_stats": {
    "total": 15420,
    "by_model": { "deepseek/deepseek-v4-flash": 15420 }
  }
}
```

**消息角色说明：**

| `role` | 含义 | 关键字段 |
|--------|------|---------|
| `user` | 用户消息 | `content`（多模态数组）, `parts`（纯文本简化视图） |
| `assistant` | AI 回复 | `content`, `parts`, `mood`, `tool_calls` |
| `tool` | 工具执行结果 | `tool_call_id`, `name`, `success`, `error`, `duration_ms` |

**通用字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `role` | string | `"user"` \| `"assistant"` \| `"tool"` |
| `parts` | array | 纯文本简化视图 `[{ "content": "..." }]`——可直接拼接渲染 |
| `content` | array | 多模态内容数组，每项 `{ "type": "text\|image", "content\|source": "...", "mime": "..." }` |
| `content_type` | object | 多模态标记，如 `{"has_image": true, "image_count": 1}` |
| `mood` | string | 仅 assistant，情绪标签 |

**assistant 特有字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `tool_calls` | array | LLM 请求的工具调用列表，每项 `{ "id": "...", "function": { "name": "...", "arguments": "..." } }` |

**tool 消息特有字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `tool_call_id` | string | 对应的 tool_call ID |
| `name` | string | 工具名 |
| `success` | bool | 执行是否成功 |
| `error` | string\|null | 错误信息（成功时为 null） |
| `error_type` | string\|null | 错误类型码 |
| `duration_ms` | int | 工具执行耗时（毫秒） |
| `truncated` | bool | 结果是否被截断 |

**渲染提示：** 收到 assistant 消息且 `tool_calls` 非空 → 显示"调用工具"过渡卡片。收到 tool 消息 → 替换为工具结果卡片。

### 2. `capabilities` — 服务能力声明

```json
{
  "type": "capabilities",
  "stt": true,
  "tts": true,
  "llm_model": "deepseek/deepseek-v4-flash",
  "live2d_static_image": "/static/Elysia.png"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `stt` | bool | 语音识别是否可用 |
| `tts` | bool | 语音合成是否可用 |
| `llm_model` | string | 当前 LLM 模型标识 |
| `live2d_static_image` | string | Live2D 静态图（无 Live2D 时用） |

### 3. `state` — Agent 状态变更

```json
{ "type": "state", "state": "speaking" }
```

`state` 取值：
| 值 | 含义 |
|----|------|
| `idle` | 空闲，等待用户输入 |
| `thinking` | 正在调用 LLM 思考 |
| `executing` | 正在执行工具调用 |
| `speaking` | 正在输出回复 |
| `interrupted` | 被打断，正在重置 |
| `idle_background_task` | 空闲但后台有异步任务运行（可继续对话） |

前端可用此状态显示加载动画、禁用输入框等。注意 `idle_background_task` 下用户仍可发消息——行为同 `idle`。

### 4. `delta` — 流式文本增量

```json
{ "type": "delta", "content": "你好" }
{ "type": "delta", "content": "今天天气真好。", "stream_id": 3 }
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | string | 文本内容（单字、短语或完整句子） |
| `stream_id` | int（可选） | TTS 开启时存在，绑定了同一句的音频流，可作为气泡 key |

**TTS 关闭时**：无 `stream_id`，逐字推送，前端累积到当前气泡。
**TTS 开启时**：有 `stream_id`，每条 `delta` 是完整句子，直接创建新气泡。

### 5. `message` — 完整消息 / 句边界

两种用途：

**句边界标记**（TTS 关闭时）：
```json
{ "type": "message", "content": "" }
```
`content` 为空字符串 → 表示上一句结束。前端应归档当前气泡，下一句 `delta` 创建新气泡。

**完整消息**（非流式场景、系统通知）：
```json
{
  "type": "message",
  "role": "assistant",
  "content": "完整回复内容...",
  "mood": "curious"
}
```

### 6. `typing` — 输入状态指示

```json
{ "type": "typing", "typing": true }
{ "type": "typing", "typing": false }
```

LLM 开始/结束生成时推送。前端可显示/隐藏 "..." 动画。

### 7. `expression` — 角色表情

```json
{
  "type": "expression",
  "expression": "smile",
  "intensity": 0.8
}
```

用于驱动 Live2D 或静态图的表情切换。纯 .NET 客户端可忽略此消息。

### 8. `action` — 工具执行状态

`action` 用于推送工具执行的生命周期事件。子类型由 `action` 字段区分：

#### 8a. `tool_executing` — 工具开始执行

```json
{
  "type": "action",
  "action": "tool_executing",
  "params": {
    "tool": "web_search",
    "call_id": "call_abc123",
    "args": { "query": "Python 3.13 新特性" }
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `params.tool` | string | 工具名 |
| `params.call_id` | string | 工具调用唯一 ID（后续 `tool_done` 用此 ID 匹配） |
| `params.args` | object | 工具参数（key-value，value 已转为字符串） |

**前端应**：创建一张"执行中"卡片（旋转动画），用 `call_id` 作为卡片 key。

#### 8b. `tool_done` — 工具执行完成

```json
{
  "type": "action",
  "action": "tool_done",
  "params": {
    "tool": "web_search",
    "call_id": "call_abc123",
    "success": true,
    "content": "搜索结果...",
    "error": null,
    "error_type": null,
    "duration_ms": 1234,
    "truncated": false
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `params.tool` | string | 工具名 |
| `params.call_id` | string | 与 `tool_executing` 的 `call_id` 对应 |
| `params.success` | bool | 执行是否成功 |
| `params.content` | string | 工具返回内容（文本） |
| `params.error` | string\|null | 错误信息（成功时为 null） |
| `params.error_type` | string\|null | 错误类型码 |
| `params.duration_ms` | int | 执行耗时（毫秒） |
| `params.truncated` | bool | 结果是否被截断 |

**前端应**：找到 `call_id` 对应的执行中卡片，更新状态：
- 成功：绿色 ✓ + 耗时 + 展开显示结果内容
- 失败：红色 ✗ + 错误信息，**默认展开**（失败信息需要可见）
- `truncated: true` → 追加"(已截断)"提示

#### 8c. Live2D 动作（可选）

```json
{ "type": "action", "action": "wave", "params": { "duration": 2.0 } }
```

纯功能型客户端可忽略。仅 Live2D 前端使用。

### 9. `command` — 指令

`command` 用于推送需要前端交互的指令。子类型由 `command` 字段区分：

#### 9a. `tool_approval` — 工具审批

```json
{
  "type": "command",
  "command": "tool_approval",
  "params": {
    "tool": "write_file",
    "params": "{'path': 'report.md', ...}",
    "request_id": "msg_abc123"
  }
}
```

需要用户确认的危险操作（如 `write_file`）。前端应弹出确认对话框，用户选 approve/deny 后发送 `tool_approval` 消息回复。

#### 9b. `inner_voice` — AI 内心独白

```json
{
  "type": "command",
  "command": "inner_voice",
  "params": {
    "content": "她今天好像不太一样…但又说不上来哪里不对。"
  }
}
```

Elysia 的内心独白——她想了但没说出来的话。前端应以淡色/斜体/小字渲染，出现在公开回复气泡的上方。inner_voice 在流式输出结束后一次性发送。

前端设置面板提供"内心独白" toggle（`#inner-voice-toggle`）控制气泡显示/隐藏。关闭时 body 添加 `inner-voice-hidden` class，CSS 隐藏已有气泡 + JS guard 阻止新气泡创建。

#### 9c. `block` — 结构化内容块

```json
{
  "type": "command",
  "command": "block",
  "params": {
    "block_type": "code",
    "content": "print('hello')",
    "language": "python"
  }
}

{
  "type": "command",
  "command": "block",
  "params": {
    "block_type": "markdown",
    "content": "# 标题\n这是 **markdown** 内容"
  }
}
```

LLM 回复中的代码块或 Markdown 块单独推送。`block_type` 取值：`"code"`（language 字段为编程语言名）| `"markdown"`（无 language 字段）。

**前端应**：代码块用等宽字体 + 语法高亮渲染，Markdown 块渲染为富文本。这些块独立于流式 delta 文字，作为独立 UI 元素插入。

### 10. `audio` + `audio_end` — TTS 音频流

```json
{ "type": "audio", "data": "<base64>", "stream_id": 0, "seq": 0, "is_final": false }
...
{ "type": "audio_end", "stream_id": 0 }
```

- `data`：base64 编码的音频数据。**只有每句第一块（seq=0）含 WAV/RIFF 头**，后续块为裸 PCM 续流。拼接全部块后得到完整 WAV 文件
- `stream_id`：区分并发音频流（通常为 0）
- `seq`：递增序号，用于排序和去重
- `is_final`：始终为 `false`，音频结束由 `audio_end` 标记

**音频流模型：**

```
每个 stream_id:
  audio { seq: 0 }  ← WAV header + PCM 开头
  audio { seq: 1 }  ← 纯 PCM 续流（无 header）
  audio { seq: 2 }  ← 纯 PCM 续流
  ...
  audio_end          ← 流结束，触发合并播放
```

**处理规则：**
- 收到 `audio` → 按 `stream_id` 分组，按 `seq` 排序缓冲
- 收到 `audio_end` → 按 `seq` 顺序拼接所有块 → 完整 WAV → 播放
- **不要**逐块播放——只有第一块有 WAV 头，后续块单独播放是噪音

**音频格式：** 解码方式从首块 WAV 头读取（采样率/声道/位深）。GPT-SoVits 为 32000Hz/mono/int16，OpenAI TTS 为 24000Hz/mono/int16。播放库：Web 用 `Audio` 元素 + `Blob`，.NET 用 NAudio/MediaPlayer。

### 11. `transcription` — STT 识别结果

```json
{
  "type": "transcription",
  "text": "帮我查一下天气",
  "emotion": "NEUTRAL",
  "events": ["laugh"]
}
```

用户语音转文字后推送给前端显示。`emotion` 和 `events` 可选。

### 12. `wake` — 唤醒词检测

```json
{ "type": "wake" }
```

检测到唤醒词时推送。前端可用作动画/音效触发。

### 13. `token_usage` — Token 消耗统计

```json
{
  "type": "token_usage",
  "turn": {
    "rounds": [
      {
        "model": "deepseek/deepseek-v4-flash",
        "prompt_tokens": 1234,
        "completion_tokens": 567,
        "total_tokens": 1801,
        "round_num": 0,
        "timestamp": 1719000000.123
      }
    ],
    "turn_total": 1801
  },
  "session": {
    "total": 15420,
    "by_model": { "deepseek/deepseek-v4-flash": 15420 }
  }
}
```

| 字段 | 说明 |
|------|------|
| `turn.rounds` | 当前轮每轮 LLM 调用详情 |
| `turn.turn_total` | 当前轮总计 token |
| `session.total` | 整个会话累计 token |
| `session.by_model` | 按模型分组的累计 token |

每轮 LLM 调用结束后推送。`state` 变为 `idle` 时可将 turn 级的 token 标签贴到最后一句话上。

### 14. `switch_model_result` — 模型切换结果

```json
{
  "type": "switch_model_result",
  "service": "llm",
  "model": "deepseek/deepseek-v4-flash",
  "ok": true,
  "error": null
}
```

### 15. `error` — 错误消息

```json
{ "type": "error", "message": "Invalid JSON" }
```

---

## 流式文字与 TTS 音频同步

### 分句策略

服务端使用 `SentenceBuffer` 按标点符号（`。！？.!?\n～…` 等）切分 LLM 输出流。每检测到一个完整句子，根据 TTS 是否可用走不同路径。

### TTS 开启时（绑定发送）

服务端**不发送**逐字 delta。每个完整句子触发一次 TTS 合成，等首块音频到达后**绑定发送**：

```
每个句子 (stream_id=N):
  delta     { stream_id: N, content: "完整句子文字" }   ← 创建新气泡
  audio     { stream_id: N, seq: 0, data: "<base64>" }  ← 第 0 块音频
  audio     { stream_id: N, seq: 1, data: "<base64>" }  ← 第 1 块音频
  ...
  audio_end { stream_id: N }                             ← 本句音频结束
```

#### 音频缓冲与合并播放

**不要**收到一块音频就播一块。必须等 `audio_end` 后合并为完整 WAV 再播放：

```
// 缓冲阶段
audio 到达 → base64 decode → byte[] → push 到 audioChunks[streamId]

// 合并播放
audio_end 到达:
  1. concat 所有 chunks → 完整 WAV bytes
  2. 创建可播放对象 (Web: Blob + URL.createObjectURL / .NET: MemoryStream)
  3. push { url, streamId } 到 audioQueue (FIFO)
  4. 调用 playNext()
```

**`playNext()` 实现逻辑：**

```
if (正在播放 || 队列空) return
取队首 { url, streamId }:
  player = new AudioPlayer(url)
  player.volume = volume
  player.play()

  player.onEnded:
    释放音频资源 (Web: URL.revokeObjectURL / .NET: stream.Dispose)
    清理 streamBubbles[streamId]
    audioPlaying = false
    revealNextQueuedBubble()   ← 弹出下一个隐藏气泡
    clearPlayingHighlight()    ← 取消蓝光
    playNext()                 ← 递归播放下一句
```

关键设计：**FIFO 串行播放**。上一句播完才播下一句，保证句间顺序。

#### 文字-语音同步（核心）

`stream_id` 是文字和音频的桥梁：

**delta 处理 (appendDelta)：**

```
delta 到达 (带 stream_id):
  创建新气泡
  streamBubbles[streamId] = bubble    ← 记录映射

  if (audioPlaying):
    气泡放入 nextBubbleQueue（隐藏，不挂 DOM）  ← 音字同步关键
  else:
    气泡直接 appendChild 到聊天区
```

**为什么排队：** TTS 开启时，服务端文字和音频绑定发送，但文字可能比音频先到。如果当前句音频还在播，下一句文字不应该提前显示——否则用户看到还没听到的话。所以 `audioPlaying == true` 时，新气泡暂存 `nextBubbleQueue`（`opacity: 0`，不挂 DOM）。

**播放完成后的弹出：**

```
onEnded → revealNextQueuedBubble():
  row = nextBubbleQueue.shift()
  DOM.appendChild(row)
  requestAnimationFrame:
    row.opacity = 1               ← 淡入
    row.transform = translateY(0) ← 从下方滑入
```

#### 播放高亮

当前正在播放的句子对应气泡高亮：

```
playNext() → highlightPlaying(bubble):
  playingBubble = bubble
  bubble.classList.add('playing', 'pulse')   ← 蓝光 + 呼吸动画

onEnded → clearPlayingHighlight():
  playingBubble.classList.remove('playing', 'pulse')
  playingBubble = null
```

#### 用户打断时的清理

用户发新消息时，清空所有排队和映射：

```
用户发消息:
  finalizeDelta()              ← 归档当前气泡
  nextBubbleQueue = []         ← 丢弃排队气泡
  streamBubbles = {}           ← 清空 stream→bubble 映射
  playingBubble 高亮取消
```

#### .NET 端需要的状态字段

| 变量 | 类型 | 用途 |
|------|------|------|
| `audioChunks` | `Dictionary<int, List<byte[]>>` | 按 stream_id 缓冲音频块 |
| `audioQueue` | `Queue<AudioItem>` | 待播放的完整 WAV（FIFO） |
| `audioPlaying` | `bool` | 当前是否正在播放 |
| `streamBubbles` | `Dictionary<int, BubbleView>` | stream_id → 气泡控件映射 |
| `nextBubbleQueue` | `Queue<BubbleView>` | 音频播放中暂缓显示的气泡 |
| `playingBubble` | `BubbleView?` | 当前高亮的控件 |

### TTS 关闭时（纯文字流）

LLM 逐 token 产出 → 逐字 `delta`（无 `stream_id`）推送给前端：

```
delta { content: "今" }
delta { content: "天" }
...
delta { content: "今天天气真好。" }   ← 完整句（SentenceBuffer 切出）
message { content: "" }              ← 句边界标记
```

**前端处理规则：**

1. `delta` 逐字追加到当前气泡
2. `message` 且 `content: ""` → **句边界**：归档当前气泡，下一句 `delta` 创建新气泡
3. `state` 变为 `idle` → 全部句子完成，最后一个气泡也归档

### audio 格式

详见 [10. audio + audio_end](#10-audio--audio_end---tts-音频流)。

---

## 工具执行生命周期

LLM 决定调用工具后，前端收到一系列消息来展示工具执行进度：

```
state { state: "executing" }
  → action { action: "tool_executing", params: { tool, call_id, args } }   ← 工具 1 开始
  → action { action: "tool_executing", params: { tool, call_id, args } }   ← 工具 2 开始（可并行）
  ...
  → action { action: "tool_done", params: { call_id, success, content, ... } }  ← 工具 1 完成
  → action { action: "tool_done", params: { call_id, success, content, ... } }  ← 工具 2 完成
state { state: "thinking" }   （或 "speaking" → 流式文字）
```

**前端渲染规则：**

1. 收到 `tool_executing` → 创建工具卡片（旋转动画 + "执行中"），用 `call_id` 做 key
2. 收到 `tool_done` → 找到对应 `call_id` 的卡片，更新为完成/失败状态
3. `state` 变为 `executing` → 可显示"使用工具…"全局状态提示
4. 多个工具并行：`tool_executing` 可能连续来多个，独立建卡
5. 工具结果也会在 `session` 消息中作为 `role: "tool"` 出现（用于历史重放）
6. 会话重放时：assistant 消息含 `tool_calls` → 先建卡片；立即跟 tool 消息 → 更新卡片

详见 [8a. tool_executing](#8a-tool_executing---工具开始执行) 和 [8b. tool_done](#8b-tool_done---工具执行完成)。

---

## Client → Server（客户端发送）

### 1. `text` — 发送消息

**纯文本：**
```json
{ "type": "text", "content": "帮我查一下Python 3.13的新特性" }
```

**多模态（文本+图片）：**
```json
{
  "type": "text",
  "content": [
    { "type": "text", "content": "描述这张图片" },
    { "type": "image", "source": "/uploads/img_abc123.png", "mime": "image/png" }
  ]
}
```

### 2. `interrupt` — 打断

```json
{ "type": "interrupt" }
```

立即停止当前 LLM 生成/TTS 播放，重置到 IDLE。

### 3. `switch_model` — 切换模型

```json
{ "type": "switch_model", "service": "llm", "model": "openai/gpt-4o" }
```

`service` 取值: `"llm"` | `"stt"` | `"tts"`。后端异步返回 `switch_model_result`。

### 4. `tool_approval` — 审批回复

```json
{ "type": "tool_approval", "request_id": "msg_abc123", "approved": true }
```

回复 `tool_approval` command。`approved` 为 `false` 则拒绝操作。

### 5. `wake_toggle` — 开启/关闭语音唤醒

```json
{ "type": "wake_toggle", "enabled": true }
```

---

## HTTP REST API

Base URL: `http://{host}:{port}`

### 健康检查

```
GET /health
GET /ready
```

响应:
```json
{
  "status": "ok",
  "fsm": "idle",
  "llm": "deepseek/deepseek-v4-flash",
  "memory": { "session_count": 22, "long_term_count": 45 },
  "tools": ["web_search", "read_file", "write_file", ...]
}
```

### 图片上传

```
POST /api/images
Content-Type: multipart/form-data
  file: <image_file>  (字段名必须是 "file")
```

**约束：**
- 允许的 MIME: `image/png`, `image/jpeg`, `image/gif`, `image/webp`
- 大小上限: 10 MB（可通过 `config.yaml` 的 `server.image_max_mb` 调整）
- 超过上限返回 `{"ok": false, "error": "图片过大 (X MB)，最大 Y MB"}`

响应（成功）:
```json
{ "ok": true, "path": "/uploads/img_abc123.png" }
```

上传成功后，在 `text` 消息的 `content` 数组中通过 `"source": "/uploads/img_abc123.png"` 引用。

### 纯文本测试

```
POST /api/chat
Content-Type: application/json
```

请求:
```json
{ "content": [{ "type": "text", "content": "你好" }], "user_name": "妖梦" }
```

响应（异步）:
```json
{ "ok": true }
```

注意：此端点不等待 LLM 回复，仅提交消息到管道。

### Provider 管理

**列表：**
```
GET /api/providers
```
响应:
```json
{
  "llm": [
    { "name": "deepseek/deepseek-v4-flash", "base_url": "https://api.deepseek.com/v1", "env_key": "DEEPSEEK_API_KEY" }
  ],
  "stt": [...],
  "tts": [...]
}
```

**新增自定义 provider：**
```
POST /api/providers
Content-Type: application/json
```
请求:
```json
{
  "service": "llm",
  "entry": { "name": "my-llm", "base_url": "http://...", "api_key": "sk-..." }
}
```
响应:
```json
{ "ok": true, "data": { "name": "my-llm", ... } }
```

**删除：**
```
DELETE /api/providers/{service}/{name}
```
响应:
```json
{ "ok": true, "data": [...] }
```

**连通性测试：**
```
POST /api/providers/test
Content-Type: application/json
```
请求:
```json
{ "service": "llm", "base_url": "http://...", "api_key": "sk-...", "model": "gpt-4" }
```
响应:
```json
{ "ok": true }
```

### 控制端点

```
POST /shutdown       → {"status": "shutting_down"}
GET  /shutdown       → 同上（浏览器直接访问）
POST /restart        → {"status": "restarting"}（仅 POST，不支持 GET）
GET  /tools          → {"plugins": [{ "name": "web_search", "description": "...", "safety": {...} }, ...]}
POST /tools/reload   → 请求: {"name": "web_search"} 响应: {"ok": true, "name": "web_search"}
```

`/shutdown` 接受 GET 和 POST。`/restart` 仅接受 POST。

### Debug API（开发用）

```
GET  /debug                  → debug 页面 HTML
GET  /debug/api              → Agent 完整内部状态 JSON
GET  /debug/api/tasks        → TaskSystem 任务快照
GET  /debug/api/tasks/stream → TaskSystem SSE 实时推送
GET  /debug/api/db-status    → MongoDB/Milvus 连接状态
GET  /debug/api/ltm          → 长期记忆条目列表
GET  /debug/api/sessions     → Session 列表
GET  /debug/api/prompts      → Prompt 历史记录
```

---

## .NET / WPF 接入要点

1. **WebSocket 客户端** — 使用 `System.Net.WebSockets.ClientWebSocket`，连接 `/ws`
2. **JSON 序列化** — 使用 `System.Text.Json`，收到消息先读 `type` 字段路由
3. **流式文字** —
   - 检测 `capabilities.tts`：true → TTS 模式（delta 带 stream_id，完整句）；false → 纯文字模式（逐字 delta）
   - TTS 模式：每条 delta 创建新气泡，stream_id 关联音频流
   - 纯文字模式：逐字追加到气泡，遇到 `message { content: "" }` 归档当前气泡
4. **音频播放** —
   - `audio` 的 `data` 是 base64 WAV，decode 后用 NAudio 等库播放
   - 按 `stream_id` 分组，按 `seq` 排序，收到 `audio_end` 才播下一句
5. **Live2D / 表情** — 非必须，忽略 `expression`/`action` 消息即可
6. **工具卡片** — 收到 `action.action === "tool_executing"` 创建执行中卡片，收到 `action.action === "tool_done"` 更新为完成/失败。`call_id` 关联两张消息
7. **结构化内容** — 收到 `command.command === "block"` 独立渲染代码块（等宽字体+语法高亮）或 Markdown 块（富文本），不混入流式文字
8. **图片上传** — 先 POST `/api/images` 拿到 `path`，再通过 WS 发送含 `source` 的多模态 `text` 消息
9. **审批弹窗** — 收到 `type: "command", command: "tool_approval"` 时弹出确认框，用户选择后发回 `tool_approval` 消息

### 最小接入代码（伪 C#）

```csharp
var ws = new ClientWebSocket();
await ws.ConnectAsync(new Uri("ws://localhost:8765/ws"), CancellationToken.None);

// 收消息
var buffer = new byte[65536];
while (ws.State == WebSocketState.Open) {
    var result = await ws.ReceiveAsync(buffer, CancellationToken.None);
    var json = Encoding.UTF8.GetString(buffer, 0, result.Count);
    var msg = JsonSerializer.Deserialize<Dictionary<string, object>>(json);
    var type = msg["type"].ToString();
    
    switch (type) {
        case "session":   /* 恢复历史 */ break;
        case "delta":
            var sid = msg.TryGet("stream_id", out var s) ? (int)s : (int?)null;
            if (sid != null) {
                // TTS 模式：完整句子，创建新气泡
                AppendBubble(sid.Value, (string)msg["content"]);
            } else {
                // 纯文字模式：逐字追加到当前气泡
                AppendToCurrentBubble((string)msg["content"]);
            }
            break;
        case "message":
            if (string.IsNullOrEmpty((string)msg["content"])) {
                FinalizeCurrentBubble();  // 句边界
            }
            break;
        case "state":     /* 更新状态 */ break;
        case "audio":     /* 按 stream_id 缓冲，seq 排序播放 */ break;
        case "audio_end": /* 标记 stream_id 结束，可播下一句 */ break;
        case "action":
            if ((string)msg["action"] == "tool_executing") {
                AddToolCard(msg["params"]);   // call_id 做 key，执行中动画
            } else if ((string)msg["action"] == "tool_done") {
                UpdateToolCard(msg["params"]); // call_id 匹配，显示结果
            }
            break;
        case "command":
            if ((string)msg["command"] == "tool_approval") {
                ShowApprovalDialog(msg["params"]);
            } else if ((string)msg["command"] == "block") {
                RenderBlock(msg["params"]);    // code → 语法高亮, markdown → 富文本
            }
            break;
        // ...
    }
}

// 发消息
await ws.SendAsync(Encoding.UTF8.GetBytes(
    "{\"type\":\"text\",\"content\":\"你好\"}"), 
    WebSocketMessageType.Text, true, CancellationToken.None);
```
