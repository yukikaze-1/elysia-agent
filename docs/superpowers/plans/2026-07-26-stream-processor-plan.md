# StreamProcessor — CognitiveLoop 流式解析提取 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从 `CognitiveLoop._think_loop()` 中提取 mood/inner_voice/sentence 流式解析状态机为独立的 `StreamProcessor` 组件

**Architecture:** 纯状态机模式 — `StreamProcessor.feed(delta) → list[StreamEvent]`，内部持有 `BlockFilter` + `SentenceBuffer`，管道顺序: mood 剥离 → inner_voice 分离 → 块过滤 → 断句。副作用完全由 `CognitiveLoop._dispatch_stream_event()` 分发。

**Tech Stack:** Python 3.11+, Pydantic（无需，只用 dataclass）, pytest

## Global Constraints

- StreamProcessor 是纯状态机，零 I/O、零外部依赖（不 `import core.schema`、不 `import Module`）
- `feed()` 对同一输入产生确定性输出
- 保持与现有 mood/inner_voice/sentence 行为 100% 兼容
- `CognitiveLoop.process()` 公共契约 — 不变
- 新增 README 内容需遵循 `docs/readme-conventions.md`
- 测试用 `pytest -m "unit or service"` 运行
- 使用 conda 环境: `conda activate Elysia`

---

### Task 1: StreamProcessor 核心类 + 17 个单元测试 (TDD)

**Files:**
- Create: `core/cognitive/stream_processor.py`
- Create: `core/cognitive/tests/test_stream_processor.py`

**Interfaces:**
- Produces: `StreamEventType` (StrEnum), `StreamEvent` (frozen dataclass), `StreamProcessor` (class with `feed`, `flush`, `reset`)

**Spec reference:** 测试策略表格 17 个用例 + 跨 chunk 缓冲策略 5 种场景

- [ ] **Step 1: 创建测试文件并写第一批失败测试（基础标签检测）**

```python
# core/cognitive/tests/test_stream_processor.py
"""StreamProcessor 单元测试 — 纯状态机，零 mock。"""
import pytest
from core.cognitive.stream_processor import (
    StreamProcessor,
    StreamEventType,
    StreamEvent,
)


class TestBasicText:
    """基础文本处理 — 无标签。"""

    def test_empty_string(self):
        proc = StreamProcessor()
        assert proc.feed("") == []
        assert proc.flush() == []

    def test_plain_text_with_period(self):
        proc = StreamProcessor()
        events = proc.feed("hello world。")
        assert len(events) >= 2
        types = [e.type for e in events]
        assert StreamEventType.TEXT_DELTA in types
        assert StreamEventType.SENTENCE in types
        # SENTENCE 应是完整句
        sentence_events = [e for e in events if e.type == StreamEventType.SENTENCE]
        assert any("hello world。" in e.text for e in sentence_events)

    def test_plain_text_no_delimiter_flush(self):
        proc = StreamProcessor()
        events = proc.feed("no period")
        # feed 返回 TEXT_DELTA，SentenceBuffer 缓存文本不产出 SENTENCE
        text_deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert len(text_deltas) >= 1
        assert not any(e.type == StreamEventType.SENTENCE for e in events)
        # flush 吐出尾句
        flush_events = proc.flush()
        assert any(e.type == StreamEventType.SENTENCE for e in flush_events)
        assert any("no period" in e.text for e in flush_events)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `PYTHONPATH=/home/yomu/Agent python -m pytest core/cognitive/tests/test_stream_processor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.cognitive.stream_processor'`

- [ ] **Step 3: 创建 StreamProcessor 骨架 + StreamEvent 类型**

```python
# core/cognitive/stream_processor.py
"""StreamProcessor — 流式标签检测 + 文本管道。

纯状态机组件。管道顺序:
  delta → mood 剥离 → inner_voice 分离 → BlockFilter → SentenceBuffer
输出: list[StreamEvent] — MOOD / INNER_VOICE / SENTENCE / TEXT_DELTA / TTFT
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.text.block_filter import BlockFilter
from core.text.sentence_buffer import SentenceBuffer


class StreamEventType(StrEnum):
    MOOD = "mood"
    INNER_VOICE = "inner_voice"
    SENTENCE = "sentence"
    TEXT_DELTA = "text_delta"
    TTFT = "ttft"


@dataclass(frozen=True)
class StreamEvent:
    type: StreamEventType
    text: str = ""


class StreamProcessor:
    """流式文本解析管道。纯状态机，零 I/O。"""

    # 标签常量
    _MOOD_OPEN = "<mood>"
    _MOOD_CLOSE = "</mood>"
    _IV_OPEN = "<inner_voice>"
    _IV_CLOSE = "</inner_voice>"

    def __init__(self, *, min_chunk_length: int = 5,
                 max_buffer_length: int = 2000) -> None:
        self._block_filter = BlockFilter()
        self._sentence_buf = SentenceBuffer(
            min_chunk_length=min_chunk_length,
            max_buffer_length=max_buffer_length,
        )
        self._mood_state = "MOOD_PRE"  # MOOD_PRE | MOOD | DONE
        self._mood_buf = ""
        self._mood_tail = ""
        self._iv_state = "PRE"  # PRE | THINK | REPLY
        self._iv_think_buf = ""
        self._iv_tail = ""
        self._ttft_sent = False

    def feed(self, delta: str) -> list[StreamEvent]:
        """输入流式文本片段，返回零到多个事件。"""
        if not delta:
            return []
        events: list[StreamEvent] = []

        # --- mood 剥离 ---
        delta, mood_events = self._process_mood(delta)
        events.extend(mood_events)
        if not delta:
            return events

        # --- inner_voice 分离 ---
        delta, iv_events = self._process_inner_voice(delta)
        events.extend(iv_events)
        if not delta:
            return events

        # --- TTFT ---
        # 匹配现有代码行为: TTFT 在 inner_voice 标签闭合后的第一个可见 token 触发。
        # inner_voice 前的文本（如 "hello<inner_voice>...</inner_voice>" 中的 "hello"）
        # 不触发 TTFT——因为 _process_inner_voice 在 PRE 阶段直接产生 TEXT_DELTA 事件，
        # 而 TTFT 检查在 feed() 的后半段，此时 delta 已全部被 inner_voice 处理消费。
        if not self._ttft_sent:
            self._ttft_sent = True
            events.append(StreamEvent(type=StreamEventType.TTFT))

        # --- BlockFilter + SentenceBuffer ---
        clean = self._block_filter.feed(delta)
        if clean:
            events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=clean))
            sentence = self._sentence_buf.feed(clean)
            if sentence:
                events.append(StreamEvent(type=StreamEventType.SENTENCE, text=sentence))

        return events

    def flush(self) -> list[StreamEvent]:
        """流结束时调用：吐出尾句、丢弃未闭合块、处理未闭合标签。"""
        events: list[StreamEvent] = []

        # 未闭合 inner_voice → 广播残留
        if self._iv_state == "THINK":
            if self._iv_tail:
                self._iv_think_buf += self._iv_tail
                self._iv_tail = ""
            if self._iv_think_buf.strip():
                events.append(StreamEvent(
                    type=StreamEventType.INNER_VOICE,
                    text=self._iv_think_buf.strip(),
                ))

        # PRE 状态残留 tail → 作为普通文本处理
        if self._iv_state == "PRE" and self._iv_tail:
            clean = self._block_filter.feed(self._iv_tail)
            if clean:
                events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=clean))
                sentence = self._sentence_buf.feed(clean)
                if sentence:
                    events.append(StreamEvent(type=StreamEventType.SENTENCE, text=sentence))
            self._iv_tail = ""

        # BlockFilter flush
        trailing = self._block_filter.flush()
        if trailing:
            events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=trailing))
            sentence = self._sentence_buf.feed(trailing)
            if sentence:
                events.append(StreamEvent(type=StreamEventType.SENTENCE, text=sentence))

        # SentenceBuffer flush
        remaining = self._sentence_buf.flush()
        if remaining and remaining.strip():
            events.append(StreamEvent(type=StreamEventType.SENTENCE, text=remaining))

        return events

    def reset(self) -> None:
        """重置所有内部状态（每轮对话开始时调用）。"""
        self._block_filter.reset()
        self._sentence_buf.reset()
        self._mood_state = "MOOD_PRE"
        self._mood_buf = ""
        self._mood_tail = ""
        self._iv_state = "PRE"
        self._iv_think_buf = ""
        self._iv_tail = ""
        self._ttft_sent = False

    # ---- mood 状态机 ----

    def _process_mood(self, delta: str) -> tuple[str, list[StreamEvent]]:
        """MOOD_PRE → MOOD → DONE。返回 (remainder_delta, events)。"""
        events: list[StreamEvent] = []

        if self._mood_state == "MOOD_PRE":
            combined = self._mood_tail + delta
            idx = combined.find(self._MOOD_OPEN)
            if idx >= 0:
                before = combined[:idx].strip()
                if before:
                    self._mood_state = "DONE"
                    return combined, events
                self._mood_state = "MOOD"
                delta = combined[idx + len(self._MOOD_OPEN):]
                self._mood_tail = ""
            else:
                safe_len = max(0, len(combined) - len(self._MOOD_OPEN) + 1)
                if safe_len > 0:
                    safe_text = combined[:safe_len]
                    stripped = safe_text.lstrip()
                    if stripped and not stripped.startswith("<"):
                        self._mood_state = "DONE"
                        return combined, events
                    delta = safe_text
                else:
                    self._mood_tail = combined
                    return "", events
                self._mood_tail = combined[safe_len:]

        if self._mood_state == "MOOD":
            combined = self._mood_tail + delta
            idx = combined.find(self._MOOD_CLOSE)
            if idx >= 0:
                self._mood_buf += combined[:idx]
                delta = combined[idx + len(self._MOOD_CLOSE):].lstrip()
                self._mood_state = "DONE"
                self._mood_tail = ""
                if self._mood_buf.strip():
                    events.append(StreamEvent(
                        type=StreamEventType.MOOD,
                        text=self._mood_buf.strip(),
                    ))
            else:
                keep_len = min(len(self._MOOD_CLOSE) - 1, len(combined))
                if len(combined) > keep_len:
                    self._mood_buf += combined[:-keep_len]
                self._mood_tail = combined[-keep_len:]
                return "", events

        # DONE: pass through
        return delta, events

    # ---- inner_voice 状态机 ----

    def _process_inner_voice(self, delta: str) -> tuple[str, list[StreamEvent]]:
        """PRE → THINK → REPLY。返回 (remainder_delta, events)。"""
        events: list[StreamEvent] = []

        if self._iv_state == "PRE":
            combined = self._iv_tail + delta
            idx = combined.find(self._IV_OPEN)
            if idx >= 0:
                before = combined[:idx].lstrip()
                if before:
                    clean = self._block_filter.feed(before)
                    if clean:
                        events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=clean))
                        sentence = self._sentence_buf.feed(clean)
                        if sentence:
                            events.append(StreamEvent(type=StreamEventType.SENTENCE, text=sentence))
                self._iv_state = "THINK"
                self._iv_tail = ""
                delta = combined[idx + len(self._IV_OPEN):]
            else:
                safe_len = max(0, len(combined) - len(self._IV_OPEN) + 1)
                if safe_len > 0:
                    clean = self._block_filter.feed(combined[:safe_len])
                    if clean:
                        events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=clean))
                        sentence = self._sentence_buf.feed(clean)
                        if sentence:
                            events.append(StreamEvent(type=StreamEventType.SENTENCE, text=sentence))
                self._iv_tail = combined[safe_len:]
                return "", events

        if self._iv_state == "THINK":
            combined = self._iv_tail + delta
            idx = combined.find(self._IV_CLOSE)
            if idx >= 0:
                self._iv_think_buf += combined[:idx]
                after = combined[idx + len(self._IV_CLOSE):].lstrip()
                self._iv_state = "REPLY"
                self._iv_tail = ""
                if self._iv_think_buf.strip():
                    events.append(StreamEvent(
                        type=StreamEventType.INNER_VOICE,
                        text=self._iv_think_buf.strip(),
                    ))
                delta = after
            else:
                keep_len = min(len(self._IV_CLOSE) - 1, len(combined))
                if len(combined) > keep_len:
                    self._iv_think_buf += combined[:-keep_len]
                self._iv_tail = combined[-keep_len:]
                return "", events

        # REPLY: pass through
        return delta, events
```

- [ ] **Step 4: 运行基础测试验证通过**

Run: `PYTHONPATH=/home/yomu/Agent python -m pytest core/cognitive/tests/test_stream_processor.py::TestBasicText -v`
Expected: PASS

- [ ] **Step 5: 补充跨 chunk 标签检测测试（spec 测试表格中的 9 个标签相关用例）**

```python
class TestMoodTag:
    """<mood> 标签检测。"""

    def test_complete_mood_tag(self):
        proc = StreamProcessor()
        events = proc.feed("<mood>happy</mood>hi")
        mood_events = [e for e in events if e.type == StreamEventType.MOOD]
        assert len(mood_events) == 1
        assert mood_events[0].text == "happy"

    def test_mood_open_across_chunks(self):
        proc = StreamProcessor()
        e1 = proc.feed("<mo")
        assert not any(e.type == StreamEventType.MOOD for e in e1)
        e2 = proc.feed("od>happy</mood>hi")
        mood_events = [e for e in e2 if e.type == StreamEventType.MOOD]
        assert len(mood_events) == 1
        assert mood_events[0].text == "happy"

    def test_mood_close_across_chunks(self):
        proc = StreamProcessor()
        e1 = proc.feed("<mood>hap")
        assert not any(e.type == StreamEventType.MOOD for e in e1)
        e2 = proc.feed("py</mood>hi")
        mood_events = [e for e in e2 if e.type == StreamEventType.MOOD]
        assert len(mood_events) == 1
        assert mood_events[0].text == "happy"

    def test_unclosed_mood_flush_discards(self):
        proc = StreamProcessor()
        proc.feed("<mood>happy")
        events = proc.flush()
        # 未闭合 mood → 丢弃残留，不产生 MOOD 事件
        assert not any(e.type == StreamEventType.MOOD for e in events)

    def test_text_before_mood_aborts_detection(self):
        proc = StreamProcessor()
        events = proc.feed("hello <mood>happy</mood>")
        # mood 前有非空白文本 → DONE → 不检测 mood
        assert not any(e.type == StreamEventType.MOOD for e in events)
        # hello 应作为普通文本输出
        text_deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert any("hello" in e.text for e in text_deltas)

    def test_whitespace_before_mood_tag(self):
        proc = StreamProcessor()
        events = proc.feed("  <mood>happy</mood>hi")
        mood_events = [e for e in events if e.type == StreamEventType.MOOD]
        assert len(mood_events) == 1
        assert mood_events[0].text == "happy"


class TestInnerVoiceTag:
    """<inner_voice> 标签检测。"""

    def test_complete_inner_voice_tag(self):
        proc = StreamProcessor()
        events = proc.feed("<inner_voice>thinking</inner_voice>reply")
        iv_events = [e for e in events if e.type == StreamEventType.INNER_VOICE]
        assert len(iv_events) == 1
        assert iv_events[0].text == "thinking"

    def test_inner_voice_open_across_chunks(self):
        proc = StreamProcessor()
        e1 = proc.feed("<inner_v")
        assert not any(e.type == StreamEventType.INNER_VOICE for e in e1)
        e2 = proc.feed("oice>thinking</inner_voice>hi")
        iv_events = [e for e in e2 if e.type == StreamEventType.INNER_VOICE]
        assert len(iv_events) == 1
        assert iv_events[0].text == "thinking"

    def test_inner_voice_close_across_chunks(self):
        proc = StreamProcessor()
        e1 = proc.feed("<inner_voice>th")
        assert not any(e.type == StreamEventType.INNER_VOICE for e in e1)
        e2 = proc.feed("ink</inner_voice>hi")
        iv_events = [e for e in e2 if e.type == StreamEventType.INNER_VOICE]
        assert len(iv_events) == 1
        assert iv_events[0].text == "thinking"

    def test_unclosed_inner_voice_flush_broadcasts(self):
        proc = StreamProcessor()
        proc.feed("<inner_voice>thinking")
        events = proc.flush()
        # 未闭合 inner_voice → flush 广播残留
        iv_events = [e for e in events if e.type == StreamEventType.INNER_VOICE]
        assert len(iv_events) == 1
        assert "thinking" in iv_events[0].text


class TestBothTags:
    """mood + inner_voice 同时出现。"""

    def test_both_tags_same_chunk(self):
        proc = StreamProcessor()
        events = proc.feed("<mood>happy</mood><inner_voice>hmm</inner_voice>hi")
        mood_events = [e for e in events if e.type == StreamEventType.MOOD]
        iv_events = [e for e in events if e.type == StreamEventType.INNER_VOICE]
        assert len(mood_events) == 1
        assert len(iv_events) == 1
        assert mood_events[0].text == "happy"
        assert iv_events[0].text == "hmm"


class TestCodeBlockFiltering:
    """代码块过滤。"""

    def test_code_block_filtered(self):
        proc = StreamProcessor()
        events = proc.feed("hi <|code|python>print(1)</|code|> bye。")
        # 代码块内容被 BlockFilter 吞掉
        text_deltas = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        all_text = "".join(e.text for e in text_deltas)
        assert "print(1)" not in all_text
        # 前导和后续文本保留
        sentence_events = [e for e in events if e.type == StreamEventType.SENTENCE]
        assert any("bye" in e.text for e in sentence_events)


class TestTTFT:
    """首个 token 到达事件。"""

    def test_ttft_fires_once(self):
        proc = StreamProcessor()
        e1 = proc.feed("hello")
        ttft_events = [e for e in e1 if e.type == StreamEventType.TTFT]
        assert len(ttft_events) == 1
        e2 = proc.feed(" world")
        ttft_events2 = [e for e in e2 if e.type == StreamEventType.TTFT]
        assert len(ttft_events2) == 0  # 第二次不触发

    def test_ttft_not_before_mood_tag(self):
        proc = StreamProcessor()
        events = proc.feed("<mood>happy</mood>visible")
        # TTFT 应在 mood 标签剥离后才触发
        ttft_events = [e for e in events if e.type == StreamEventType.TTFT]
        assert len(ttft_events) == 1, "TTFT should fire once for visible text"
        # TTFT 在 visible text 到达后才触发
        visible_events = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
        ttft_idx = events.index(ttft_events[0])
        first_visible_idx = events.index(visible_events[0]) if visible_events else 0
        assert ttft_idx >= first_visible_idx


class TestSafeLenForwarding:
    """MOOD_PRE safe_len 动态转发 — 防止 < 被过早泄漏。"""

    def test_long_prefix_before_partial_mood(self):
        proc = StreamProcessor()
        e1 = proc.feed("a" * 100 + "<m")
        # 前 100 个 'a' 全部作为 TEXT_DELTA 安全转发
        text_deltas = [e for e in e1 if e.type == StreamEventType.TEXT_DELTA]
        assert len(text_deltas) >= 1
        all_text = "".join(e.text for e in text_deltas)
        assert "a" * 90 in all_text  # 大部分已转发
        assert "<" not in all_text    # < 未泄漏
        e2 = proc.feed("ood>happy</mood>")
        mood_events = [e for e in e2 if e.type == StreamEventType.MOOD]
        assert len(mood_events) == 1
        assert mood_events[0].text == "happy"


class TestReset:
    """reset() 清除所有内部状态。"""

    def test_reset_clears_state(self):
        proc = StreamProcessor()
        proc.feed("<mood>happy</mood><inner_voice>think</inner_voice>hello。")
        proc.reset()
        # 重置后应像新实例一样
        events = proc.feed("fresh start。")
        assert any(e.type == StreamEventType.SENTENCE for e in events)
        assert not any(e.type == StreamEventType.MOOD for e in events)
```

- [ ] **Step 6: 运行全部 StreamProcessor 测试**

Run: `PYTHONPATH=/home/yomu/Agent python -m pytest core/cognitive/tests/test_stream_processor.py -v`
Expected: 17 passed

- [ ] **Step 7: Commit**

```bash
git add core/cognitive/stream_processor.py core/cognitive/tests/test_stream_processor.py
git commit -m "feat: add StreamProcessor — streaming tag detection + text pipeline

Extract mood/inner_voice/sentence state machines from CognitiveLoop._think_loop()
into a pure state machine component. 17 unit tests covering tag detection,
cross-chunk buffering, block filtering, TTFT, and edge cases."
```

---

### Task 2: 集成 StreamProcessor 到 CognitiveLoop._think_loop()

**Files:**
- Modify: `core/cognitive/loop.py:286-603`（`_think_loop` 方法 + 新增 `_dispatch_stream_event`）

**Interfaces:**
- Consumes: `StreamProcessor`, `StreamEvent`, `StreamEventType` (from Task 1)
- Produces: `CognitiveLoop._dispatch_stream_event()` (新增私有方法)

**Spec reference:** `_dispatch_stream_event()` 集成点设计 + `_think_loop` 使用方式

- [ ] **Step 1: 在 CognitiveLoop 中添加 `_dispatch_stream_event()` 方法**

在 `loop.py` 中 `CognitiveLoop` 类的 `_build_wake_up_message` 静态方法之前（line ~886），插入：

```python
    # ---- StreamProcessor event dispatch ----

    async def _dispatch_stream_event(
        self, event: StreamEvent, *,
        speaking_started: bool, all_sentences: list[str],
    ) -> tuple[bool, list[str]]:
        """分发 StreamEvent → 副作用。

        这是 StreamProcessor（纯状态机）和 CognitiveLoop（副作用世界）之间的桥。
        返回更新后的 (speaking_started, all_sentences)。
        """
        if event.type == StreamEventType.MOOD:
            if self._character_engine:
                _apply_mood(event.text, self._character_engine, self._tts_mgr)
            self._mood_applied = True

        elif event.type == StreamEventType.INNER_VOICE:
            if event.text:
                try:
                    await self._output.command(
                        "inner_voice",
                        params={"content": event.text},
                    )
                except Exception:
                    logger.debug("inner_voice broadcast failed", exc_info=True)

        elif event.type == StreamEventType.TTFT:
            ttft_ms = int((time.perf_counter() - self._ttft_start) * 1000)
            if self._clock is not None:
                self._clock._spans["ttft"] = ttft_ms
            logger.debug("TTFT: %d ms", ttft_ms)

        elif event.type == StreamEventType.SENTENCE:
            sentence = event.text
            if not sentence:
                return speaking_started, all_sentences
            all_sentences.append(sentence)
            logger.debug("分句 (%d 字符)", len(sentence))
            if not speaking_started:
                await self._output.typing(False)
                self._fsm.transition(AgentState.SPEAKING)
                self._bus.post(
                    StateChangeData(state="speaking", request_id=get_request_id()),
                    priority=EventPriority.STATE_CHANGE,
                )
                speaking_started = True
            if self._tts_mgr.active:
                self._tts_mgr.stream_sentence(sentence)
            else:
                await self._output._broadcast(
                    "on_text_delta", sentence,
                    capability=ChannelCapability.TEXT_DELTA,
                )
                await self._output.send("")

        elif event.type == StreamEventType.TEXT_DELTA:
            if not self._tts_mgr.active and event.text:
                await self._output._broadcast(
                    "on_text_delta", event.text,
                    capability=ChannelCapability.TEXT_DELTA,
                )

        return speaking_started, all_sentences
```

在 `__init__` 中新增属性初始化（放在现有属性之后，约 line ~126）：

```python
self._ttft_start: float = 0.0
```

- [ ] **Step 2: 添加 `StreamProcessor` import**

在 `loop.py` 顶部 import 区域（`from ..text.sentence_buffer import SentenceBuffer` 行之后，约 line 34）添加：

```python
from .stream_processor import StreamProcessor, StreamEvent, StreamEventType
```

- [ ] **Step 3: 替换 `_think_loop` 中的内联状态机**

将 `_think_loop` 方法中 lines 356-603（`for round_num` 循环内的流式解析 + flush tail）替换为 StreamProcessor 调用。**L604 之后的 tool_calls 执行 + round 循环收尾逻辑不变。**

**关键变更：**

删除的代码块（lines 358-603）：
- `acc_text`, `speaking_started`, `all_sentences` 变量声明（保留，移到循环顶部）
- `block_filter` / `sentence_buf` 引用（删除，StreamProcessor 内部持有）
- `MOOD_OPEN`, `MOOD_CLOSE`, `mood_state`, `mood_buf`, `mood_tail`, `mood_applied` 变量（删除）
- `TAG_OPEN`, `TAG_CLOSE`, `iv_state`, `iv_tail`, `iv_think_buf` 变量（删除）
- `_handle_sentence` 嵌套函数（删除，由 `_dispatch_stream_event` 替代）
- 流式循环中的 mood/inner_voice/block_filter/sentence_buffer 处理（lines 419-547）
- flush tail 逻辑（lines 563-603）

替换为：

```python
        try:
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                acc_text = ""
                raw_tool_calls: list[dict] = []
                speaking_started = False
                all_sentences: list[str] = []
                ttft_done = False
                t_think_start = time.perf_counter()
                self._ttft_start = t_think_start  # 供 _dispatch_stream_event TTFT 使用

                # 工具循环的后续轮次也捕获 prompt
                if round_num > 0 and self._prompt_recorder is not None:
                    self._prompt_recorder.record(
                        messages=list(messages),
                        tools=tool_defs,
                        round_num=round_num,
                        model=self._llm.model,
                    )

                # StreamProcessor: 流式标签检测 + 文本管道
                self._mood_applied = False
                proc = StreamProcessor()

                stream = self._llm.stream_think(messages, tools=tool_defs,
                    extra_body={"thinking": {"type": "disabled"}})
                try:
                    async for chunk in stream:
                        delta: str = chunk.get("content", "")
                        tc_deltas = chunk.get("tool_calls")

                        if delta:
                            acc_text += delta
                            for event in proc.feed(delta):
                                speaking_started, all_sentences = (
                                    await self._dispatch_stream_event(
                                        event,
                                        speaking_started=speaking_started,
                                        all_sentences=all_sentences,
                                    )
                                )

                        if tc_deltas:
                            merge_stream_tool_calls(raw_tool_calls, tc_deltas)

                        # Token 消耗: stream 最后可能带 usage
                        if "usage" in chunk:
                            if self._token_tracker is not None:
                                await self._token_tracker.record(
                                    usage=chunk["usage"],
                                    model=self._llm.model,
                                    round_num=round_num,
                                )
                except asyncio.CancelledError:
                    raise

                # Flush StreamProcessor
                for event in proc.flush():
                    speaking_started, all_sentences = (
                        await self._dispatch_stream_event(
                            event,
                            speaking_started=speaking_started,
                            all_sentences=all_sentences,
                        )
                    )
```

**重要：** 将 `_handle_sentence` 嵌套函数的副作用（`sentence_buf.feed`, `_output.typing(False)`, `FSM.transition(SPEAKING)`, `_tts_mgr.stream_sentence` / `_output._broadcast`）全部迁移到 `_dispatch_stream_event`。

- [ ] **Step 4: 删除 `process()` 中 `self._sentence_buf` / `self._block_filter` 的创建**

在 `process()` 方法中（line ~246-247），删除：
```python
self._sentence_buf = SentenceBuffer(min_chunk_length=5, max_buffer_length=2000)
self._block_filter = BlockFilter()
```

理由：`__init__`（L126-127）已初始化为 `None`，`_handle_interrupt`（L940-943）已有 `is not None` 守卫——删除后保持 `None`，`_handle_interrupt` 自动跳过 reset，不会崩。`_transition_idle` 不引用 `_sentence_buf`（它用 `self._tts_mgr.wait_all()`）。

- [ ] **Step 5: 运行现有 cognitive 测试验证无回归**

Run: `PYTHONPATH=/home/yomu/Agent python -m pytest core/cognitive/tests/ -v`
Expected: 现有测试全部 PASS

- [ ] **Step 6: 运行 integration 测试验证端到端行为**

Run: `PYTHONPATH=/home/yomu/Agent python -m pytest core/tests/test_phase3.py -v -k "test_basic" --timeout=120`
Expected: PASS（验证完整对话管线未被破坏）

- [ ] **Step 7: Commit**

```bash
git add core/cognitive/loop.py
git commit -m "refactor: integrate StreamProcessor into CognitiveLoop._think_loop()

Replace ~250 lines of inline mood/inner_voice/sentence state machines
with StreamProcessor + _dispatch_stream_event()."

# 如果 process() 中的 sentence_buf/block_filter 创建也被清理:
# git add core/cognitive/loop.py
# git commit -m "refactor: integrate StreamProcessor, remove duplicate buffer creation"
```

---

### Task 3: 更新 exports, README, CLAUDE.md

**Files:**
- Modify: `core/cognitive/__init__.py`
- Modify: `core/cognitive/README.md` — 文件布局、数据流图、关键机制
- Modify: `CLAUDE.md` — 目录树

**Interfaces:**
- Consumes: `StreamProcessor`, `StreamEvent`, `StreamEventType` (from Task 1)

- [ ] **Step 1: 更新 `__init__.py`**

Replace `core/cognitive/__init__.py`:

```python
"""Cognitive 包 — 对外透出 CognitiveLoop + StreamProcessor。"""
from .loop import CognitiveLoop
from .stream_processor import StreamProcessor, StreamEvent, StreamEventType

__all__ = [
    "CognitiveLoop",
    "StreamProcessor",
    "StreamEvent",
    "StreamEventType",
]
```

- [ ] **Step 2: 更新 README — 文件布局**

在 `core/cognitive/README.md` 的"文件布局"树形图中 `loop.py` 行之后添加：

```
├── stream_processor.py  — StreamProcessor: 流式标签检测 + 文本管道（mood/inner_voice/BlockFilter/SentenceBuffer）
```

- [ ] **Step 3: 更新 README — 数据流图**

替换"数据流"节的 LLM stream 部分：

```
LLM stream
  ├─ content delta ──► StreamProcessor.feed(delta)
  │                     ├─ mood 剥离 ──► MOOD 事件 → _apply_mood()
  │                     ├─ inner_voice 分离 ──► INNER_VOICE 事件 → output.command()
  │                     ├─ BlockFilter（过滤 code/markdown）
  │                     └─ SentenceBuffer ──► SENTENCE 事件 → TTS / 直接输出
  │                                       ──► TEXT_DELTA 事件 → 前端增量
  │                                       ──► TTFT 事件 → 延迟统计
  └─ tool_call delta → merge_stream_tool_calls()
                        → normalize_tool_calls()
                          → 直接执行 / submit_task 拦截
```

- [ ] **Step 4: 更新 README — 关键机制**

在"inner_voice 协议"小节末尾添加一句：

> 标签检测逻辑由 `StreamProcessor` 独立实现（纯状态机，零 I/O），详见 `stream_processor.py`。

在"关键机制"节新增 StreamProcessor 小节：

```markdown
### StreamProcessor 流式解析管道

纯状态机组件，封装 mood/inner_voice 标签检测 + BlockFilter + SentenceBuffer 管道。每轮对话创建新实例。

- `feed(delta: str) → list[StreamEvent]` — 输入流式文本片段
- `flush() → list[StreamEvent]` — 流结束吐出尾句、丢弃未闭合块、广播未闭合 inner_voice
- `reset()` — 重置所有状态

事件类型: `MOOD` / `INNER_VOICE` / `SENTENCE` / `TEXT_DELTA` / `TTFT`

`CognitiveLoop._dispatch_stream_event()` 负责将事件翻译为副作用（mood 更新、inner_voice 广播、TTS 分发、FSM 切换）。
```

- [ ] **Step 5: 更新 CLAUDE.md**

在 `CLAUDE.md` 的 `core/cognitive/` 目录树中，`loop.py` 行之后添加：

```
│   ├── stream_processor.py   # StreamProcessor: 流式标签检测 + 文本管道
```

在 cognitive/ 子模块描述中更新 `loop.py` 的说明：

```
│   ├── loop.py            # CognitiveLoop(CognitiveTools): process + _think_loop + _dispatch_stream_event
```

- [ ] **Step 6: 运行全量测试（排除 slow）**

Run: `make test-normal`
Expected: 全部 PASS（或与 main 分支一致的已知失败数）

- [ ] **Step 7: Commit**

```bash
git add core/cognitive/__init__.py core/cognitive/README.md CLAUDE.md
git commit -m "docs: update exports, README, CLAUDE.md for StreamProcessor"
```
