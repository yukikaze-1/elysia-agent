"""BlockFilter tests — filtering, flush safety, parts ordering."""

import pytest
from core.text.block_filter import BlockFilter, build_parts_with_blocks
from core.schema.part import TextPart

pytestmark = pytest.mark.unit


# ============================================================================
# Group 1: Basic filtering
# ============================================================================

class TestBasicFiltering:
    def test_code_content_filtered(self):
        """代码块内容不进 TTS 文本。"""
        bf = BlockFilter()
        result = bf.feed("你好。<|code|python>print('x')</|code|>再见。")
        assert result == "你好。"
        assert bf.flush() == "再见。"

    def test_markdown_content_filtered(self):
        """Markdown 块内容不进 TTS 文本。"""
        bf = BlockFilter()
        result = bf.feed("前面<|markdown|>表格</|markdown|>后面")
        assert result == "前面"
        assert bf.flush() == "后面"

    def test_plain_text_passes_through(self):
        """无块标记的纯文本原样通过。"""
        bf = BlockFilter()
        result = bf.feed("你好世界！")
        result += bf.flush()
        assert result == "你好世界！"

    def test_empty_input(self):
        """空输入返回空字符串。"""
        bf = BlockFilter()
        assert bf.feed("") == ""


# ============================================================================
# Group 2: Flush safety (未闭合块不泄漏)
# ============================================================================

class TestFlushSafety:
    def test_unclosed_code_no_leak(self):
        """未闭合的代码块内容在 flush 时丢弃。"""
        bf = BlockFilter()
        assert bf.feed("<|code|python>secret") == ""
        assert bf.flush() == ""

    def test_unclosed_markdown_no_leak(self):
        """未闭合的 markdown 块内容在 flush 时丢弃。"""
        bf = BlockFilter()
        assert bf.feed("<|markdown|>hidden") == ""
        assert bf.flush() == ""

    def test_trailing_partial_tag_trimmed(self):
        """尾部半截起始标记 <| 被丢弃。"""
        bf = BlockFilter()
        result = bf.feed("hello<|cod")
        result += bf.flush()
        assert result == "hello"

    def test_flush_with_no_pending(self):
        """空 buffer flush 返回空。"""
        bf = BlockFilter()
        assert bf.flush() == ""


# ============================================================================
# Group 3: Cross-chunk marker handling
# ============================================================================

class TestCrossChunk:
    def test_marker_split_across_chunks(self):
        """标记跨 chunk 边界时正确处理。"""
        bf = BlockFilter()
        result = ""
        result += bf.feed("你好。<|co")
        result += bf.feed("de|python>")
        result += bf.feed("print('x')</|co")
        result += bf.feed("de|>再见。")
        result += bf.flush()
        assert result == "你好。再见。"

    def test_end_marker_split_across_chunks(self):
        """结束标记跨 chunk 时正确处理。"""
        bf = BlockFilter()
        result = bf.feed("<|code|>x</|co")
        result += bf.feed("de|>hello")
        result += bf.flush()
        assert result == "hello"


# ============================================================================
# Group 4: Tag matching
# ============================================================================

class TestTagMatching:
    def test_mismatched_end_tags(self):
        """</|code|> 不关闭 <|markdown|>。"""
        bf = BlockFilter()
        result = bf.feed(
            "<|markdown|>text</|code|>still hidden</|markdown|>你好。"
        )
        result += bf.flush()
        assert result == "你好。"

    def test_markdown_nested_code_behavior(self):
        """跨类型嵌套：进入 markdown 块后只在 buf 中找 </|markdown|>，
        不会扫描新的起始标记。<|code|> 被当作块内容丢弃。
        整个 markdown 块内容（含内嵌的 <|code|>inner</|code|>still_outer）
        在找到 </|markdown|> 后全部丢弃。"""
        bf = BlockFilter()
        result = bf.feed(
            "<|markdown|>outer<|code|>inner</|code|>still_outer</|markdown|>done。"
        )
        result += bf.flush()
        # 整个 markdown 块被丢弃，"done。" 是唯一通过的文本
        assert result == "done。"


# ============================================================================
# Group 5: Reset
# ============================================================================

class TestReset:
    def test_reset_clears_state(self):
        """reset 后状态干净，可以重新使用。"""
        bf = BlockFilter()
        bf.feed("<|code|>x")
        bf.reset()
        result = bf.feed("hello")
        result += bf.flush()
        assert result == "hello"

    def test_reset_clears_mode(self):
        """reset 清除未闭合的块模式。"""
        bf = BlockFilter()
        bf.feed("<|markdown|>")
        assert bf._mode == "markdown"
        bf.reset()
        assert bf._mode is None
        result = bf.feed("safe")
        result += bf.flush()
        assert result == "safe"


# ============================================================================
# Group 6: build_parts_with_blocks ordering
# ============================================================================

class TestPartsOrdering:
    def test_parts_preserve_order_simple(self):
        """单块+单句交替：块在原文位置。"""
        text = (
            "你好。"
            "<|markdown|>表格</|markdown|>"
            "再见。"
        )
        parts, content_type = build_parts_with_blocks(
            text,
            ["你好。", "再见。"],
        )
        assert parts == [
            TextPart(content="你好。"),
            TextPart(content="表格"),
            TextPart(content="再见。"),
        ]
        assert content_type == {"markdown": [1], "code": []}

    def test_parts_preserve_order_complex(self):
        """多块多句、块在开头/结尾/中间。"""
        text = (
            "<|code|python>print(1)</|code|>"
            "你好。"
            "<|markdown|>note</|markdown|>"
            "再见。"
            "<|code|>end</|code|>"
        )
        parts, content_type = build_parts_with_blocks(
            text,
            ["你好。", "再见。"],
        )
        assert parts == [
            TextPart(content="print(1)"),
            TextPart(content="你好。"),
            TextPart(content="note"),
            TextPart(content="再见。"),
            TextPart(content="end"),
        ]
        assert content_type == {"markdown": [2], "code": [0, 4]}

    def test_parts_unmatched_sentences_appended(self):
        """句子在原文中找不到时兜底追加到末尾。"""
        parts, _ = build_parts_with_blocks(
            "some text without matches",
            ["你好。", "再见。"],
        )
        # 没有块标记时直接返回句子列表
        assert parts == [
            TextPart(content="你好。"),
            TextPart(content="再见。"),
        ]

    def test_parts_no_blocks(self):
        """无块标记时直接返回句子列表。"""
        parts, content_type = build_parts_with_blocks(
            "你好。今天天气真好。",
            ["你好。", "今天天气真好。"],
        )
        assert parts == [
            TextPart(content="你好。"),
            TextPart(content="今天天气真好。"),
        ]
        assert content_type == {"markdown": [], "code": []}
