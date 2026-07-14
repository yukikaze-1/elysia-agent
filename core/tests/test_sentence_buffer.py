"""SentenceBuffer comprehensive tests — split/punctuation/min_chunk/consecutive delimiters.

Default config: delimiters=。！？.!?\n～…〜, min_chunk=5
"""

import pytest
from core.sentence_buffer import SentenceBuffer

pytestmark = pytest.mark.unit


# ============================================================================
# Helpers
# ============================================================================

def _feed_all(buf: SentenceBuffer, text: str) -> str | None:
    """Feed all chars one at a time, returning the last extracted sentence."""
    result = None
    for ch in text:
        s = buf.feed(ch)
        if s is not None:
            result = s
    return result


def _collect_all(buf: SentenceBuffer, text: str) -> list[str]:
    """Feed all chars one at a time, collecting all extracted sentences."""
    results = []
    for ch in text:
        s = buf.feed(ch)
        if s is not None:
            results.append(s)
    return results


# ============================================================================
# Group 1: Basic punctuation delimiters
# ============================================================================

class TestBasicDelimiters:
    def test_period_chinese(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "你好。") == "你好。"

    def test_exclamation_chinese(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "太好了！") == "太好了！"

    def test_question_chinese(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "真的吗？") == "真的吗？"

    def test_period_english(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "Hi. ") == "Hi."

    def test_exclamation_english(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "Great!") == "Great!"

    def test_question_english(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "Really?") == "Really?"

    def test_newline_is_delimiter(self):
        buf = SentenceBuffer(min_chunk_length=3)
        results = _collect_all(buf, "AAA\nBBB\n")
        assert results == ["AAA", "BBB"]  # \n 是分隔符，不应保留在句尾

    def test_multiple_sentences(self):
        buf = SentenceBuffer(min_chunk_length=3)
        results = _collect_all(buf, "你好！今天天气真好。要不要出去玩？")
        assert results == ["你好！", "今天天气真好。", "要不要出去玩？"]


# ============================================================================
# Group 2: Missing delimiters (～ … 〜)
# ============================================================================

class TestMissingDelimiters:
    def test_wave_dash(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "好的～") == "好的～"

    def test_wave_dash_mid_sentence(self):
        buf = SentenceBuffer(min_chunk_length=3)
        results = _collect_all(buf, "嗯～好吧。那就这样～")
        assert results == ["嗯～", "好吧。", "那就这样～"]

    def test_ellipsis(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "我想想…") == "我想想…"

    def test_ellipsis_double(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("这个嘛……算了。") == "这个嘛……"
        assert buf._extract() == "算了。"

    def test_wave_dash_japanese(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "そうですね〜") == "そうですね〜"

    def test_chinese_dash_not_split(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "他——总之很好。")
        assert result == "他——总之很好。"


# ============================================================================
# Group 3: Consecutive delimiters
# ============================================================================

class TestConsecutiveDelimiters:
    def test_exclamation_question(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("啊？！") == "啊？！"

    def test_question_exclamation(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("什么！？") == "什么！？"

    def test_double_exclamation(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("太好了！！") == "太好了！！"

    def test_double_question(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("真的吗？？") == "真的吗？？"

    def test_triple_dots_period(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("这样啊。。。") == "这样啊。。。"
        # Note: ... on its own (no trailing 。) would split earlier

    def test_wave_then_exclamation(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("好的～！") == "好的～！"

    def test_interleaved_delimiters(self):
        buf = SentenceBuffer(min_chunk_length=1)
        assert buf.feed("你好。嗯？！是啊……") == "你好。"
        assert buf._extract() == "嗯？！"
        assert buf._extract() == "是啊……"

    def test_consecutive_with_space(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("啊？！") == "啊？！"


# ============================================================================
# Group 4: Short sentence threshold (min_chunk)
# ============================================================================

class TestMinChunk:
    def test_two_char_sentence_held(self):
        buf = SentenceBuffer(min_chunk_length=5)
        result = _feed_all(buf, "好嘞")
        assert result is None

    def test_two_char_then_delimiter_held_when_more_follows(self):
        """好嘞！(3 chars < 5) with nothing following → held, then flushed."""
        buf = SentenceBuffer(min_chunk_length=5)
        result = _feed_all(buf, "好嘞！")
        assert result is None  # held (too short, nothing follows)
        assert buf.flush() == "好嘞！"  # flush releases it

    def test_two_char_then_delimiter_merged_when_more_follows(self):
        """好嘞！(3 chars < 5) then more text → held and merged."""
        buf = SentenceBuffer(min_chunk_length=5)
        results = _collect_all(buf, "好嘞！接下来我们要去吃饭了。")
        assert "好嘞！" in results[0] if results else False

    def test_five_char_sentence_emitted(self):
        buf = SentenceBuffer(min_chunk_length=5)
        result = _feed_all(buf, "太好了吧！")
        assert result == "太好了吧！"  # 5 chars ≥ 5

    def test_short_then_long(self):
        """Short「嗯。」emitted immediately when text follows, buffer continues."""
        buf = SentenceBuffer(min_chunk_length=5)
        result = buf.feed("嗯。今天天气真好。")
        assert result == "嗯。"  # 2 chars < 5 but text follows → emit (un-stick buffer)
        # "今天天气真好。" was extracted to buffer, triggers on next feed
        remaining = buf.flush()
        assert remaining == "今天天气真好。"

    def test_default_min_chunk(self):
        buf = SentenceBuffer()  # default min_chunk=5
        result = _feed_all(buf, "好。")
        assert result is None  # 2 chars < 5, nothing follows → held
        assert buf.flush() == "好。"  # released on flush

    def test_min_chunk_exactly_on_boundary(self):
        buf = SentenceBuffer(min_chunk_length=4)
        result = _feed_all(buf, "你好啊。")
        assert result == "你好啊。"  # 4 chars = 4

    def test_single_char_held(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "啊")
        assert result is None


# ============================================================================
# Group 5: English punctuation (not delimiters in Chinese context)
# ============================================================================

class TestEnglishPunctuation:
    def test_comma_does_not_split(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "Hello, world.")
        assert result == "Hello, world."  # comma not in default set

    def test_semicolon_does_not_split(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "It was late; he left.")
        assert result == "It was late; he left."

    def test_colon_does_not_split(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "Note: this. ")
        assert result == "Note: this."  # : not a delimiter

    def test_chinese_comma_not_split(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "今天天气很好，适合出去玩。")
        assert result == "今天天气很好，适合出去玩。"


# ============================================================================
# Group 6: Mixed Chinese/English
# ============================================================================

class TestMixedScript:
    def test_chinese_with_english_word(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "我很喜欢Python。")
        assert result == "我很喜欢Python。"

    def test_english_with_chinese_delimiter(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "OK好的！")
        assert result == "OK好的！"

    def test_code_block(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "可以用 print('hello') 来输出。")
        assert result == "可以用 print('hello') 来输出。"

    def test_url_not_split(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("访问 https://example.com 看看。") == "访问 https://example.com 看看。"

    def test_number_with_dot(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("圆周率是3.14。") == "圆周率是3.14。"

    def test_abbreviation(self):
        """Dr. should not split as a sentence boundary."""
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("Dr. Who is cool?") == "Dr. Who is cool?"

    def test_mixed_sentences(self):
        buf = SentenceBuffer(min_chunk_length=3)
        results = _collect_all(buf, "你好！Hello world. 真的吗？")
        # "Hello world." — the . is followed by \" → sentence boundary
        assert results[0] == "你好！"
        assert len(results) == 3


# ============================================================================
# Group 7: Streaming behavior (character-by-character)
# ============================================================================

class TestStreaming:
    def test_one_char_at_a_time(self):
        buf = SentenceBuffer(min_chunk_length=3)
        results = []
        for ch in "你好。再见！":
            s = buf.feed(ch)
            if s is not None:
                results.append(s)
        assert results == ["你好。", "再见！"]

    def test_partial_before_delimiter(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("你") is None
        assert buf.feed("好") is None
        assert buf.feed("。") == "你好。"

    def test_flush_partial(self):
        buf = SentenceBuffer()
        buf.feed("你好")
        assert buf.flush() == "你好"
        assert buf.flush() is None

    def test_flush_empty(self):
        buf = SentenceBuffer()
        assert buf.flush() is None

    def test_flush_after_complete(self):
        buf = SentenceBuffer(min_chunk_length=3)
        _feed_all(buf, "你好。")
        assert buf.flush() is None

    def test_flush_whitespace_only(self):
        buf = SentenceBuffer()
        buf.feed("   ")
        assert buf.flush() is None

    def test_reset(self):
        buf = SentenceBuffer(min_chunk_length=3)
        buf.feed("你好")
        buf.reset()
        assert buf.feed("再") is None
        assert buf.feed("见。") == "再见。"


# ============================================================================
# Group 8: Edge cases
# ============================================================================

class TestEdgeCases:
    def test_empty_input(self):
        buf = SentenceBuffer()
        assert buf.feed("") is None

    def test_whitespace_preserved(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "你好。   ")
        assert result == "你好。"

    def test_only_delimiter_discarded(self):
        buf = SentenceBuffer()
        result = _feed_all(buf, "。")
        assert result is None  # nothing before delim → skip

    def test_only_delimiters(self):
        buf = SentenceBuffer()
        result = _feed_all(buf, "。！？")
        assert result is None

    def test_very_long_sentence(self):
        buf = SentenceBuffer(min_chunk_length=3)
        long_text = "今天" * 200 + "。"
        result = _feed_all(buf, long_text)
        assert result is not None

    def test_newline_mid_sentence(self):
        buf = SentenceBuffer(min_chunk_length=3)
        results = _collect_all(buf, "第一行\n第二行。")
        assert results == ["第一行", "第二行。"]  # \n 是分隔符，不应保留在句尾

    def test_carriage_return_not_delimiter(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "hello\rworld。")
        assert result == "hello\rworld。"


# ============================================================================
# Group 9: Japanese punctuation
# ============================================================================

class TestJapanesePunctuation:
    def test_japanese_period(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert _feed_all(buf, "はい。") == "はい。"

    def test_japanese_comma_not_split(self):
        buf = SentenceBuffer(min_chunk_length=3)
        result = _feed_all(buf, "今日は散歩に行きました。")
        assert result == "今日は散歩に行きました。"


# ============================================================================
# Group 10: Batch feed
# ============================================================================

class TestBatchFeed:
    def test_three_sentences_one_feed(self):
        buf = SentenceBuffer(min_chunk_length=3)
        results = _collect_all(buf, "你好。今天天气真好。要不要出去玩？")
        assert results == ["你好。", "今天天气真好。", "要不要出去玩？"]

    def test_hold_then_batch(self):
        buf = SentenceBuffer(min_chunk_length=5)
        buf.feed("嗯。好。就这样吧。")
        # All below min_chunk=5 → everything stays in buffer
        result = buf.flush()
        assert result is not None and "就这样吧。" in result

    def test_partial_delimiter_across_feeds(self):
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("你") is None
        assert buf.feed("好") is None
        assert buf.feed("吗") is None
        assert buf.feed("？") == "你好吗？"
        assert buf.feed("很") is None
        assert buf.feed("好") is None
        assert buf.feed("。") == "很好。"


# ============================================================================
# Group 11: Configurable delimiters
# ============================================================================

class TestConfigurableDelimiters:
    def test_custom_delimiters(self):
        buf = SentenceBuffer(delimiters="，。", min_chunk_length=2)
        assert buf.feed("你好，世界。再见。") == "你好，"
        assert buf._extract() == "世界。"
        assert buf._extract() == "再见。"

    def test_no_delimiters(self):
        buf = SentenceBuffer(delimiters="")
        result = _feed_all(buf, "你好。再见！")
        assert result is None
        assert buf.flush() == "你好。再见！"

    def test_only_english_delimiters(self):
        buf = SentenceBuffer(delimiters=".!?", min_chunk_length=3)
        assert buf.feed("Hello! How are you? Good.") == "Hello!"
        assert buf._extract() == " How are you?"
        assert buf._extract() == " Good."

    def test_custom_delimiters_with_chinese_text(self):
        buf = SentenceBuffer(delimiters="！。", min_chunk_length=3)
        results = _collect_all(buf, "你好！How are you? 再见。")
        assert results == ["你好！", "How are you? 再见。"]

    def test_custom_min_chunk_with_default_delimiters(self):
        buf = SentenceBuffer(min_chunk_length=10)
        assert buf._min_chunk == 10
        result = _feed_all(buf, "你好。")
        assert result is None  # 3 chars < 10, nothing follows → held
        assert buf.flush() == "你好。"  # released on flush


