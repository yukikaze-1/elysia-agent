"""parts 构建测试 — after_sentence_idx 交错 + public_text 重建。

对齐设计文档 `docs/superpowers/specs/archive/2026-08/2026-08-01-stream-parser-refactor-design.md`
测试策略 #9（四条交错语料钉死语义）与 #12（public_text 无 META 泄漏）。

输出形状与旧正则重解析路径等价：
- parts: TextPart 列表，句子与块交错排列
- content_type: {"markdown": [...], "code": [...]}（块在 parts 中的索引）
"""

import pytest

from core.schema.part import TextPart
from core.text.parts import build_parts, rebuild_public_text
from core.text.tokenizer import StreamTokenizer, Token, TokenKind

pytestmark = pytest.mark.unit


def _block(kind: str, content: str, after: int, lang: str = "") -> tuple[str, str, str, int]:
    """构造 BlockRef：(kind, content, lang, after_sentence_idx)。"""
    return (kind, content, lang, after)


# ============================================================================
# Group 1: 四条交错语料（测试策略 #9，钉死 after_sentence_idx 语义）
# ============================================================================

class TestInterleaveCorpora:
    def test_corpus1_sentence_with_inline_pause(self):
        """块前句子含内嵌标签：块插在该句之后（字符偏移式误插被钉死）。

        持久化 TextPart 剥 PAUSE（显示剥离），块位置不变（after_sentence_idx 语义）。
        """
        parts, content_type = build_parts(
            ["好的。<#0.5#>"],
            [_block("markdown", "表格", after=1)],
        )
        assert parts == [
            TextPart(content="好的。"),
            TextPart(content="表格"),
        ]
        assert content_type == {"markdown": [1], "code": []}

    def test_corpus2_short_sentence_held(self):
        """块前短句（min_chunk 暂留，完成计数=1）：块插在句**后**。"""
        parts, content_type = build_parts(
            ["好。"],
            [_block("markdown", "表格", after=1)],
        )
        assert parts == [
            TextPart(content="好。"),
            TextPart(content="表格"),
        ]
        assert content_type == {"markdown": [1], "code": []}

    def test_corpus3_sentence_unemitted(self):
        """句中块（句未落地，after_sentence_idx=0）：块落首句之前。"""
        parts, content_type = build_parts(
            [],
            [_block("code", "x=1", after=0)],
        )
        assert parts == [TextPart(content="x=1")]
        assert content_type == {"markdown": [], "code": [0]}

    def test_corpus4_sentence_with_after_1(self):
        """段尾围栏（"第一段。"完成计数=1）：块插在该句之后。"""
        parts, content_type = build_parts(
            ["第一段。"],
            [_block("code", "print(1)", after=1)],
        )
        assert parts == [
            TextPart(content="第一段。"),
            TextPart(content="print(1)"),
        ]
        assert content_type == {"markdown": [], "code": [1]}


# ============================================================================
# Group 2: 多块交错（相对原始句子列表定位，插入后不重索引）
# ============================================================================

class TestMultiBlock:
    def test_two_blocks_same_slot_in_list_order(self):
        """同 after_sentence_idx 的多块按 blocks 列表顺序排列。"""
        parts, content_type = build_parts(
            ["s1。", "s2。"],
            [
                _block("markdown", "A", after=1),
                _block("code", "B", after=1),
            ],
        )
        assert parts == [
            TextPart(content="s1。"),
            TextPart(content="A"),
            TextPart(content="B"),
            TextPart(content="s2。"),
        ]
        assert content_type == {"markdown": [1], "code": [2]}

    def test_blocks_across_slots(self):
        """块分布在不同插入点：after=0 / after=1 / after=2。"""
        parts, content_type = build_parts(
            ["s1。", "s2。"],
            [
                _block("code", "C", after=0),
                _block("markdown", "A", after=1),
                _block("markdown", "B", after=2),
            ],
        )
        assert parts == [
            TextPart(content="C"),
            TextPart(content="s1。"),
            TextPart(content="A"),
            TextPart(content="s2。"),
            TextPart(content="B"),
        ]
        assert content_type == {"markdown": [2, 4], "code": [0]}

    def test_after_idx_beyond_sentences_appended(self):
        """after_sentence_idx >= len(all_sentences) → 追加末尾。"""
        parts, content_type = build_parts(
            ["s1。"],
            [_block("code", "tail", after=5)],
        )
        assert parts == [
            TextPart(content="s1。"),
            TextPart(content="tail"),
        ]
        assert content_type == {"markdown": [], "code": [1]}

    def test_mixed_after_and_clamp(self):
        """大 after + 常规 after 混排：大 after 钳制到末尾。"""
        parts, content_type = build_parts(
            ["s1。", "s2。"],
            [
                _block("markdown", "mid", after=1),
                _block("code", "end", after=9),
            ],
        )
        assert parts == [
            TextPart(content="s1。"),
            TextPart(content="mid"),
            TextPart(content="s2。"),
            TextPart(content="end"),
        ]
        assert content_type == {"markdown": [1], "code": [3]}

    def test_negative_after_clamped_to_front(self):
        """负数 after_sentence_idx（正常为计数，防御）钳制到首句之前。"""
        parts, content_type = build_parts(
            ["s1。"],
            [_block("code", "x", after=-1)],
        )
        assert parts == [
            TextPart(content="x"),
            TextPart(content="s1。"),
        ]
        assert content_type == {"markdown": [], "code": [0]}

    def test_no_blocks_returns_sentences(self):
        """无块时直接返回句子列表（对齐旧组件行为）。"""
        parts, content_type = build_parts(
            ["你好。", "今天天气真好。"],
            [],
        )
        assert parts == [
            TextPart(content="你好。"),
            TextPart(content="今天天气真好。"),
        ]
        assert content_type == {"markdown": [], "code": []}

    def test_content_type_keys_always_present(self):
        """content_type 恒含 markdown/code 两键（对齐旧实现）。"""
        _, content_type = build_parts(
            ["s1。"],
            [_block("code", "x", after=1)],
        )
        assert set(content_type.keys()) == {"markdown", "code"}

    def test_sentence_text_preserved_exact(self):
        """持久化 TextPart 剥 PAUSE/语气词，STAGE_CN 保留（显示剥离）。"""
        parts, _ = build_parts(
            ["（眨眼）好的。<#1#>"],
            [_block("code", "x", after=1)],
        )
        assert parts[0] == TextPart(content="（眨眼）好的。")

    def test_sentence_strips_tone_word_and_pause(self):
        """持久化剥英文语气词 (sighs) + PAUSE；拼音标注 (chu3li3) 保留。"""
        parts, _ = build_parts(
            ["不过——(sighs) 好的。<#0.5#> 处理(chu3li3)。"],
            [],
        )
        assert parts[0].content == "不过—— 好的。 处理(chu3li3)。"


# ============================================================================
# Group 3: 块内容（对齐旧 _parse_blocks 的 .strip()）
# ============================================================================

class TestBlockContent:
    def test_block_content_stripped(self):
        """块内容去首尾空白（旧 _parse_blocks 行为：content.strip()）。"""
        parts, _ = build_parts(
            ["s1。"],
            [_block("code", "  print(1)\n", after=1)],
        )
        assert parts == [
            TextPart(content="s1。"),
            TextPart(content="print(1)"),
        ]


# ============================================================================
# Group 4: rebuild_public_text（token 流按序拼接，去 META）
# ============================================================================

class TestRebuildPublicText:
    def test_mid_sentence_code_block_repositioned(self):
        """句中 <|code|> 块 → 按 token 序还原原文位置（测试策略 #12）。"""
        tokens = [
            Token(TokenKind.SPEECH, "看这个"),
            Token(TokenKind.CODE, "x=1"),
            Token(TokenKind.SPEECH, "示例"),
        ]
        assert rebuild_public_text(tokens) == "看这个x=1示例"

    def test_pause_preserved_as_is(self):
        """PAUSE 原样保留（public_text 只去 META 与代码块标记，spec 279）。"""
        tokens = [
            Token(TokenKind.SPEECH, "好的。"),
            Token(TokenKind.PAUSE, "<#0.5#>"),
            Token(TokenKind.SPEECH, "继续"),
        ]
        assert rebuild_public_text(tokens) == "好的。<#0.5#>继续"

    def test_stage_cn_kept(self):
        """STAGE_CN（中文舞台指示）原样保留。"""
        tokens = [
            Token(TokenKind.STAGE_CN, "（眨眼）"),
            Token(TokenKind.SPEECH, "好的。"),
        ]
        assert rebuild_public_text(tokens) == "（眨眼）好的。"

    def test_meta_dropped(self):
        """META（<mood>/<inner_voice>）剔除，前后语音连续。"""
        tokens = [
            Token(TokenKind.SPEECH, "你好"),
            Token(TokenKind.META, "开心", meta_kind="mood"),
            Token(TokenKind.SPEECH, "世界"),
            Token(TokenKind.META, "思考中", meta_kind="inner_voice"),
            Token(TokenKind.SPEECH, "！"),
        ]
        assert rebuild_public_text(tokens) == "你好世界！"

    def test_fence_code_content_raw_kept(self):
        """围栏块内容（含换行）按 token 原样插回——重建不剥空白。"""
        tokens = [
            Token(TokenKind.SPEECH, "第一段。\n"),
            Token(TokenKind.CODE, "print(1)\n", lang="python"),
            Token(TokenKind.SPEECH, "结束。"),
        ]
        assert rebuild_public_text(tokens) == "第一段。\nprint(1)\n结束。"

    def test_from_real_tokenizer(self):
        """真实 tokenizer 产物 → rebuild_public_text 还原（端到端）。"""
        tz = StreamTokenizer()
        toks: list[Token] = []
        for p in ["看这个<|code|>x=1</|", "code|>示例好的。<#0.5#>继续"]:
            toks.extend(tz.feed(p))
        toks.extend(tz.flush())
        assert rebuild_public_text(toks) == "看这个x=1示例好的。<#0.5#>继续"

    def test_empty_tokens(self):
        assert rebuild_public_text([]) == ""
