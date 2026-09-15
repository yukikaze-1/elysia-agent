"""tags 标签注册表测试 — 纯数据定义 + emoji 收口。

对齐设计文档 `docs/superpowers/specs/archive/2026-08/2026-08-01-stream-parser-refactor-design.md`
行为对照表。每个用例钉死一项标签定义。
"""

import json
import re

import pytest

from core.text.tags import (
    CODE_CLOSE,
    CODE_OPEN_RE,
    FENCE_MARK,
    FENCE_MAX_INDENT,
    MARKDOWN_CLOSE,
    MARKDOWN_OPEN,
    META_TAGS,
    PAUSE_RE,
    PAUSE_TAIL_RE,
    STAGE_CN_CLOSE,
    STAGE_CN_OPEN,
    STAGE_CN_RE,
    STAGE_EN_WORDS,
    STAGE_K,
    STAGE_M,
    START_PREFIXES,
    is_emoji_only,
)

pytestmark = pytest.mark.unit


# ============================================================================
# Group 1: Emoji 收口
# ============================================================================

class TestIsEmojiOnly:
    def test_emoji_only_true(self):
        """纯 emoji 句（♪♪）→ True。"""
        assert is_emoji_only("♪♪") is True

    def test_mixed_text_false(self):
        """emoji 带中文（好的♪）→ False。"""
        assert is_emoji_only("好的♪") is False

    def test_empty_string_true(self):
        """空串视为纯 emoji（无文字）→ True。"""
        assert is_emoji_only("") is True

    def test_plain_text_false(self):
        """纯文字 → False。"""
        assert is_emoji_only("你好世界") is False


# ---- Emoji 序列边界（CODE_REVIEW_REPORT #46 缺口行 #12）----------------------
#
# 修复前 `_EMOJI_RE` 只是若干**单码点**区间，序列成分（ZWJ U+200D、变体选择符
# U+FE0F、keycap 组合符 U+20E3、区域指示符、tag 字符）全在区间外 →
# `👨‍👩‍👧‍👦` / `❤️` / `1️⃣` / `🇨🇳` / `🏳️‍🌈` 一律 False。
# 危害不是"少判一个 emoji"：`tts_adapter.stream_sentence` 的 is_emoji_only 闸门
# 放行后，Module/tts 的 `_EMOJI_RE`（同样只认单码点）会把基底码点剥掉、
# **只剩 ZWJ 串**（`👨‍👩‍👧‍👦` → `"\u200d\u200d\u200d"`），非空 → 真的发一次
# 合成请求 → 正是缺口表点名的 "TTS 400 / 分句错位" 风险。

# 正例：整句 = 一个或多个 emoji 序列（含修饰符 / 连接符 / 区域指示符 / tag 序列）
_EMOJI_ONLY_TRUE = [
    pytest.param("\U0001F600", id="single-u1f600"),
    pytest.param("\U0001F44D\U0001F3FD", id="skin-tone-modifier"),
    pytest.param(
        "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466",
        id="zwj-family",
    ),
    pytest.param("\u2764\uFE0F", id="heart-vs16"),
    pytest.param("\u2764", id="heart-bare-u2764"),
    pytest.param("1\uFE0F\u20E3", id="keycap-1"),
    pytest.param("#\uFE0F\u20E3", id="keycap-hash"),
    pytest.param("\U0001F1E8\U0001F1F3", id="regional-indicator-cn"),
    pytest.param("\U0001F3F3\uFE0F\u200D\U0001F308", id="rainbow-flag"),
    pytest.param(
        "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F",
        id="tag-sequence-scotland",
    ),
    pytest.param("\U0001FAE0", id="extended-a-u1fae0"),
    pytest.param("\U0001F44D\U0001F44D\U0001F44D", id="repeat-same"),
    pytest.param("\U0001F44D \U0001F389", id="two-emoji-space-between"),
    pytest.param("\U0001F3FD", id="lone-skin-tone-modifier"),
    pytest.param("\u266A\u266A", id="musical-notes-existing"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace-spaces"),
    pytest.param("\t\n ", id="whitespace-mixed"),
]

# 反例 A：含真实文本字符 → False（"闸门不得放宽成含 emoji 即跳过"）
_NON_EMOJI_TEXT_FALSE = [
    pytest.param("\U0001F44D \u597D", id="emoji-space-cjk"),
    pytest.param("hello", id="ascii-word"),
    pytest.param("\u3002", id="ideographic-full-stop"),
    pytest.param("\U0001F44Da", id="emoji-ascii-letter"),
    pytest.param("<#0.5#>", id="pause-tag"),
    pytest.param("\U0001F44D\uFF01", id="emoji-cjk-punct"),
    pytest.param("1", id="bare-digit"),
    pytest.param("#", id="bare-hash"),
    pytest.param("(\u7728\u773C)", id="stage-cn-paren"),
]

# 反例 B：只有序列修饰符、没有基底码点 —— 修饰符不构成 emoji 序列（见类文档）
_LONE_MODIFIER_FALSE = [
    pytest.param("\u200D", id="lone-zwj"),
    pytest.param("\uFE0F", id="lone-vs16"),
    pytest.param("\u20E3", id="lone-keycap-combining"),
]

# 反例 C：代理码点（不可达，仅记录边界 —— 见 TestSurrogateBoundary）
_SURROGATE_FALSE = [
    pytest.param("\ud83d\ude00", id="literal-surrogate-pair"),
    pytest.param("\ud83d", id="literal-lone-surrogate"),
]

_EMOJI_ONLY_FALSE = _NON_EMOJI_TEXT_FALSE + _LONE_MODIFIER_FALSE + _SURROGATE_FALSE

# 独立判据：非 emoji、非空白、非序列修饰符的"真文本"字符类（与生产正则**分写**，
# 只用于表自检——避免"用被测函数验证被测表"的循环论证）。
_TEXT_RESIDUE_RE = (
    r"[^"
    "\U0001F300-\U0001F9FF"
    "\U00002600-\U000027BF"
    "\U00002300-\U000023FF"
    "\U0001F1E6-\U0001F1FF"
    "\U0001FA70-\U0001FAFF"
    "\uFE0F\u200D\u20E3"
    "\U000E0020-\U000E007F"
    "\U0001F3FB-\U0001F3FF"
    r"\s"
    "]"
)


class TestIsEmojiOnlySequences:
    @pytest.mark.parametrize("text", _EMOJI_ONLY_TRUE)
    def test_sequences_true(self, text):
        """emoji 序列（ZWJ / 变体选择符 / 肤色 / keycap / 区域指示符 / tag）→ True。"""
        assert is_emoji_only(text) is True

    @pytest.mark.parametrize("text", _EMOJI_ONLY_FALSE)
    def test_non_emoji_content_false(self, text):
        """含非 emoji、非空白字符 → False（闸门不得被放宽成"含 emoji 即跳过"）。"""
        assert is_emoji_only(text) is False

    def test_table_not_vacuous(self):
        """参数表自检：正反例非空、互不相交，且反例 A 确实含"真文本"残留。

        "反例真的含非 emoji 字符"用**独立**字符类（``_TEXT_RESIDUE_RE``）现算，
        不复用 ``is_emoji_only``——否则自检与被测函数同源，等于没检。
        """
        positives = [p.values[0] for p in _EMOJI_ONLY_TRUE]
        negatives = [p.values[0] for p in _EMOJI_ONLY_FALSE]
        assert positives and negatives
        assert len(set(positives)) == len(positives)
        assert len(set(negatives)) == len(negatives)
        assert set(positives) & set(negatives) == set()
        for param in _NON_EMOJI_TEXT_FALSE:
            text = param.values[0]
            assert re.search(_TEXT_RESIDUE_RE, text), f"{text!r} 缺真文本残留"
        # 修饰符反例必须**只有**修饰符，否则它测不到"修饰符无基底"这条边界
        for param in _LONE_MODIFIER_FALSE:
            text = param.values[0]
            assert not re.search(_TEXT_RESIDUE_RE, text), f"{text!r} 并非纯修饰符"
            assert all(
                ch in "\u200D\uFE0F\u20E3" for ch in text
            ), f"{text!r} 含非修饰符字符"
        for param in _SURROGATE_FALSE:
            for ch in param.values[0]:
                assert 0xD800 <= ord(ch) <= 0xDFFF, "代理反例必须是代理码点"

    def test_long_input_terminates(self):
        """10 万字符纯 emoji 序列：终止、不爆栈、判定仍为 True。"""
        assert is_emoji_only("\U0001F600" * 25_000) is True
        assert is_emoji_only(
            "\U0001F468\u200D\U0001F469\u200D\U0001F467" * 10_000
        ) is True
        # 末尾掺一个汉字 → 必须 False（长输入不得退化成"总是 True"）
        assert is_emoji_only("\U0001F600" * 25_000 + "\u597D") is False


class TestSurrogateBoundary:
    """代理对（surrogate pair）边界 —— 记录不可达，不为它扭曲正则。

    结论：**成对代理在管道里不可达**（证据见本类的三个用例与
    ``core/text/README.md``「换行与 emoji 边界」）。

    1. WS 入口 ``core/input/ws_handler.py::_run_ws_message_loop`` 用
       ``json.loads(msg.data)``，而 ``msg.data`` 是 aiohttp 已按 UTF-8 解码的 str；
       UTF-8 编码**禁止**代理码点（``"\\ud83d".encode("utf-8")`` → UnicodeEncodeError）。
    2. ``json.loads('"\\ud83d\\ude00"')`` 会把转义对**合并**成单个 U+1F600，
       线上实际到达 ``is_emoji_only`` 的是真字符而非两个代理码点。
    3. LLM SDK 的增量内容同样是已解码 str（不产生代理码点）。

    只有**畸形** JSON（单独 ``"\\ud83d"`` 转义）能构造出裸代理；它不是 emoji、
    也不是合法文本，故保持 False —— 放行一次无意义合成请求无害，把乱码当 emoji
    静默吞掉反而会掩盖上游解码问题。
    """

    def test_paired_surrogates_are_not_emoji(self):
        """字面代理对 → False（不可达；记录边界，避免正则被代理区间污染）。"""
        assert is_emoji_only("\ud83d\ude00") is False

    def test_lone_surrogate_is_not_emoji(self):
        """裸代理 → False（同上：乱码不是 emoji）。"""
        assert is_emoji_only("\ud83d") is False

    def test_real_char_after_json_decode_is_emoji(self):
        """JSON 解码后的真字符（U+1F600）→ True —— 这才是线上实际形态。"""
        decoded = json.loads('"\\ud83d\\ude00"')
        assert decoded == "\U0001F600"
        assert is_emoji_only(decoded) is True


# ============================================================================
# Group 2: PAUSE（TTS 停顿标签）
# ============================================================================

class TestPauseRegex:
    def test_complete_tag_matches(self):
        """<#0.5#> / <#1#> 完整标签匹配。"""
        assert PAUSE_RE.fullmatch("<#0.5#>") is not None
        assert PAUSE_RE.fullmatch("<#1#>") is not None

    def test_non_numeric_no_match(self):
        """<#abc#> 不含数字 → 不匹配。"""
        assert PAUSE_RE.fullmatch("<#abc#>") is None

    def test_tail_hold_mid_tag(self):
        """尾部残缺 <#0.5 应持有（等待数字补全）。"""
        assert PAUSE_TAIL_RE.search("<#0.5") is not None

    def test_tail_hold_with_trailing_hash(self):
        """<#0.5# 前缀态必须持有——否则 `<#0.5#|>` 切分点立即释放为 SPEECH 泄漏。"""
        assert PAUSE_TAIL_RE.search("<#0.5#") is not None

    def test_tail_hold_bare_start(self):
        """尾部仅 <# 也应持有（等待数字）。"""
        assert PAUSE_TAIL_RE.search("<#") is not None

    def test_complete_tag_not_held(self):
        """完整标签 <#0.5#> 不属于持有态（已闭合，直接作为 PAUSE 输出）。"""
        assert PAUSE_TAIL_RE.search("<#0.5#>") is None

    def test_non_digit_releases(self):
        """<#100元 遇非数字字符（元）→ 不持有，释放为 SPEECH。"""
        assert PAUSE_TAIL_RE.search("<#100元") is None


# ============================================================================
# Group 3: STAGE_CN（中文舞台指示）
# ============================================================================

class TestStageCnRegex:
    def test_fullwidth_matches(self):
        """全角（眨眼）→ 匹配。"""
        assert STAGE_CN_RE.search("（眨眼）") is not None

    def test_halfwidth_matches(self):
        """半角(眨眼)→ 匹配（对齐现状 #6）。"""
        assert STAGE_CN_RE.search("(眨眼)") is not None

    def test_halfwidth_no_cjk_no_match(self):
        """半角 (laughs) 无 CJK → 不匹配（行为表 #10 保留）。"""
        assert STAGE_CN_RE.search("(laughs)") is None

    def test_fullwidth_no_cjk_no_match(self):
        """全角括号包裹纯英文（如（hello））无 CJK → 不匹配。

        注：brief 原文「不匹配（纯英文）」中「纯英文」三字本身是 CJK，
        正则按设计（含任一 CJK 即匹配）会命中——此处用真正的纯英文内容验证意图。
        """
        assert STAGE_CN_RE.search("（hello）") is None

    def test_internal_punctuation_matches(self):
        """（我有点，犹豫。）含内部标点 → 整体匹配（#6a span 保护素材）。"""
        assert STAGE_CN_RE.search("（我有点，犹豫。）") is not None

    def test_open_close_sets(self):
        """开/闭括号字符集（半角 + 全角）。"""
        assert STAGE_CN_OPEN == frozenset({"（", "("})
        assert STAGE_CN_CLOSE == frozenset({"）", ")"})

    def test_hold_windows(self):
        """持有窗口常量：M=64（CJK 上限）、K=16（英文早退窗）。"""
        assert STAGE_M == 64
        assert STAGE_K == 16


# ============================================================================
# Group 4: STAGE_EN_WORDS（英文语气词表，文档化用途）
# ============================================================================

class TestStageEnWords:
    def test_required_words_present(self):
        """laughs / sighs / clears throat 必须在词表内。"""
        assert "laughs" in STAGE_EN_WORDS
        assert "sighs" in STAGE_EN_WORDS
        assert "clears throat" in STAGE_EN_WORDS

    def test_non_empty(self):
        """词表非空（从旧 _TONE_TAG_RE 提取）。"""
        assert STAGE_EN_WORDS


# ============================================================================
# Group 5: META_TAGS（心情 / 内心独白）
# ============================================================================

class TestMetaTags:
    def test_has_mood_and_inner_voice(self):
        """kind 含 mood 与 inner_voice，open/close/emit 三元组。"""
        assert "mood" in META_TAGS
        assert "inner_voice" in META_TAGS
        assert META_TAGS["mood"] == ("<mood>", "</mood>", "mood")
        assert META_TAGS["inner_voice"] == ("<inner_voice>", "</inner_voice>", "inner_voice")

    def test_open_close_emit_non_empty(self):
        """每对 open/close/emit 均为非空字符串。"""
        for open_tag, close_tag, emit in META_TAGS.values():
            assert open_tag
            assert close_tag
            assert emit


# ============================================================================
# Group 6: MARKDOWN / CODE 结构化块
# ============================================================================

class TestMarkdownCode:
    def test_markdown_tags(self):
        """Markdown 块开/闭标记。"""
        assert MARKDOWN_OPEN == "<|markdown|>"
        assert MARKDOWN_CLOSE == "</|markdown|>"

    def test_code_tags(self):
        """CODE 标签开（正则带 lang）/闭。"""
        assert CODE_CLOSE == "</|code|>"
        m = CODE_OPEN_RE.fullmatch("<|code|python>")
        assert m is not None and m.group(1) == "python"
        m2 = CODE_OPEN_RE.fullmatch("<|code|>")
        assert m2 is not None and m2.group(1) == ""

    def test_code_lang_restricted_charset(self):
        """lang 受限字符集 [A-Za-z0-9_+.-]*——含空格不匹配。"""
        assert CODE_OPEN_RE.fullmatch("<|code|py thon>") is None

    def test_fence_constants(self):
        """围栏标记与最大缩进（对齐 CommonMark ≤3）。"""
        assert FENCE_MARK == "```"
        assert FENCE_MAX_INDENT == 3


# ============================================================================
# Group 7: START_PREFIXES（起始模式前缀，tokenizer 持有判定）
# ============================================================================

class TestStartPrefixes:
    def test_contains_all_families(self):
        """覆盖全部起始模式前缀：PAUSE / META / 结构化块 / STAGE_CN / 围栏。"""
        assert "<#" in START_PREFIXES
        assert "<m" in START_PREFIXES
        assert "<i" in START_PREFIXES
        assert "<|" in START_PREFIXES
        assert "（" in START_PREFIXES
        assert "(" in START_PREFIXES
        assert "```" in START_PREFIXES

    def test_bare_less_than_is_prefix_of_some(self):
        """裸 `<` 是某起始模式的前缀 → 可持有（等待 <# / <m / <i / <|）。"""
        assert any(p.startswith("<") for p in START_PREFIXES)

    def test_less_than_digit_not_prefix(self):
        """`<1` 不构成任何起始模式前缀 → 释放（价格<100元 不误扣，设计测试 5）。"""
        assert not any(p.startswith("<1") for p in START_PREFIXES)

    def test_backtick_prefix_of_fence(self):
        """单个反引号是围栏标记前缀 → 可持有。"""
        assert any(p.startswith("`") for p in START_PREFIXES)
