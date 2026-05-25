# Output Module

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
├── test_output.py       # 单元测试
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

所有方法默认 no-op，channel 按需 override。

### OutputManager

- `add_channel(ch)` / `remove_channel(ch)` — 管理通道
- `stream(deltas, finalize, *, channels, capability)` — 流式，广播 delta + 最终消息
- `send(msg, *, channels, capability)` — 非流式完整消息
- `typing(typing, *, ...)` — 便捷方法，自动路由到 TYPING channel
- `expression(expr, intensity, *, ...)` / `action(action, *, ...)` / `command(cmd, *, ...)` — 便捷方法

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
python -m pytest Module/output/test_output.py -v
python -m Module.output
```
