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
        """初始化流处理器。

        Args:
            min_chunk_length: SentenceBuffer 最小分块长度（字符数）。
            max_buffer_length: SentenceBuffer 最大缓冲长度（字符数）。
        """
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
        """输入流式文本片段，返回零到多个事件。

        Args:
            delta: LLM 输出的增量文本片段。

        Returns:
            解析出的 StreamEvent 列表，可能为空。
        """
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

        # --- TTFT (before BlockFilter, matches CognitiveLoop L532-537) ---
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
        """流结束时调用：吐出尾句、丢弃未闭合块、处理未闭合标签。

        Returns:
            残留的 StreamEvent 列表，包括未闭合的 inner_voice、
            BlockFilter 尾部和 SentenceBuffer 残留。
        """
        events: list[StreamEvent] = []

        # MOOD_PRE 状态残留 tail → 路由经 inner_voice 管道
        # （inner_voice 可能处于 THINK 状态，需要拼接 mood_tail 以检测 </inner_voice>）
        if self._mood_state == "MOOD_PRE" and self._mood_tail:
            tail = self._mood_tail
            self._mood_tail = ""
            tail, iv_events = self._process_inner_voice(tail)
            events.extend(iv_events)
            if tail:
                clean = self._block_filter.feed(tail)
                if clean:
                    events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=clean))
                    sentence = self._sentence_buf.feed(clean)
                    if sentence:
                        events.append(StreamEvent(type=StreamEventType.SENTENCE, text=sentence))
                pending = self._block_filter.flush()
                if pending:
                    events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=pending))
                    sentence = self._sentence_buf.feed(pending)
                    if sentence:
                        events.append(StreamEvent(type=StreamEventType.SENTENCE, text=sentence))

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
        """Mood 标签状态机：MOOD_PRE → MOOD → DONE。

        Args:
            delta: 输入的增量文本片段。

        Returns:
            (remainder_delta, events) 元组：
            remainder_delta 为剥离 mood 标签后剩余文本，
            events 为解析出的 MOOD 事件列表。
        """
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
                    first_m = combined.find("<m", 0, safe_len)
                    if first_m > 0:
                        safe_len = first_m
                    elif first_m < 0 and combined.find("<m") < 0:
                        # 全文无 <m → 不可能是 mood 标签，全部转发
                        self._mood_tail = ""
                        return combined, events
                    if safe_len > 0:
                        delta = combined[:safe_len]
                    else:
                        self._mood_tail = combined
                        return "", events
                else:
                    stripped = combined.lstrip()
                    if stripped and not stripped.startswith("<"):
                        self._mood_tail = ""
                        return combined, events
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
        """Inner_voice 标签状态机：PRE → THINK → REPLY。

        Args:
            delta: 输入的增量文本片段。

        Returns:
            (remainder_delta, events) 元组：
            remainder_delta 为剥离 inner_voice 标签后剩余文本，
            events 为解析出的 INNER_VOICE / TEXT_DELTA / SENTENCE 事件列表。
        """
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
                    first_i = combined.find("<i", 0, safe_len)
                    if first_i > 0:
                        safe_len = first_i
                    elif first_i < 0 and combined.find("<i") < 0:
                        # 全文无 <i → 不可能是 inner_voice 标签，全部转发
                        self._iv_tail = ""
                        return combined, events
                    if safe_len > 0:
                        self._iv_tail = combined[safe_len:]
                        return combined[:safe_len], events
                    else:
                        self._iv_tail = combined
                        return "", events
                else:
                    stripped = combined.lstrip()
                    if stripped and not stripped.startswith("<"):
                        self._iv_tail = ""
                        return combined, events
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
