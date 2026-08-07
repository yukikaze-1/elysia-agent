"""SpeechSplitter 结构感知分句测试。

对齐设计文档 `docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md`
「Token 模型」「路由规则」、测试策略 #3（span 保护）与分句相关章节。

要点：
- 边界扫描只在 SPEECH 文本上进行；PAUSE/STAGE_CN 是受保护 span（记录位置，
  切句时按原序内嵌回句子）。
- 连续 SPEECH token 必须先拼接再扫描（tokenizer 的确定性边界粒度，如
  `价格<100元` → [S("价格"), S("<1"), S("00元")]）。
- `completed_count` = 句边界已消费的句子数，含因 min_chunk 暂留的短句。
"""

import re

import pytest

from core.text.tags import PAUSE_RE, STAGE_CN_RE
from core.text.tokenizer import StreamTokenizer, Token, TokenKind

from core.text.splitter import SpeechSplitter

pytestmark = pytest.mark.unit


# ============================================================================
# 辅助函数
# ============================================================================

# 行内标签（PAUSE + STAGE_CN）用于把测试串拆成 token 序列
_TAG_SPLIT_RE = re.compile(r"(<#\d+\.?\d*#>|[\（(][^\)\）]*[一-鿿][^\)\）]*[\）)])")


def _tokens(text: str, *, frag: int = 0) -> list[Token]:
    """把字符串转换为 SPEECH/PAUSE/STAGE_CN token 序列。

    Args:
        frag: SPEECH 段切分粒度（字符数）。0 = 整段一个 token；
            1 = 逐字符（最大拼接压力）；2+ = 按块切分。
    """
    out: list[Token] = []
    for part in _TAG_SPLIT_RE.split(text):
        if not part:
            continue
        if PAUSE_RE.fullmatch(part):
            out.append(Token(TokenKind.PAUSE, part))
        elif STAGE_CN_RE.fullmatch(part):
            out.append(Token(TokenKind.STAGE_CN, part))
        elif frag > 0:
            for i in range(0, len(part), frag):
                out.append(Token(TokenKind.SPEECH, part[i : i + frag]))
        else:
            out.append(Token(TokenKind.SPEECH, part))
    return out


def _feed_all(buf: SpeechSplitter, text: str, *, frag: int = 1) -> str | None:
    """feed 一串 token（默认逐字符 SPEECH），返回最后一个完整句子。"""
    result = None
    for token in _tokens(text, frag=frag):
        sentences = buf.feed(token)
        if sentences:
            result = sentences[-1]
    return result


def _collect_all(buf: SpeechSplitter, text: str, *, frag: int = 1) -> list[str]:
    """feed 一串 token，收集所有完整句子。"""
    results: list[str] = []
    for token in _tokens(text, frag=frag):
        results.extend(buf.feed(token))
    return results


def _feed_raw(buf: SpeechSplitter, tokens: list[Token]) -> list[str]:
    """feed 手工构造的 token 序列，返回全部句子。"""
    results: list[str] = []
    for token in tokens:
        results.extend(buf.feed(token))
    return results


# ============================================================================
# Group 1: 标点用例复用（token 序列喂入，结果与旧句缓存组件一致）
# ============================================================================

class TestBasicDelimiters:
    def test_period_chinese(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert _feed_all(buf, "你好。") == "你好。"

    def test_exclamation_chinese(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert _feed_all(buf, "太好了！") == "太好了！"

    def test_question_chinese(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert _feed_all(buf, "真的吗？") == "真的吗？"

    def test_newline_is_delimiter(self):
        buf = SpeechSplitter(min_chunk_length=3)
        results = _collect_all(buf, "AAA\nBBB\n")
        assert results == ["AAA", "BBB"]  # \n 是分隔符，不应保留在句尾

    def test_multiple_sentences(self):
        buf = SpeechSplitter(min_chunk_length=3)
        results = _collect_all(buf, "你好！今天天气真好。要不要出去玩？")
        assert results == ["你好！", "今天天气真好。", "要不要出去玩？"]


class TestMusicNoteDelimiters:
    def test_music_note_splits(self):
        """♪ (U+266A) 作为句子结尾。"""
        buf = SpeechSplitter(min_chunk_length=3)
        results = _collect_all(buf, "好～那我就赖在这里不走了♪ 陪你聊到你开始犯困为止。")
        assert results == ["好～那我就赖在这里不走了♪", " 陪你聊到你开始犯困为止。"]

    def test_heart_suit_splits(self):
        """♡ (U+2661) 作为句子结尾。"""
        buf = SpeechSplitter(min_chunk_length=3)
        results = _collect_all(buf, "不客气♡ 能被你这么一句道谢，这一趟折腾也值了。")
        assert results == ["不客气♡", " 能被你这么一句道谢，这一趟折腾也值了。"]

    def test_music_note_alone_at_end(self):
        """句尾 ♪ 并入前句（连续标点合并），不独占一行。"""
        buf = SpeechSplitter(min_chunk_length=3)
        results = _collect_all(buf, "今晚早点休息♪")
        assert results == ["今晚早点休息♪"]


class TestConsecutiveDelimiters:
    def test_exclamation_question(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "啊？！")) == ["啊？！"]

    def test_question_exclamation(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "什么！？")) == ["什么！？"]

    def test_double_exclamation(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "太好了！！")) == ["太好了！！"]

    def test_triple_dots_period(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "这样啊。。。")) == ["这样啊。。。"]
        assert buf.completed_count() == 1

    def test_wave_then_exclamation(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "好的～！")) == ["好的～！"]

    def test_interleaved(self):
        buf = SpeechSplitter(min_chunk_length=1)
        # feed 一次返回本次能确定的全部句子
        assert buf.feed(Token(TokenKind.SPEECH, "你好。嗯？！是啊……")) == ["你好。", "嗯？！"]
        assert buf.flush() == ["是啊……"]


class TestMinChunk:
    def test_two_char_sentence_held(self):
        buf = SpeechSplitter(min_chunk_length=5)
        result = _feed_all(buf, "好嘞")
        assert result is None

    def test_short_then_delimiter_held_when_nothing_follows(self):
        buf = SpeechSplitter(min_chunk_length=5)
        result = _feed_all(buf, "好嘞！")
        assert result is None  # 太短且后面无文本 → 暂留
        assert buf.flush() == ["好嘞！"]  # flush 释放

    def test_short_emitted_when_more_follows(self):
        buf = SpeechSplitter(min_chunk_length=5)
        results = _collect_all(buf, "嗯。今天天气真好。")
        assert results == ["嗯。", "今天天气真好。"]  # 短句但有后续文本 → 立即发出（防卡死）
        assert buf.flush() == []

    def test_five_char_sentence_emitted(self):
        buf = SpeechSplitter(min_chunk_length=5)
        result = _feed_all(buf, "太好了吧！")
        assert result == "太好了吧！"

    def test_min_chunk_exactly_on_boundary(self):
        buf = SpeechSplitter(min_chunk_length=4)
        result = _feed_all(buf, "你好啊。")
        assert result == "你好啊。"


class TestEnglishPunctuation:
    def test_url_not_split(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "访问 https://example.com 看看。")) == [
            "访问 https://example.com 看看。"
        ]

    def test_number_with_dot(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "圆周率是3.14。")) == ["圆周率是3.14。"]

    def test_decimal_terminal_period_splits(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "版本是 3.14.")) == ["版本是 3.14."]

    def test_abbreviation(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "Dr. Who is cool?")) == ["Dr. Who is cool?"]

    def test_abbreviation_case_insensitive(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "DR. Who is cool?")) == ["DR. Who is cool?"]

    def test_chinese_comma_not_split(self):
        buf = SpeechSplitter(min_chunk_length=3)
        result = _feed_all(buf, "今天天气很好，适合出去玩。")
        assert result == "今天天气很好，适合出去玩。"

    def test_mixed_sentences(self):
        buf = SpeechSplitter(min_chunk_length=3)
        results = _collect_all(buf, "你好！Hello world. 真的吗？")
        assert results[0] == "你好！"
        assert len(results) == 3


class TestStreaming:
    def test_partial_before_delimiter(self):
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "你")) == []
        assert buf.feed(Token(TokenKind.SPEECH, "好")) == []
        assert buf.feed(Token(TokenKind.SPEECH, "。")) == ["你好。"]

    def test_flush_partial(self):
        buf = SpeechSplitter()
        buf.feed(Token(TokenKind.SPEECH, "你好"))
        assert buf.flush() == ["你好"]
        assert buf.flush() == []

    def test_flush_empty(self):
        buf = SpeechSplitter()
        assert buf.flush() == []

    def test_flush_after_complete(self):
        buf = SpeechSplitter(min_chunk_length=3)
        _feed_all(buf, "你好。")
        assert buf.flush() == []

    def test_flush_whitespace_only(self):
        buf = SpeechSplitter()
        _feed_all(buf, "   ")
        assert buf.flush() == []

    def test_only_delimiter_held(self):
        buf = SpeechSplitter()
        result = _feed_all(buf, "。")
        assert result is None  # 纯标点短句 → 暂留未发出
        assert buf.completed_count() == 1  # 但句边界已消费 → 计入完成
        assert buf.flush() == ["。"]

    def test_whitespace_only_delimiter_discarded(self):
        """前导 \n（纯空白句）被丢弃，不计入完成。"""
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.SPEECH, "\n你好。")) == ["你好。"]
        assert buf.completed_count() == 1

    def test_reset(self):
        buf = SpeechSplitter(min_chunk_length=3)
        buf.feed(Token(TokenKind.SPEECH, "你好"))
        buf.reset()
        assert buf.feed(Token(TokenKind.SPEECH, "再")) == []
        assert buf.feed(Token(TokenKind.SPEECH, "见。")) == ["再见。"]


class TestMaxBufferLength:
    def test_forced_split_on_whitespace(self):
        buf = SpeechSplitter(min_chunk_length=3, max_buffer_length=20)
        result = _feed_all(buf, "this is a very long sentence without any punctuation", frag=0)
        assert result is not None
        assert len(result) <= 20

    def test_forced_split_no_whitespace(self):
        buf = SpeechSplitter(min_chunk_length=3, max_buffer_length=10)
        result = _feed_all(buf, "abcdefghijklmnopqrstuvwxyz", frag=0)
        assert result is not None
        assert len(result) <= 10

    def test_forced_split_counts_completed(self):
        """强制切分发出的长块也计入 completed_count（保证与 all_sentences 对齐）。"""
        buf = SpeechSplitter(min_chunk_length=3, max_buffer_length=10)
        _feed_all(buf, "abcdefghijklmnopqrstuvwxyz", frag=0)
        assert buf.completed_count() >= 1


# ============================================================================
# Group 2: Span 保护（PAUSE / STAGE_CN 整体不切分）
# ============================================================================

class TestSpanProtection:
    def test_stage_cn_with_internal_punctuation_not_split(self):
        """（我有点，犹豫。）含 `。` 但不产生边界 → 整段保留。"""
        buf = SpeechSplitter(min_chunk_length=3)
        # 先喂 STAGE_CN，再喂后续语音
        assert buf.feed(Token(TokenKind.STAGE_CN, "（我有点，犹豫。）")) == []
        assert buf.completed_count() == 0  # 内部 。 未触发边界
        assert buf.feed(Token(TokenKind.SPEECH, "对吧。")) == ["（我有点，犹豫。）对吧。"]
        assert buf.completed_count() == 1

    def test_stage_cn_flush_tail(self):
        """纯 STAGE_CN 尾句无语音内容 → 整体丢弃（设计 #5）。"""
        buf = SpeechSplitter()
        buf.feed(Token(TokenKind.STAGE_CN, "（我有点，犹豫。）"))
        assert buf.flush() == []

    def test_pause_whole_in_sentence(self):
        """<#0.5#> 整体嵌入句中，不因内部 . 被切。"""
        buf = SpeechSplitter(min_chunk_length=3)
        buf.feed(Token(TokenKind.SPEECH, "好的"))
        buf.feed(Token(TokenKind.PAUSE, "<#0.5#>"))
        assert buf.feed(Token(TokenKind.SPEECH, "。")) == ["好的<#0.5#>。"]
        assert buf.flush() == []

    def test_pause_no_boundary(self):
        """纯 PAUSE 不产生边界。"""
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.PAUSE, "<#0.5#>")) == []
        assert buf.completed_count() == 0
        assert buf.flush() == []  # 无语音内容 → 整体丢弃


# ============================================================================
# Group 3: PAUSE 嵌入（原序内嵌）
# ============================================================================

class TestPauseEmbedding:
    def test_pause_belongs_to_next_sentence(self):
        """好的。<#0.5#>然后 → feed 返回 好的。；flush 返回 <#0.5#>然后。"""
        buf = SpeechSplitter(min_chunk_length=5)
        assert buf.feed(Token(TokenKind.SPEECH, "好的。")) == []
        buf.feed(Token(TokenKind.PAUSE, "<#0.5#>"))
        assert buf.feed(Token(TokenKind.SPEECH, "然后")) == ["好的。"]
        assert buf.flush() == ["<#0.5#>然后"]

    def test_leading_pause_goes_to_tail(self):
        """<#1#>然后说 → PAUSE 归属待分句句子，flush 尾句含完整标签。"""
        buf = SpeechSplitter()
        buf.feed(Token(TokenKind.PAUSE, "<#1#>"))
        buf.feed(Token(TokenKind.SPEECH, "然后说"))
        assert buf.flush() == ["<#1#>然后说"]

    def test_pause_between_chars_embedded(self):
        """好<#0.5#>。 → PAUSE 按原序嵌在好与。之间。"""
        buf = SpeechSplitter(min_chunk_length=3)
        buf.feed(Token(TokenKind.SPEECH, "好"))
        buf.feed(Token(TokenKind.PAUSE, "<#0.5#>"))
        buf.feed(Token(TokenKind.SPEECH, "。"))
        assert buf.flush() == ["好<#0.5#>。"]

    def test_multiple_pauses_preserve_order(self):
        buf = SpeechSplitter(min_chunk_length=3)
        buf.feed(Token(TokenKind.SPEECH, "好"))
        buf.feed(Token(TokenKind.PAUSE, "<#0.5#>"))
        buf.feed(Token(TokenKind.SPEECH, "了"))
        buf.feed(Token(TokenKind.PAUSE, "<#1#>"))
        buf.feed(Token(TokenKind.SPEECH, "吧"))
        assert buf.feed(Token(TokenKind.SPEECH, "。")) == ["好<#0.5#>了<#1#>吧。"]
        assert buf.flush() == []


# ============================================================================
# Group 4: 被丢弃空白区内的 span 保留（行为表 #1 PAUSE 完整性）
# ============================================================================

class TestDiscardedRegionSpanPreservation:
    """纯空白 complete 被丢弃时，落在该区域内的 span 重锚保留，不静默丢失。

    回归：`_consume_region` 曾过滤掉 `pos < end` 的 span，导致
    `好的。<#1#> \n继续。` / `<#1#>\n继续。` 中 PAUSE 从 TTS 流消失。
    """

    def test_pause_newline_keeps_span_for_next(self):
        """好的。<#1#>\n继续。 → PAUSE 完整进入首句。"""
        buf = SpeechSplitter()
        assert buf.feed(Token(TokenKind.SPEECH, "好的。")) == []
        buf.feed(Token(TokenKind.PAUSE, "<#1#>"))
        assert buf.feed(Token(TokenKind.SPEECH, "\n")) == []
        assert buf.feed(Token(TokenKind.SPEECH, "继续。")) == ["好的。<#1#>"]
        assert buf.flush() == ["继续。"]

    def test_pause_space_newline_not_lost(self):
        """好的。<#1#> \n继续。 → 空格+换行的空白 complete 被丢弃时 PAUSE 重锚保留。"""
        buf = SpeechSplitter()
        assert buf.feed(Token(TokenKind.SPEECH, "好的。")) == []
        buf.feed(Token(TokenKind.PAUSE, "<#1#>"))
        buf.feed(Token(TokenKind.SPEECH, " "))
        buf.feed(Token(TokenKind.SPEECH, "\n"))
        assert buf.feed(Token(TokenKind.SPEECH, "继续。")) == ["好的。"]
        assert buf.flush() == ["<#1#>继续。"]

    def test_leading_pause_newline_not_lost(self):
        """<#1#>\n继续。 → 行首 PAUSE 在 \n 空白句被丢弃后重锚保留。"""
        buf = SpeechSplitter()
        buf.feed(Token(TokenKind.PAUSE, "<#1#>"))
        buf.feed(Token(TokenKind.SPEECH, "\n"))
        buf.feed(Token(TokenKind.SPEECH, "继续。"))
        assert buf.flush() == ["<#1#>继续。"]

    def test_multiple_pauses_in_discarded_region_keep_order(self):
        """空白区内多个 span 重锚后保持原相对顺序。"""
        buf = SpeechSplitter()
        buf.feed(Token(TokenKind.PAUSE, "<#1#>"))
        buf.feed(Token(TokenKind.PAUSE, "<#2#>"))
        buf.feed(Token(TokenKind.SPEECH, "\n"))
        buf.feed(Token(TokenKind.SPEECH, "继续"))
        assert buf.flush() == ["<#1#><#2#>继续"]

    def test_stage_cn_in_discarded_region_not_lost(self):
        """STAGE_CN 在空白句被丢弃后同样重锚保留。"""
        buf = SpeechSplitter()
        buf.feed(Token(TokenKind.STAGE_CN, "（眨眼）"))
        buf.feed(Token(TokenKind.SPEECH, "\n"))
        buf.feed(Token(TokenKind.SPEECH, "继续"))
        assert buf.flush() == ["（眨眼）继续"]


# ============================================================================
# Group 5: 完成计数（completed_count）
# ============================================================================

class TestCompletedCount:
    def test_short_sentence_held_still_counts(self):
        """好。\n → 好。<min_chunk 暂留未发出，但边界已消费 → 计数 1。"""
        buf = SpeechSplitter(min_chunk_length=5)
        assert buf.feed(Token(TokenKind.SPEECH, "好。")) == []
        assert buf.feed(Token(TokenKind.SPEECH, "\n")) == []
        assert buf.completed_count() == 1

    def test_five_char_sentence_emitted_on_newline(self):
        """代码如下： (5 字符) \n 到达 → 立即发出（不触发暂留）。"""
        buf = SpeechSplitter(min_chunk_length=5)
        assert buf.feed(Token(TokenKind.SPEECH, "代码如下：")) == []
        assert buf.feed(Token(TokenKind.SPEECH, "\n")) == ["代码如下："]
        assert buf.completed_count() == 1

    def test_multiple_sentences_count(self):
        buf = SpeechSplitter(min_chunk_length=3)
        _feed_all(buf, "你好。再见。")
        assert buf.completed_count() == 2

    def test_completed_survives_flush(self):
        buf = SpeechSplitter(min_chunk_length=5)
        buf.feed(Token(TokenKind.SPEECH, "好。"))
        buf.feed(Token(TokenKind.SPEECH, "\n"))
        assert buf.completed_count() == 1
        assert buf.flush() == ["好。\n"]
        assert buf.completed_count() == 1

    def test_held_then_unstuck_no_double_count(self):
        """暂留短句被后续文本触发发出 → 不重复计数。"""
        buf = SpeechSplitter(min_chunk_length=5)
        assert buf.feed(Token(TokenKind.SPEECH, "好。")) == []
        assert buf.completed_count() == 1
        assert buf.feed(Token(TokenKind.SPEECH, "\n")) == []
        assert buf.completed_count() == 1  # 同一边界不重复计
        assert buf.feed(Token(TokenKind.SPEECH, "接下来")) == ["好。"]
        assert buf.completed_count() == 1

    def test_held_sentence_grows_by_merging_no_double_count(self):
        """暂留短句因连续标点扩展合并 → 仍只计一次。"""
        buf = SpeechSplitter(min_chunk_length=5)
        assert buf.feed(Token(TokenKind.SPEECH, "好。")) == []
        assert buf.feed(Token(TokenKind.SPEECH, "？")) == []
        assert buf.completed_count() == 1
        assert buf.feed(Token(TokenKind.SPEECH, "啊")) == ["好。？"]
        assert buf.completed_count() == 1


# ============================================================================
# Group 6: SPEECH 拼接（tokenizer 确定性边界粒度）
# ============================================================================

class TestSpeechConcatenation:
    def test_fragmented_speech_tokens_concatenate(self):
        """价格<100元 被 tokenizer 切为 [S(价格), S(<1), S(00元)] → 拼接后不误切。"""
        buf = SpeechSplitter(min_chunk_length=3)
        result = _feed_raw(
            buf,
            [Token(TokenKind.SPEECH, "价格"), Token(TokenKind.SPEECH, "<1"),
             Token(TokenKind.SPEECH, "00元。")],
        )
        assert result == ["价格<100元。"]

    def test_char_by_char_speech(self):
        """逐字符 SPEECH token（最大拼接压力）→ 结果与整段一致。"""
        buf = SpeechSplitter(min_chunk_length=3)
        assert _feed_all(buf, "我很喜欢Python。") == "我很喜欢Python。"

    def test_leading_spaces_preserved(self):
        """行首空格是独立 SPEECH token → 拼接保留。"""
        buf = SpeechSplitter(min_chunk_length=2)
        result = _feed_raw(
            buf,
            [Token(TokenKind.SPEECH, " "), Token(TokenKind.SPEECH, " "),
             Token(TokenKind.SPEECH, "好"), Token(TokenKind.SPEECH, "。")],
        )
        assert result == ["  好。"]

    def test_real_tokenizer_integration(self):
        """真实 tokenizer 输出直接喂入 splitter（端到端）。"""
        tz = StreamTokenizer()
        toks = tz.feed("价格<100元。")
        toks.extend(tz.flush())
        buf = SpeechSplitter(min_chunk_length=3)
        assert _feed_raw(buf, toks) == ["价格<100元。"]


# ============================================================================
# Group 7: 其他 token 路由（分句器忽略带外 token）
# ============================================================================

class TestOutOfBandTokens:
    def test_code_markdown_meta_ignored(self):
        """CODE/MARKDOWN/META 由协调器路由，分句器忽略、不冲刷。"""
        buf = SpeechSplitter(min_chunk_length=3)
        assert buf.feed(Token(TokenKind.CODE, "x=1", lang="python")) == []
        assert buf.feed(Token(TokenKind.MARKDOWN, "内容")) == []
        assert buf.feed(Token(TokenKind.META, "开心", meta_kind="mood")) == []
        assert buf.feed(Token(TokenKind.SPEECH, "你好。")) == ["你好。"]
