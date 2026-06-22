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
Server ──→ Client (15 种):
  session, capabilities, state
  delta, message, typing
  expression, action, command
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

前端可用此状态显示加载动画、禁用输入框等。

### 4. `delta` — 流式文本增量

```json
{ "type": "delta", "content": "你好" }
```

每次推送一个或多个字符。前端应逐字追加到当前 AI 气泡中。

可选字段 `stream_id`（int）用于多流场景。

### 5. `message` — 完整消息

非流式场景推送完整消息（系统通知、历史重放等）：

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

用于驱动 Live2D 或静态图的表情切换。

### 8. `action` — 角色动作

```json
{
  "type": "action",
  "action": "wave",
  "params": { "duration": 2.0 }
}
```

用于 Live2D 动作触发。`params` 可选。

### 9. `command` — 指令

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

当前唯一的 `command` 是 `tool_approval`——需要用户确认的危险操作。前端应弹出确认对话框，用户选 approve/deny 后通过 `tool_approval` 消息回复。

### 10. `audio` + `audio_end` — TTS 音频流

```json
{ "type": "audio", "data": "<base64>", "stream_id": 0, "seq": 0, "is_final": false }
...
{ "type": "audio_end", "stream_id": 0 }
```

- `data`：base64 编码的 **WAV** 音频块（含 RIFF 头），MIME: `audio/wav`
- `stream_id`：区分并发音频流（通常为 0）
- `seq`：递增序号，用于排序和去重
- `is_final`：始终为 `false`，音频结束由 `audio_end` 标记

**音频格式：** WAV, 32000Hz 或 24000Hz（取决于 TTS 后端）, mono, int16。第一个 `audio` 消息的 WAV 头已包含完整格式信息，客户端从中读取采样率即可。解码后用 NAudio（.NET）/ Web Audio API（Web）/ AVAudioEngine（macOS）播放。

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
POST /restart        → {"status": "restarting"}
GET  /tools          → {"plugins": [{ "name": "web_search", "description": "...", "safety": {...} }, ...]}
POST /tools/reload   → 请求: {"name": "web_search"} 响应: {"ok": true, "name": "web_search"}
```

`/shutdown` 和 `/restart` 均接受 GET（浏览器直接访问）和 POST。

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
3. **音频** — `audio` 消息的 `data` 是 base64，decode 后用 NAudio 等库播放
4. **Live2D / 表情** — 非必须，忽略 `expression`/`action` 消息即可
5. **图片上传** — 先 POST `/api/images` 拿到 `path`，再通过 WS 发送含 `source` 的多模态 `text` 消息
6. **审批弹窗** — 收到 `type: "command", command: "tool_approval"` 时弹出确认框，用户选择后发回 `tool_approval` 消息

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
        case "delta":     /* 追加文本 */ break;
        case "state":     /* 更新状态 */ break;
        case "audio":     /* 播放音频 */ break;
        case "command":   /* 处理审批 */ break;
        // ...
    }
}

// 发消息
await ws.SendAsync(Encoding.UTF8.GetBytes(
    "{\"type\":\"text\",\"content\":\"你好\"}"), 
    WebSocketMessageType.Text, true, CancellationToken.None);
```
