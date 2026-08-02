"""新协调器 StreamProcessor 测试。

对齐设计文档 `docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md`
「事件面」「数据流」「行为对照表」+ 测试策略。

迁移来源：`core/cognitive/tests/test_stream_processor.py`（事件面兼容，import 路径
更新）。按任务指示**不迁移**固化旧 #3 缺陷的两个用例
（`test_text_before_mood_aborts_detection` / `test_long_prefix_before_partial_mood`），
改写为「句中 <mood> 剥离」断言（行为表 #3 修复）。

新行为固化（行为对照表逐项）：
- #2 ``` 围栏不进 SENTENCE/TTS，进 get_blocks
- #3 句中 <mood> 剥离，`你好<mood>…</mood>世界` SENTENCE 序列 == `你好世界`
- #4 两个 <inner_voice> → 两个 INNER_VOICE 事件
- #5 `<#1#>然后说` flush 尾句含完整标签
- #6 `(眨眼)` 半角 STAGE_CN → SENTENCE 完整保留
- #6a `（我有点，犹豫。）` 内部标点不切分 → SENTENCE 完整
- #7 残缺 <mood> → 无 MOOD 事件

TEXT_DELTA 粒度：SPEECH token 即时发 TEXT_DELTA（剥离 PAUSE，不等待分句）；
SENTENCE 只在分句完成时发。TTFT：首个非 META 可见文本（SPEECH/STAGE_CN）
到达时发一次。
"""

import pytest

from core.text.stream_processor import (
    StreamProcessor,
    StreamEvent,
    StreamEventType,
)
from core.text.tokenizer import Token, TokenKind

pytestmark = pytest.mark.unit


def _feed_split(pieces, **kwargs):
    """依次 feed 各 delta，再 flush，返回扁平事件序列。"""
    proc = StreamProcessor(**kwargs)
    events: list[StreamEvent] = []
    for p in pieces:
        events.extend(proc.feed(p))
    events.extend(proc.flush())
    return events


def _whole(s: str, **kwargs) -> list[StreamEvent]:
    return _feed_split([s], **kwargs)


# ============================================================================
# Group 1: 基础文本（迁移 TestBasicText，适配 tokenizer 边界粒度）
# ============================================================================

class TestBasicText:
    def test_empty_string(self):
        proc = StreamProcessor()
        assert proc.feed("") == []
        assert proc.flush() == []

    def test_plain_text_with_period(self):
        """迁移：feed 输入含空格的文本 → TEXT_DELTA 即时（SPEECH token），
        句边界完成才出 SENTENCE（此处 "hello world。" 尾段在 flush 落地）。"""
        proc = StreamProcessor()
        events = proc.feed("hello world。")
        # TTFT 在首个可见 SPEECH 到达时触发
        assert StreamEventType.TTFT in [e.type for e in events]
        flush_events = proc.flush()
        types = [e.type for e in flush_events]
        assert StreamEventType.TEXT_DELTA in types
        assert StreamEventType.SENTENCE in types
        sentence_events = [e for e in flush_events if e.type == StreamEventType.SENTENCE]
        assert any("hello world。" in e.text for e in sentence_events)

    def test_plain_text_no_delimiter_flush(self):
        """无标点文本：TTFT/TEXT_DELTA/SENTENCE 在 flush 一并落地（tokenizer 边界粒度）。"""
        proc = StreamProcessor()
        events = proc.feed("no period")
        all_events = events + proc.flush()
        assert any(e.type == StreamEventType.TTFT for e in all_events)
        assert any(e.type == StreamEventType.TEXT_DELTA for e in all_events)
        assert any(e.type == StreamEventType.SENTENCE for e in all_events)
        assert any("no period" in e.text for e in all_events
                   if e.type == StreamEventType.SENTENCE)


# ============================================================================
# Group 2: TEXT_DELTA 粒度（任务 brief：SPEECH 即时，SENTENCE 只在分句完成）
# ============================================================================

class TestTextDeltaGranularity:
    def test_text_delta_incremental_not_sentence_gated(self):
        """SPEECH token 即时发 TEXT_DELTA；未到句边界不发 SENTENCE。"""
        proc = StreamProcessor(min_chunk_length=3)
        # "好的 " → SPEECH("好的") + SPEECH(" ") 边界触发 → TEXT_DELTA 立即
        e1 = proc.feed("好的 ")
        deltas = [e.text for e in e1 if e.type == StreamEventType.TEXT_DELTA]
        assert any("好的" in d for d in deltas)
        assert not any(e.type == StreamEventType.SENTENCE for e in e1)  # 未分句
        # 句子完整后（flush 落地）才出现 SENTENCE
        e2 = proc.feed("世界。")
        flush_events = proc.flush()
        sents = [e.text for e in e2 + flush_events
                 if e.type == StreamEventType.SENTENCE]
        assert any("好的" in s and "世界" in s for s in sents)

    def test_pause_stripped_from_delta_kept_in_sentence(self):
        """TEXT_DELTA 剥离 PAUSE；SENTENCE 保留完整标签（送 TTS）。"""
        proc = StreamProcessor()
        proc.feed("<#0.5#>你好。")
        events = proc.flush()
        deltas = [e.text for e in events if e.type == StreamEventType.TEXT_DELTA]
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        assert all("<#0.5#>" not in d for d in deltas)
        assert any("你好" in d for d in deltas)
        assert any("<#0.5#>" in s for s in sentences)


# ============================================================================
# Group 3: <mood> 标签（迁移 TestMoodTag + 修复 #3 改写）
# ============================================================================

class TestMoodTag:
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
        """残缺 <mood> → 无 MOOD 事件（行为表 #7）。"""
        proc = StreamProcessor()
        proc.feed("<mood>happy")
        events = proc.flush()
        assert not any(e.type == StreamEventType.MOOD for e in events)

    def test_whitespace_before_mood_tag(self):
        proc = StreamProcessor()
        events = proc.feed("  <mood>happy</mood>hi")
        mood_events = [e for e in events if e.type == StreamEventType.MOOD]
        assert len(mood_events) == 1
        assert mood_events[0].text == "happy"

    def test_mid_sentence_mood_stripped(self):
        """句中 <mood> 剥离为 MOOD 事件 + 语音连续（修复 #3，任意位置剥离）。

        改写自旧 `test_text_before_mood_aborts_detection`（该用例固化的是
        旧 #3 缺陷——只有句首 mood 被剥离）。
        """
        proc = StreamProcessor()
        events = proc.feed("你好<mood>开心</mood>世界")
        mood_events = [e for e in events if e.type == StreamEventType.MOOD]
        assert len(mood_events) == 1
        assert mood_events[0].text == "开心"
        flush_events = proc.flush()
        sentences = [e.text for e in events + flush_events
                     if e.type == StreamEventType.SENTENCE]
        # SENTENCE 序列 == 你好世界（mood 不泄漏，前后语音连续）
        assert sentences == ["你好世界"]
        deltas = "".join(e.text for e in events + flush_events
                         if e.type == StreamEventType.TEXT_DELTA)
        assert "<mood>" not in deltas
        assert "你好世界" in deltas

    def test_mood_stripped_after_long_text_prefix(self):
        """改写旧 `test_long_prefix_before_partial_mood`：长文本前缀后的句中
        <mood> 仍剥离（旧缺陷：文本在前 → mood 检测被中止）。"""
        proc = StreamProcessor()
        events = proc.feed("a" * 100 + "<mood>happy</mood>world")
        mood_events = [e for e in events if e.type == StreamEventType.MOOD]
        assert len(mood_events) == 1
        assert mood_events[0].text == "happy"
        all_events = events + proc.flush()
        deltas = "".join(e.text for e in all_events
                         if e.type == StreamEventType.TEXT_DELTA)
        assert "<mood>" not in deltas
        assert "world" in deltas
        # 语音文本连续（mood 前长文本 + mood 后文本）
        sentences = [e.text for e in all_events
                     if e.type == StreamEventType.SENTENCE]
        assert any("a" * 80 in s and "world" in s for s in sentences)


# ============================================================================
# Group 4: <inner_voice> 标签（迁移 TestInnerVoiceTag + 修复 #4）
# ============================================================================

class TestInnerVoiceTag:
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
        """未闭合 <inner_voice> → flush 广播残留（行为表 #8）。"""
        proc = StreamProcessor()
        proc.feed("<inner_voice>thinking")
        events = proc.flush()
        iv_events = [e for e in events if e.type == StreamEventType.INNER_VOICE]
        assert len(iv_events) == 1
        assert "thinking" in iv_events[0].text

    def test_two_inner_voices_two_events(self):
        """两个 <inner_voice> → 两个 INNER_VOICE 事件（修复 #4）。"""
        proc = StreamProcessor()
        events = proc.feed("你<inner_voice>一</inner_voice>好<inner_voice>二</inner_voice>！")
        iv_events = [e for e in events if e.type == StreamEventType.INNER_VOICE]
        assert len(iv_events) == 2
        assert iv_events[0].text == "一"
        assert iv_events[1].text == "二"

    def test_empty_inner_voice_no_event(self):
        """空 <inner_voice></inner_voice> → 不发事件（错误处理表）。"""
        proc = StreamProcessor()
        events = proc.feed("<inner_voice></inner_voice>hi")
        assert not any(e.type == StreamEventType.INNER_VOICE for e in events)


# ============================================================================
# Group 5: mood + inner_voice 同时出现（迁移 TestBothTags）
# ============================================================================

class TestBothTags:
    def test_both_tags_same_chunk(self):
        proc = StreamProcessor()
        events = proc.feed("<mood>happy</mood><inner_voice>hmm</inner_voice>hi")
        mood_events = [e for e in events if e.type == StreamEventType.MOOD]
        iv_events = [e for e in events if e.type == StreamEventType.INNER_VOICE]
        assert len(mood_events) == 1
        assert len(iv_events) == 1
        assert mood_events[0].text == "happy"
        assert iv_events[0].text == "hmm"


class TestUnknownMetaKind:
    """未知 META emit 降级：log warning + 丢弃（不产生事件、不崩溃）。"""

    def test_unknown_emit_dropped_with_warning(self, caplog):
        """meta_kind 不在 META_TAGS 注册表（或枚举无对应值）→ 事件丢弃 + warning。

        注意：直接构造 Token 喂 _route——不能用 `[action]x[/action]` 走 feed 路径
        （`[` 前缀不被 tokenizer 识别，会落为 SPEECH 产生 TEXT_DELTA/SENTENCE）。
        """
        proc = StreamProcessor()
        events: list[StreamEvent] = []
        with caplog.at_level("WARNING", logger="core.text.stream_processor"):
            proc._route(
                Token(TokenKind.META, text="x", meta_kind="action"), events,
            )
        assert events == []
        assert any("未知 META 类型" in r.message for r in caplog.records)

    def test_known_emit_still_emits(self):
        """mood/inner_voice emit 仍正常发事件（行为不变）。"""
        proc = StreamProcessor()
        events: list[StreamEvent] = []
        proc._route(
            Token(TokenKind.META, text="happy", meta_kind="mood"), events,
        )
        assert events == [StreamEvent(type=StreamEventType.MOOD, text="happy")]


# ============================================================================
# Group 6: 代码块过滤（迁移 TestCodeBlockFiltering + 修复 #2）
# ============================================================================

class TestCodeBlockFiltering:
    def test_code_block_stripped(self):
        proc = StreamProcessor()
        events = proc.feed("hi <|code|python>print(1)</|code|> bye。")
        text_deltas = "".join(e.text for e in events
                              if e.type == StreamEventType.TEXT_DELTA)
        assert "print(1)" not in text_deltas
        all_events = events + proc.flush()
        sentence_events = [e for e in all_events if e.type == StreamEventType.SENTENCE]
        assert any("bye" in e.text for e in sentence_events)

    def test_code_block_cross_chunk(self):
        proc = StreamProcessor()
        e1 = proc.feed("hi <|cod")
        text1 = "".join(e.text for e in e1 if e.type == StreamEventType.TEXT_DELTA)
        assert "print" not in text1
        e2 = proc.feed("e|python>print(1)</|code|> bye。")
        text2 = "".join(e.text for e in e2 if e.type == StreamEventType.TEXT_DELTA)
        assert "print(1)" not in text2
        flush_events = proc.flush()
        all_text = "".join(
            e.text for e in e1 + e2 + flush_events
            if e.type == StreamEventType.TEXT_DELTA
        )
        assert "hi " in all_text
        assert " bye。" in all_text or "bye" in all_text
        assert "print(1)" not in all_text

    def test_fence_never_enters_sentence(self):
        """``` 围栏内容不进 SENTENCE/TTS（修复 #2），进 get_blocks。"""
        proc = StreamProcessor()
        events = proc.feed("好的。\n```python\nprint(1)\n```")
        all_events = events + proc.flush()
        assert not any(
            e.type == StreamEventType.SENTENCE and "print" in e.text
            for e in all_events
        )
        blocks = proc.get_blocks()
        assert any(
            kind == "code" and "print(1)" in content
            for kind, content, _lang, _after in blocks
        )

    def test_markdown_block_not_in_sentence(self):
        """<|markdown|> 块不进 SENTENCE。"""
        proc = StreamProcessor()
        events = proc.feed("前。<|markdown|># 标题</|markdown|>后。")
        all_events = events + proc.flush()
        assert not any(
            e.type == StreamEventType.SENTENCE and "# 标题" in e.text
            for e in all_events
        )
        blocks = proc.get_blocks()
        assert any(kind == "markdown" and "# 标题" in content
                   for kind, content, _lang, _after in blocks)


# ============================================================================
# Group 7: TTFT 时机
# ============================================================================

class TestTTFT:
    def test_ttft_fires_once(self):
        proc = StreamProcessor()
        e1 = proc.feed("hello ")
        ttft_events = [e for e in e1 if e.type == StreamEventType.TTFT]
        assert len(ttft_events) == 1
        e2 = proc.feed(" world")
        assert not any(e.type == StreamEventType.TTFT for e in e2)

    def test_ttft_not_before_mood_tag(self):
        """首 delta 全为 mood 时不发 TTFT；可见文本到达才发一次。"""
        proc = StreamProcessor()
        e1 = proc.feed("<mood>happy</mood>")
        assert not any(e.type == StreamEventType.TTFT for e in e1)
        e2 = proc.feed("visible")
        all_events = e1 + e2 + proc.flush()
        ttft_events = [e for e in all_events if e.type == StreamEventType.TTFT]
        assert len(ttft_events) == 1

    def test_ttft_not_before_inner_voice_only(self):
        """首 delta 全为 inner_voice 时不发 TTFT。"""
        proc = StreamProcessor()
        e1 = proc.feed("<inner_voice>hmm</inner_voice>")
        assert not any(e.type == StreamEventType.TTFT for e in e1)


# ============================================================================
# Group 8: reset（迁移 TestReset）
# ============================================================================

class TestReset:
    def test_reset_clears_state(self):
        proc = StreamProcessor()
        proc.feed("<mood>happy</mood><inner_voice>think</inner_voice>hello。")
        proc.reset()
        events = proc.feed("fresh start。")
        assert StreamEventType.TTFT in [e.type for e in events]
        assert not any(e.type == StreamEventType.MOOD for e in events)
        flush_events = proc.flush()
        assert any(e.type == StreamEventType.SENTENCE for e in flush_events)

    def test_reset_clears_blocks(self):
        proc = StreamProcessor()
        proc.feed("<|code|>x</|code|>")
        assert len(proc.get_blocks()) == 1
        proc.reset()
        assert proc.get_blocks() == []


# ============================================================================
# Group 9: PAUSE / STAGE_CN（迁移 TestPauseTags + 行为表 #5/#6/#6a）
# ============================================================================

class TestPauseTags:
    def _collect(self, proc, text):
        proc.feed(text)
        events = proc.flush()
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        deltas = [e.text for e in events if e.type == StreamEventType.TEXT_DELTA]
        return sentences, deltas

    def test_full_tag_kept_in_sentence_stripped_in_delta(self):
        proc = StreamProcessor()
        sentences, deltas = self._collect(proc, "<#0.5#>你好。")
        assert any("<#0.5#>" in s for s in sentences)
        assert all("<#0.5#>" not in d for d in deltas)
        assert any("你好" in d for d in deltas)

    def test_full_tag_does_not_break_sentence_splitting(self):
        proc = StreamProcessor()
        sentences, _ = self._collect(proc, "哼<#0.5#>……那又怎样？")
        assert any("那又怎样" in s for s in sentences)

    def test_integer_tag(self):
        proc = StreamProcessor()
        sentences, deltas = self._collect(proc, "稍等<#1#>。")
        assert any("<#1#>" in s for s in sentences)
        assert all("<#1#>" not in d for d in deltas)

    def test_tail_partial_tag_stripped_from_sentence(self):
        """半截 <#0. 在 flush 时剥离（不残留进句子）。"""
        proc = StreamProcessor()
        sentences, _ = self._collect(proc, "哈<#0.")
        assert all("<#0." not in s for s in sentences)

    def test_pause_tail_kept_in_flush_sentence(self):
        """<#1#>然后说 → flush 尾句含完整标签（行为表 #5 修复）。"""
        proc = StreamProcessor()
        proc.feed("<#1#>然后说")
        events = proc.flush()
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        assert any(s == "<#1#>然后说" for s in sentences)

    def test_halfwidth_stage_cn_kept_in_sentence(self):
        """半角 (眨眼) → SENTENCE 完整保留（行为表 #6，TTS 剥离由 tts_adapter 保证）。"""
        proc = StreamProcessor(min_chunk_length=3)
        proc.feed("(眨眼)好的。")
        events = proc.flush()
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        assert any("(眨眼)" in s for s in sentences)

    def test_stage_cn_internal_punctuation_not_split(self):
        """（我有点，犹豫。）内部标点不切分 → SENTENCE 完整（行为表 #6a）。"""
        proc = StreamProcessor()
        proc.feed("（我有点，犹豫。）然后说。")
        events = proc.flush()
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        assert any("（我有点，犹豫。）然后说" in s for s in sentences)


class TestStageEn:
    """英文语气词（圆括号）：显示剥离、TTS 保留、public_text 保留（同 PAUSE）。"""

    def test_tone_word_stripped_from_delta_kept_in_sentence(self):
        proc = StreamProcessor()
        sentences, deltas = self._collect(proc, "不过——(sighs) 被你这么一亲，我今晚大概要想着你偷偷笑了。")
        assert all("(sighs)" not in d for d in deltas)
        assert any("(sighs)" in s for s in sentences)  # SENTENCE 供 TTS，保留语气词

    def test_tone_word_does_not_trigger_ttft(self):
        """TTFT 只在首个可见文本到达时发——(sighs) 显示隐藏，单独到达不触发。"""
        proc = StreamProcessor()
        proc.feed("(sighs)")
        events = proc.flush()
        assert all(e.type is not StreamEventType.TTFT for e in events)

    def test_tone_word_kept_in_public_text(self):
        """public_text 保留英文语气词（同 PAUSE，供 analyze/log/after_response）。"""
        proc = StreamProcessor()
        proc.feed("不过——(sighs) 你好。")
        proc.flush()
        assert "(sighs)" in proc.rebuild_public_text()

    def _collect(self, proc, text):
        proc.feed(text)
        events = proc.flush()
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        deltas = [e.text for e in events if e.type == StreamEventType.TEXT_DELTA]
        return sentences, deltas


class TestAngleEn:
    """尖括号语气词（F3 违规格式）：显示 + TTS + public_text 全丢弃。"""

    def test_angle_tone_word_dropped_everywhere(self):
        proc = StreamProcessor()
        events = proc.feed("<pant>偷……偷袭可不行哦。") + proc.flush()
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        deltas = [e.text for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert all("<pant>" not in s for s in sentences)
        assert all("<pant>" not in d for d in deltas)
        assert any(s == "偷……偷袭可不行哦。" for s in sentences)

    def test_angle_tone_word_not_in_public_text(self):
        proc = StreamProcessor()
        proc.feed("<pant>偷。")
        proc.flush()
        assert "<pant>" not in proc.rebuild_public_text()

    def test_angle_non_tone_word_flows_through(self):
        """非语气词尖括号（<b>）仍是普通文本（未知模式回落）。"""
        proc = StreamProcessor()
        events = proc.feed("看<b>示例。") + proc.flush()
        deltas = [e.text for e in events if e.type == StreamEventType.TEXT_DELTA]
        assert any("<b>" in d for d in deltas)


# ============================================================================
# Group 10: emoji-only 尾部合并（迁移旧 flush 行为）
# ============================================================================

class TestEmojiMerge:
    def test_emoji_only_tail_merged_into_last_sentence(self):
        """flush 时 emoji-only 尾句合并到最后一个 SENTENCE（避免独占一行）。

        构造：`好。♪` 在 tokenizer pending 中，flush 落盘为单个 SPEECH → splitter
        先发残句 `好。`，余下 `♪` 为 emoji-only 尾 → 合并。
        """
        proc = StreamProcessor()
        proc.feed("好")
        proc.feed("。♪")
        events = proc.flush()
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        assert any(s == "好。♪" for s in sentences)
        assert all(s != "♪" for s in sentences)

    def test_emoji_only_tail_without_prior_sentence_standalone(self):
        """无先前 SENTENCE 时，emoji-only 尾句作为独立 SENTENCE 输出（不丢弃）。"""
        proc = StreamProcessor(min_chunk_length=3)
        proc.feed("今天天气真好。")
        proc.feed("♪")
        events = proc.flush()
        sentences = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        assert any("♪" in s for s in sentences)


# ============================================================================
# Group 11: get_blocks / rebuild_public_text
# ============================================================================

class TestBlocksAndPublicText:
    def test_get_blocks_ordered_with_after_idx(self):
        """`好的。\n` + 围栏：块插在第 1 个已完成句子之后（after_sentence_idx=1）。"""
        proc = StreamProcessor()
        proc.feed("好的。\n```python\nprint(1)\n```")
        proc.flush()
        assert proc.get_blocks() == [("code", "print(1)\n", "python", 1)]

    def test_get_blocks_mid_sentence_code_before_sentence(self):
        """句中 <|code|>：闭合时句边界未消费 → after_sentence_idx=0（块落句前）。"""
        proc = StreamProcessor()
        proc.feed("看这个<|code|>x=1</|code|>示例")
        proc.flush()
        assert proc.get_blocks() == [("code", "x=1", "", 0)]

    def test_after_sentence_idx_includes_forced_split(self):
        """after_sentence_idx 含 max_buffer 强制切分发出的句子（spec v3.6.3）。"""
        proc = StreamProcessor(min_chunk_length=3, max_buffer_length=10)
        proc.feed("abcdefghijklmnopqrstuvwxyz")
        proc.feed("<|code|>x</|code|>")
        proc.flush()
        # 26 字符 → 2 次强制切分（abc…/klm…）已发出 → completed_count=2
        assert proc.get_blocks() == [("code", "x", "", 2)]

    def test_get_blocks_valid_after_flush(self):
        """flush() 后 get_blocks() 有效（收尾围栏 EOF 确认闭合）。"""
        proc = StreamProcessor()
        proc.feed("第一段。\n```python\n代码\n```")
        proc.flush()
        blocks = proc.get_blocks()
        assert any(kind == "code" and lang == "python" for kind, _c, lang, _a in blocks)

    def test_unclosed_block_dropped(self):
        """未闭合围栏 → 整体丢弃，不进 blocks（行为表 #11）。"""
        proc = StreamProcessor()
        proc.feed("```python\n代码")
        proc.flush()
        assert proc.get_blocks() == []

    def test_rebuild_public_text(self):
        """public_text：去 META、保留 PAUSE/STAGE_CN、代码块按 token 原序插回。"""
        proc = StreamProcessor()
        proc.feed("看这个<|code|>x=1</|code|>示例")
        proc.feed("好的。<#0.5#>继续<mood>开心</mood>")
        proc.feed("（眨眼）")
        proc.flush()
        assert proc.rebuild_public_text() == "看这个x=1示例好的。<#0.5#>继续（眨眼）"

    def test_blocks_do_not_emit_stream_events(self):
        """CODE/MARKDOWN 块不产生流式事件（只在 get_blocks 收集）。"""
        proc = StreamProcessor()
        events = proc.feed("<|code|python>print(1)</|code|><|markdown|># 标题</|markdown|>")
        assert not any(e.type == StreamEventType.TEXT_DELTA for e in events)
        assert not any(e.type == StreamEventType.SENTENCE for e in events)
        assert not any(e.type == StreamEventType.TTFT for e in events)
        assert len(proc.get_blocks()) == 2


# ============================================================================
# Group 12: 跨 chunk 不变式（事件级 property test）
# ============================================================================

class TestEventProperty:
    """feed 拆分的扁平事件序列 == 一次 feed 的序列（tokenizer 不变式的上层传递）。"""

    PROPERTY_CORPUS = [
        "<#0.5#>好的，等一下<#1#>然后说",
        "（我有点，犹豫。）",
        "```python\n代码\n```",
        "价格<100元",
        "你好<mood>开心</mood>世界",
        "(laughs)",
        "好的。\n```python\nprint(1)\n```",
        "看这个<|code|>x=1</|code|>示例",
        "<inner_voice>思考中</inner_voice>然后说",
        "（眨眼）好的。",
        "今天天气真好。😊",
        "hello world。",
    ]

    def test_every_split_point(self):
        for s in self.PROPERTY_CORPUS:
            expected = _whole(s)
            for i in range(len(s) + 1):
                got = _feed_split([s[:i], s[i:]])
                assert got == expected, (
                    f"S={s!r} split={i!r}\n  whole={expected}\n  got={got}"
                )

    def test_char_by_char(self):
        for s in self.PROPERTY_CORPUS:
            expected = _whole(s)
            got = _feed_split(list(s))
            assert got == expected, f"S={s!r} char-by-char\n  whole={expected}\n  got={got}"


# ============================================================================
# Group: 跨标签序列事件面（spec 2026-08-02 A 节）
# ============================================================================

class TestSequenceEvents:
    """跨标签序列的事件面：状态残留会在事件序列暴露。"""

    def test_fence_reenter_events_and_blocks(self):
        s = "第一段。\n```python\n代码1\n```\n又说。\n```js\n代码2\n```\n结尾。"
        proc = StreamProcessor()
        events: list[StreamEvent] = []
        events.extend(proc.feed(s))
        events.extend(proc.flush())
        # 代码内容不进 SENTENCE / TEXT_DELTA
        assert not any(
            e.type in (StreamEventType.SENTENCE, StreamEventType.TEXT_DELTA)
            and ("代码1" in e.text or "代码2" in e.text)
            for e in events
        )
        # 两个块按 token 顺序，lang 正确，after_idx 跨块计数
        assert [(b[0], b[2], b[3]) for b in proc.get_blocks()] == [
            ("code", "python", 1), ("code", "js", 2)]

    def test_mood_inner_voice_fence_sequence(self):
        s = "好<mood>开心</mood><inner_voice>思考</inner_voice>\n```python\ncode\n```\n尾"
        proc = StreamProcessor()
        events: list[StreamEvent] = []
        events.extend(proc.feed(s))
        events.extend(proc.flush())
        types = [e.type for e in events]
        assert types.count(StreamEventType.MOOD) == 1
        assert types.count(StreamEventType.INNER_VOICE) == 1
        # 代码块内容不进 SENTENCE
        assert not any(e.type == StreamEventType.SENTENCE and "code" in e.text for e in events)
        # 语音文本跨 META 连续，无标签泄漏
        sentence_texts = [e.text for e in events if e.type == StreamEventType.SENTENCE]
        assert "好" in sentence_texts and "尾" in sentence_texts


# ============================================================================
# Group: 多代码块链路（spec 2026-08-02 B 节）
# ============================================================================

class TestMultipleCodeBlocks:
    """同一轮多个代码块：after_sentence_idx 跨块计数 + get_blocks 顺序 + parts 交错。"""

    def test_two_fences_blocks_and_events(self):
        s = "第一段。\n```python\ncode1\n```\n第二段。\n```js\ncode2\n```\n结尾。"
        proc = StreamProcessor()
        events: list[StreamEvent] = []
        events.extend(proc.feed(s))
        events.extend(proc.flush())
        # 两块按 token 顺序；第二个块 after_idx 跨块计数（2 > 1）
        assert [(b[0], b[2], b[3]) for b in proc.get_blocks()] == [
            ("code", "python", 1), ("code", "js", 2)]
        # 代码内容不进 SENTENCE / TEXT_DELTA
        assert not any(
            e.type in (StreamEventType.SENTENCE, StreamEventType.TEXT_DELTA)
            and ("code1" in e.text or "code2" in e.text)
            for e in events
        )

    def test_build_parts_two_blocks_interleave(self):
        from core.text.parts import build_parts
        sentences = ["第一段。", "第二段。", "结尾。"]
        blocks = [("code", "code1\n", "python", 1), ("code", "code2\n", "js", 2)]
        parts, content_type = build_parts(sentences, blocks)
        assert [p.content for p in parts] == ["第一段。", "code1", "第二段。", "code2", "结尾。"]
        assert content_type == {"markdown": [], "code": [1, 3]}

    def test_fence_then_markdown_tag_mixed(self):
        s = "开头。\n```python\ncode1\n```\n中间。\n<|markdown|>m1</|markdown|>\n结尾。"
        proc = StreamProcessor()
        events: list[StreamEvent] = []
        events.extend(proc.feed(s))
        events.extend(proc.flush())
        blocks = proc.get_blocks()
        assert [(b[0], b[2]) for b in blocks] == [("code", "python"), ("markdown", "")]
        # 第二个块 after_idx 跨块计数（markdown 块落在两个完成句之后）
        assert blocks[0][3] == 1 and blocks[1][3] == 2
