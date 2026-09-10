"""tags 标签注册表测试 — 纯数据定义 + emoji 收口。

对齐设计文档 `docs/superpowers/specs/archive/2026-08/2026-08-01-stream-parser-refactor-design.md`
行为对照表。每个用例钉死一项标签定义。
"""

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
