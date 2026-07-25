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
        assert iv_events[0].text == "think"  # th + ink = think (cross-chunk)

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
        # "a"*100 在 <mood> 之前 → mood 检测应被中止（匹配原始行为：
        # "有非空白内容在 <mood> 之前 → 无 mood 标签"）
        assert not any(e.type == StreamEventType.MOOD for e in e2)


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
