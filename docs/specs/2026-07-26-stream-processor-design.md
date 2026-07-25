# StreamProcessor — CognitiveLoop 流式解析提取

> 日期: 2026-07-26 | 状态: design | 作者: yukikaze-1

从 `CognitiveLoop._think_loop()` 中提取流式标签检测 + 文本管道的自包含组件。

## 动机

`core/cognitive/loop.py` 是项目最大文件（952 行）。其中 `_think_loop` 方法 560 行（L286–845），包含 3 个交织的内联状态机：

1. **mood 标签检测** — MOOD_PRE → MOOD → DONE，约 90 行
2. **inner_voice 标签检测** — PRE → THINK → REPLY，约 140 行
3. **句子缓冲 + 块过滤** — SentenceBuffer + BlockFilter 管道

这三个状态机彼此正交（mood 剥离 → inner_voice 分离 → 块过滤 → 断句），可以打包为独立组件，纯状态机逻辑零副作用。

## 目录结构

```
core/cognitive/
├── stream_processor.py   ← 新建 — StreamProcessor + StreamEvent + StreamEventType
├── loop.py               ← 修改 — _think_loop 内联状态机替换为 StreamProcessor 调用
├── tools.py              — 不变
├── timing.py             — 不变
├── __init__.py            ← 修改 — 导出 StreamProcessor, StreamEvent, StreamEventType
├── README.md              ← 修改 — 更新文件布局、数据流图、关键机制说明（详见下文）
└── tests/
    ├── test_timing.py      — 不变
    └── test_stream_processor.py  ← 新建 — 纯状态机单元测试
```

CLAUDE.md 需同步更新 `core/cognitive/` 目录树。

## 设计

### StreamEvent — 内部管道事件

`StreamEvent` 是 StreamProcessor 内部的管道事件，**不是** `core.schema.EventData`（event bus 消息）。它只服务于文本处理管道，不走 `EventBus`。

```python
class StreamEventType(StrEnum):
    MOOD = "mood"              # <mood>...</mood> 检测完毕
    INNER_VOICE = "inner_voice"  # <inner_voice>...</inner_voice> 检测完毕
    SENTENCE = "sentence"      # SentenceBuffer 产出一个完整句子
    TEXT_DELTA = "text_delta"  # 可安全显示/播放的纯文本增量
    TTFT = "ttft"              # 首个 visible token 到达（post-mood, post-inner_voice）

@dataclass(frozen=True)
class StreamEvent:
    type: StreamEventType
    text: str = ""
```

`StreamEventType` 定义在 `core/cognitive/stream_processor.py` 中，不放入 `core/schema/`——它是 cognitive 子系统的内部实现细节，不是跨模块的共享类型。

### 公共 API

```python
class StreamProcessor:
    def __init__(self, *, min_chunk_length: int = 5,
                 max_buffer_length: int = 2000): ...
    def feed(self, delta: str) -> list[StreamEvent]: ...
    def flush(self) -> list[StreamEvent]: ...
    def reset(self) -> None: ...
```

- `feed(delta)` — 输入流式文本片段，返回零到多个事件。有状态，内部累积。返回 `list[StreamEvent]` 而非 `str` 是刻意设计：和 `BlockFilter.feed() → str`、`SentenceBuffer.feed() → str | None` 不同，StreamProcessor 是管道的最上层，需要输出多种类型的事件而不仅仅是文本。
- `flush()` — 流结束时调用：吐出尾句、丢弃未闭合块、残留标签丢弃。
- `reset()` — 重置所有内部状态（内部调用 `SentenceBuffer.reset()` + `BlockFilter.reset()`，每轮对话开始时调用）。

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

### 跨 chunk 缓冲策略

这是整个状态机中最容易出错的细节。标签可能被 chunk 边界截断（如 `<moo` 在一个 chunk 结尾，`d>` 在下一个 chunk 开头），StreamProcessor 必须正确处理所有四种场景。

**1. 标签打开检测（MOOD_PRE / PRE 状态）**

对于可能跨 chunk 的起始标签 `<mood>` 或 `<inner_voice>`，采用 `safe_len` 动态转发策略：

```python
# combined = 跨 chunk 累积的尾部 + 当前 delta
# 只转发"不可能构成标签起始符"的部分，保留尾部用于跨 chunk 匹配
safe_len = max(0, len(combined) - len(TAG_OPEN) + 1)
```

其中 `TAG_OPEN = "<mood>"` 或 `"<inner_voice>"`。保留 `len(TAG_OPEN) - 1` 个尾部字符，防止 `<` 被过早泄漏。如果转发部分的第一个非空白字符不是 `<`，说明这个片段不可能以标签开头，可以安全地全部转发（快速路径）。

**2. 标签内容缓冲（MOOD / THINK 状态）**

进入标签内部后，采用 `keep_len` 尾部保留策略：

```python
# 保留末尾 n-1 个字符，用于跨 chunk 闭合标签检测
keep_len = min(len(CLOSE_TAG) - 1, len(combined))
mood_buf += combined[:-keep_len]   # 安全部分加入缓冲区
tail = combined[-keep_len:]         # 保留尾部
```

其中 `CLOSE_TAG = "</mood>"` 或 `"</inner_voice>"`。保留 `len(CLOSE_TAG) - 1`（= 6）个尾部字符，确保 `</mood` 等半截闭合标签能被下一个 chunk 补全。

**3. 无标签的快速路径**

如果 MOOD_PRE 状态中第一个非空白字符不是 `<`，设置 `mood_state = DONE`，后续所有 delta 跳过 mood 检测。同样，如果 `<mood>` 前有非空白文本（如 `hello <mood>happy</mood>`），mood 检测在 `hello ` 的第一个非空白 `h` 处被判定为 DONE——不输出假阳性的 mood 标签。

**4. 标签完全缺失**

如果整个流中从未出现 `<mood>` 或 `<inner_voice>`：
- `iv_state == "PRE"` → `flush()` 把 `iv_tail` 残留全部作为 TEXT_DELTA 输出
- `mood_state == "MOOD_PRE"` → `flush()` 把 `mood_tail` 残留全部作为 TEXT_DELTA 输出
- 流后的 mood fallback（关键词检测）照常运行

### `_dispatch_stream_event()` 集成点

`_dispatch_stream_event()` 是 `CognitiveLoop` 新增的私有方法，负责把纯 StreamEvent 转换为副作用操作。它本质上是 StreamProcessor（纯状态机）和 CognitiveLoop（有副作用的世界）之间的桥：

```python
async def _dispatch_stream_event(self, event: StreamEvent, *,
    speaking_started: bool, all_sentences: list[str],
    tts_mgr: TTSStreamManager, block_filter: BlockFilter,
) -> tuple[bool, list[str]]:
    """分发 StreamEvent → 副作用。返回更新后的 (speaking_started, all_sentences)。"""
    if event.type == StreamEventType.MOOD:
        _apply_mood(event.text, self._character_engine, tts_mgr)
        self._mood_applied = True          # 设置标志供流后 fallback 使用
    elif event.type == StreamEventType.INNER_VOICE:
        await self._output.command("inner_voice", params={"content": event.text})
    elif event.type == StreamEventType.TTFT:
        # TTFT 时钟在循环外记录；Event 本身是信号
        ...
    elif event.type == StreamEventType.SENTENCE:
        # 句子分发：TTS stream 或直接文本输出 + FSM SPEAKING 切换
        ...
    elif event.type == StreamEventType.TEXT_DELTA:
        # 直接广播文本增量到前端（TTS 关闭时）
        ...
    return speaking_started, all_sentences
```

**mood_applied 标志**：`_dispatch_stream_event` 在收到 MOOD 事件时设置 `self._mood_applied = True`。流结束后，`_think_loop` 的 fallback 逻辑读取这个标志：

```python
# 流后 fallback: LLM 未输出 <mood> → 关键词检测
if self._character_engine and not self._mood_applied:
    self._character_engine.analyze_user(user_message)
    self._tts_mgr.set_emotion(self._character_engine.state.mood)
```

这使得 StreamProcessor 不需要知道 mood 的语义——它只负责标签检测，`_dispatch_stream_event` 负责把检测结果翻译成标志位。

### `_think_loop` 使用方式

```python
proc = StreamProcessor()

async for chunk in stream:
    delta = chunk.get("content", "")
    for event in proc.feed(delta):
        speaking_started, all_sentences = await self._dispatch_stream_event(
            event, speaking_started=speaking_started, ...)

    # tool_call 合并不进 StreamProcessor
    if chunk.get("tool_calls"):
        merge_stream_tool_calls(raw_tool_calls, chunk["tool_calls"])

for event in proc.flush():
    speaking_started, all_sentences = await self._dispatch_stream_event(
        event, speaking_started=speaking_started, ...)
```

### 不在首期范围内的

以下逻辑留在 `_think_loop`，不进入 StreamProcessor：

- **tool_call 合并** — `merge_stream_tool_calls(raw_tool_calls, tc_deltas)`，与文本处理正交
- **token usage** — `chunk["usage"]` 提取 + `TokenTracker.record()`，LLM 协议层
- **`_apply_mood()`** — mood 文本 → CharacterState + TTS 情绪参数，需要 CharacterEngine + TTSStreamManager
- **句子分发** — TTS stream_sentence / 直接 text_delta 广播 / FSM SPEAKING 切换 / 输出通道选择
- **流后 mood fallback** — `_mood_applied` 为 False 时触发关键词情绪检测 (`CharacterEngine.analyze_user`) + TTS set_emotion
- **流后 public_text 剥离** — 从 `acc_text` 中 post-hoc 剥离 `<mood>...</mood>` 和 `<inner_voice>...</inner_voice>` 标签得到 `public_text`，用于 `AssistantMessage` 存入 session 和 `ResponseRecorder.record()`。注意：这和流中的标签检测是两件事——流中是实时检测，流后是完整文本清洗

### 错误处理

StreamProcessor 不执行 I/O，无网络错误。不抛异常——所有输入（包括空字符串、畸形数据）都有明确定义的行为。

## 测试策略

StreamProcessor 是纯状态机，所有测试都是 `input → assert output`，零 mock。关键测试用例：

| 用例 | 输入 | 预期事件 |
|------|------|----------|
| 空字符串 | `feed("")` | `[]` |
| 纯文本无标签 | `feed("hello world。")` | `TEXT_DELTA("hello")` + `SENTENCE("hello world。")` |
| 完整 mood 标签 | `feed("<mood>happy</mood>hi")` | `MOOD("happy")` + `TEXT_DELTA("hi")` |
| 完整 inner_voice 标签 | `feed("<inner_voice>think</inner_voice>reply")` | `INNER_VOICE("think")` + `TEXT_DELTA("reply")` |
| mood 跨 chunk 分割 | `feed("<mo")` → `feed("od>happy</mood>hi")` | `MOOD("happy")` + `TEXT_DELTA("hi")` |
| mood_close 跨 chunk 分割 | `feed("<mood>hap")` → `feed("py</mood>hi")` | `MOOD("happy")` + `TEXT_DELTA("hi")` |
| inner_voice 跨 chunk 分割 | `feed("<inner_v")` → `feed("oice>think</inner_voice>hi")` | `INNER_VOICE("think")` + `TEXT_DELTA("hi")` |
| inner_voice_close 跨 chunk 分割 | `feed("<inner_voice>th")` → `feed("ink</inner_voice>hi")` | `INNER_VOICE("think")` + `TEXT_DELTA("hi")` |
| 两个标签同一 chunk | `feed("<mood>happy</mood><inner_voice>hmm</inner_voice>hi")` | `MOOD` + `INNER_VOICE` + `TEXT_DELTA("hi")` |
| mood 前有非空白文本 | `feed("hello <mood>happy</mood>")` | `TEXT_DELTA("hello ")` — mood 检测被 DONE 跳过 |
| 未闭合 mood | `feed("<mood>happy")` → `flush()` | `MOOD` 事件在闭合时才触发 → mood 从未闭合 → flush 丢弃残留，不输出 |
| 未闭合 inner_voice | `feed("<inner_voice>think")` → `flush()` | `INNER_VOICE` 事件在闭合时才触发 → flush 丢弃残留 |
| `<mood>` 前有空白 | `feed("  <mood>happy</mood>hi")` | `MOOD("happy")` + `TEXT_DELTA("hi")` — 前导空白不影响标签检测 |
| flush 尾句 | `feed("no period")` → `flush()` | `feed` 只有 TEXT_DELTA → `flush` 吐出 `SENTENCE("no period")` |
| 代码块过滤 | `feed("hi <|code|python>print(1)</|code|> bye。")` | `TEXT_DELTA("hi ")` + `SENTENCE("hi  bye。")` — 代码块内容被吞掉 |
| MOOD_PRE 安全转发 | `feed("a" * 100 + "<m")` → `feed("ood>happy</mood>")` | 前 100 个 `a` 全部作为 TEXT_DELTA 转发（安全范围外） |

## 约束

- StreamProcessor 是纯状态机，零 I/O、零外部依赖（不 `import core.schema`、不 `import Module`）
- `feed()` 对同一输入产生确定性输出
- 保持与现有 mood/inner_voice/sentence 行为 100% 兼容
- `CognitiveLoop.process()` 公共契约 — 不变
- 新增 README 内容需遵循 `docs/readme-conventions.md`

## README 更新范围 (`core/cognitive/README.md`)

1. **文件布局树形图** — 新增 `stream_processor.py`，标注职责"流式标签检测 + 文本管道"
2. **数据流图** — 在 LLM stream 和 CognitiveLoop 之间插入 StreamProcessor 层：`LLM stream → StreamProcessor (mood/inner_voice/sentence) → _dispatch_stream_event() → TTS/output`
3. **关键机制** — 将现有 inner_voice 解析说明拆分：(a) 标签检测 → 移至 StreamProcessor 小节，(b) 广播逻辑 → 保留在 CognitiveLoop 小节，链接到 StreamProcessor

## 预期效果

| 指标 | 当前 | 预期 |
|------|------|------|
| `loop.py` 行数 | 952 | ~750 |
| `_think_loop` 行数 | 560 | ~310 |
| StreamProcessor 行数 | — | ~350（含跨 chunk 缓冲逻辑） |
| StreamProcessor 测试 | 需要启动完整 Agent | 纯函数 input → assert output，16+ 用例 |
