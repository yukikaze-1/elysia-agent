# StreamProcessor — CognitiveLoop 流式解析提取

> 从 `CognitiveLoop._think_loop()` 中提取流式标签检测 + 文本管道的自包含组件。

## 动机

`core/cognitive/loop.py` 是项目最大文件（952 行）。其中 `_think_loop` 方法约 400 行，包含 3 个交织的内联状态机：

1. **mood 标签检测** — MOOD_PRE → MOOD → DONE，约 90 行
2. **inner_voice 标签检测** — PRE → THINK → REPLY，约 140 行
3. **句子缓冲 + 块过滤** — SentenceBuffer + BlockFilter 管道

这三个状态机彼此正交（mood 剥离 → inner_voice 分离 → 块过滤 → 断句），可以打包为独立组件，纯状态机逻辑零副作用。

## 设计

### 处理管道

```
delta text ──▶ ┌─────────────────────────────────────────────┐
               │          StreamProcessor                     │
               │                                              │
               │  mood 剥离 ──▶ MOOD 事件                      │
               │       ↓                                      │
               │  inner_voice 分离 ──▶ INNER_VOICE 事件        │
               │       ↓                                      │
               │  BlockFilter ──▶ 过滤 code/markdown           │
               │       ↓                                      │
               │  SentenceBuffer ──▶ SENTENCE 事件             │
               │       ↓                                      │
               │  纯文本 ──▶ TEXT_DELTA 事件                   │
               │       ↓                                      │
               │  首个 token ──▶ TTFT 事件                     │
               └─────────────────────────────────────────────┘
```

### 事件类型

```python
class StreamEventType(StrEnum):
    MOOD = "mood"              # <mood>...</mood> 检测完毕
    INNER_VOICE = "inner_voice"  # <inner_voice>...</inner_voice> 检测完毕
    SENTENCE = "sentence"      # SentenceBuffer 产出一个完整句子
    TEXT_DELTA = "text_delta"  # 可安全显示/播放的纯文本增量
    TTFT = "ttft"              # 首个有效 token 到达

@dataclass(frozen=True)
class StreamEvent:
    type: StreamEventType
    text: str = ""
```

### 公共 API

```python
class StreamProcessor:
    def __init__(self, *, min_chunk_length: int = 5,
                 max_buffer_length: int = 2000): ...
    def feed(self, delta: str) -> list[StreamEvent]: ...
    def flush(self) -> list[StreamEvent]: ...
    def reset(self) -> None: ...
```

- `feed(delta)` — 输入流式文本片段，返回零到多个事件。有状态，内部累积。
- `flush()` — 流结束时调用：吐出尾句、丢弃未闭合块、残留标签丢弃。
- `reset()` — 重置所有内部状态（每轮对话开始时调用）。

### `_think_loop` 使用方式

```python
proc = StreamProcessor()

async for chunk in stream:
    delta = chunk.get("content", "")
    for event in proc.feed(delta):
        await self._dispatch_stream_event(event, ...)

    # tool_call 合并不进 StreamProcessor
    if chunk.get("tool_calls"):
        merge_stream_tool_calls(raw_tool_calls, chunk["tool_calls"])

for event in proc.flush():
    await self._dispatch_stream_event(event, ...)
```

副作用分发（mood 更新、inner_voice 广播、句子 TTS/文本输出、FSM 状态切换）由 `_think_loop` 中的 `_dispatch_stream_event()` 方法完成。

### 不进 StreamProcessor 的部分

- **tool_call 合并** (`merge_stream_tool_calls`) — 与文本处理正交
- **token usage** — LLM 协议层，与文本处理无关
- **`_apply_mood()`** — 需要 CharacterEngine + TTSStreamManager
- **句子分发** — 涉及 TTS/直接输出/FSM 状态切换/输出通道选择
- **流后 fallback** — mood fallback 关键词检测、acc_text 标签剥离

### 错误处理

StreamProcessor 不执行 I/O，无网络错误。不抛异常——所有输入（包括空字符串、畸形数据）都有明确定义的行为。

## 文件清单

| 文件 | 动作 |
|------|------|
| `core/cognitive/stream_processor.py` | **新建** — StreamProcessor + StreamEvent |
| `core/cognitive/loop.py` | **修改** — `_think_loop` 内联状态机替换为 StreamProcessor 调用 |
| `core/cognitive/__init__.py` | **修改** — 导出 StreamProcessor, StreamEvent, StreamEventType |
| `core/cognitive/tests/test_stream_processor.py` | **新建** — 单元测试（纯状态机，无 mock） |
| `core/cognitive/README.md` | **修改** — 更新文件布局 + 组件说明 |
| `CLAUDE.md` | **修改** — 更新目录树中 cognitive/ 的文件列表 |

## 保持不变

- `core/text/sentence_buffer.py` — 无变更
- `core/text/block_filter.py` — 无变更
- `core/adapter/tts_adapter.py` — 无变更
- `Module/` — 零变更
- 所有现有测试 — 零破坏
- `CognitiveLoop.process()` 公共契约 — 不变

## 约束

- StreamProcessor 是纯状态机，零 I/O、零外部依赖（不 `import core.schema`、不 `import Module`）
- `feed()` 对同一输入产生确定性输出
- 保持与现有 mood/inner_voice/sentence 行为 100% 兼容
- `_think_loop` 的公共契约（`process()` 的输入输出）不变

## 预期效果

| 指标 | 当前 | 预期 |
|------|------|------|
| `loop.py` 行数 | 952 | ~750 |
| `_think_loop` 行数 | ~400 | ~250 |
| StreamProcessor 测试 | 需要启动完整 Agent | 纯函数 input → assert output |
