# Output Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

统一的输出模块。管理多个 `OutputChannel`（控制台、WebSocket、未来 Discord 等），支持流式文本、表情、动作、指令等多维度输出。

## 文件结构

```
output/
├── __init__.py          # 导出
├── __main__.py          # 集成测试 (python -m Module.output)
├── protocol.py          # ChannelCapability + OutputChannel ABC
├── manager.py           # OutputManager — 多 channel 广播 + 流式协调
├── console.py           # ConsoleChannel — 终端输出
├── websocket.py         # WebSocketChannel — JSON 消息
├── tests/               # 单元测试
└── README.md
```

## 快速开始

```python
from Module.output import OutputManager, ConsoleChannel, WebSocketChannel

mgr = OutputManager()
mgr.add_channel(ConsoleChannel())
mgr.add_channel(WebSocketChannel(ws))

# 流式输出
async def llm_stream():
    for token in ["你", "好", "，", "世", "界"]:
        yield token

await mgr.typing(True)
await mgr.stream(llm_stream(), finalize=lambda text: ChatMessage(...))
await mgr.typing(False)

# 表情 + 动作
await mgr.expression("happy", 0.8)
await mgr.action("wave")

# 非流式
await mgr.send(ChatMessage(role="Elysia", content="你好"))
```

## 接口

### ChannelCapability

通道能力标记。每个 channel 声明自己支持哪些输出维度：

| Capability | 含义 |
|------------|------|
| `TEXT_DELTA` | 流式 token 推送 |
| `TEXT_MESSAGE` | 完整消息 |
| `TYPING` | 输入状态指示 |
| `EXPRESSION` | 表情/情绪变更 |
| `ACTION` | 动作变更 |
| `AUDIO` | 音频流（预留） |
| `COMMAND` | 前端指令 |

### OutputChannel ABC

所有方法默认 no-op，channel 按需 override。`protocol_version` 类属性（1=v1 默认，2=v2，握手时由 `WebSocketChannel` 设置）——批次 2 版本感知扇出据此分发。

| 方法 | 说明 |
|------|------|
| `on_text_delta(delta)` | 流式文本 token |
| `on_message(msg)` | 完整文本消息（abstract，必须实现） |
| `on_typing(typing)` | 输入状态指示 |
| `on_expression(expr, intensity)` | 表情/情绪变更 |
| `on_action(action)` | 动作变更 |
| `on_command(cmd)` | 前端指令 |
| `on_audio_chunk(chunk, stream_id)` | 音频流片段 |
| `on_audio_end(stream_id, format)` | 音频流结束 |
| `on_audio_start(stream_id, *, format, ...)` | 音频流开始（BE-063，默认 no-op） |
| `on_audio_cancelled(stream_id)` | 音频流取消（BE-063，默认 no-op） |
| `on_state(state)` | 状态变更通知 |
| `on_token_usage(data)` | Token 用量统计 |
| `on_user_message_ack(...)` | v2 用户消息确认（默认 no-op） |
| `on_generation_started(...)` | v2 生成开始（默认 no-op） |
| `on_generation_segment_started(...)` | v2 分段开始（默认 no-op） |
| `on_generation_delta(...)` | v2 分段增量（默认 no-op） |
| `on_generation_segment_completed(...)` | v2 分段完成（默认 no-op） |
| `on_generation_completed(...)` | v2 父完成（默认 no-op） |
| `on_generation_failed(...)` | v2 父失败（默认 no-op） |

v2 方法均带 `conversation_id`/`conversation_usage` 关键字参数，v2 通道按需实现，其余通道默认 no-op。详见下文「批次 2 v2 版本感知统一回合事件」。

### OutputManager

- `add_channel(ch)` / `remove_channel(ch)` — 管理通道
- `_broadcast(method, ...)` / `_filter(channels, capability)` — 能力过滤广播

#### 批次 2 v2 版本感知统一回合事件

`OutputManager` 提供统一回合事件方法，按 `channel.protocol_version` 分发（单一语义源、双 wire 呈现）：

| 方法 | v1 通道 | v2 通道 |
|---|---|---|
| `turn_started(ctx)` | 无新增 | `generation.started` |
| `segment_started(ctx)` | 无 | `generation.segment.started` |
| `text_delta(text, ctx)` | `delta` | `generation.delta` |
| `segment_completed(text, ctx, tts_active)` | TTS 关→`message:""` | `generation.segment.completed`（无活动段不发） |
| `inner_voice(text, ctx)` | `command inner_voice` | inner_voice 段 |
| `mood(expr, intensity)` | `expression` | 捕获不发 |
| `turn_completed(...)` | NO-OP（token_usage 已实时广播） | `generation.completed` |
| `turn_failed(code, msg, retryable, label, ctx)` | `❌` delta + `message:""` | `generation.failed` |
| `turn_cancelled(ctx)` | 不发 | `generation.failed{CANCELLED}` |

`token_usage(data)` 对 v2 抑制（usage 由 `generation.completed.usage` 承载）。详见 [docs/electron-backend-protocol-v2.md](../../docs/electron-backend-protocol-v2.md)。
- `stream(deltas, finalize, *, channels, capability)` — 流式，广播 delta + 最终消息
- `send(msg, *, channels, capability)` — 非流式完整消息
- `typing(typing, *, ...)` — 便捷方法，自动路由到 TYPING channel
- `expression(expr, intensity, *, ...)` / `action(action, *, ...)` / `command(cmd, *, ...)` — 便捷方法
- `audio_chunk(chunk, stream_id, *, ...)` — 音频流片段广播
- `audio_end(stream_id, format, *, ...)` — 音频流结束通知
- `notify_state(state, *, ...)` — 状态变更广播
- `token_usage(data, *, ...)` — Token 用量通知
- `close()` — 关闭所有通道

所有方法支持两种过滤方式：
- `channels=[...]` — 精确指定目标
- `capability=ChannelCapability.XXX` — 声明式过滤
- 不传 = 广播全部

## Channel 实现

| Channel | TEXT_DELTA | TYPING | EXPRESSION | ACTION | COMMAND | AUDIO |
|---------|:---:|:---:|:---:|:---:|:---:|:---:|
| ConsoleChannel | 是 | - | - | - | - | - |
| WebSocketChannel | 是 | 是 | 是 | 是 | 是 | 预留 |

## 测试

```bash
python -m pytest Module/output/tests/ -v
python -m Module.output
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [core/input/README.md](../../core/input/README.md) | WebSocket 服务器（Output 通道载体） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 输出系统 |
