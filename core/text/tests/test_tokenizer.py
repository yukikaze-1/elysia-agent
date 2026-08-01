"""tokenizer 结构感知切分测试 — 跨 chunk 不变式（核心）+ 每类标签行为。

对齐设计文档 `docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md`
「Tokenizer 受保护扫描的持有上界」「围栏检测细则」及测试策略 1/2/5/7/10/11。

核心不变式（property test）：对任意输入串 S，在任意字符边界切分为 delta 序列，
feed 拆分的 token 序列必须 == 一次 feed 的序列。
"""

import random

import pytest

from core.text.tokenizer import StreamTokenizer, Token, TokenKind


pytestmark = pytest.mark.unit


def _feed_split(pieces):
    """依次 feed 各 delta，再 flush，返回完整 token 序列。"""
    tz = StreamTokenizer()
    toks: list[Token] = []
    for p in pieces:
        toks.extend(tz.feed(p))
    toks.extend(tz.flush())
    return toks


def _whole(s: str) -> list[Token]:
    return _feed_split([s])


# ============================================================================
# Group 0: 核心不变式（property test）
# ============================================================================

# 语料覆盖：PAUSE 跨 chunk、含标点 STAGE_CN、围栏（行首/缩进/长短/未闭合）、
# 字面 <、行中反引号、META（含空/未闭合）、英文语气、反引号前缀跨 chunk、
# 半截 PAUSE、K/M 窗口、块标签（code/markdown/嵌套/未闭合）。
PROPERTY_CORPUS = [
    "<#0.5#>好的，等一下<#1#>然后说",
    "（我有点，犹豫。）",
    "```python\n代码\n```",
    "价格<100元",
    "看 ```inline``` 示例",
    "你好<mood>开心</mood>世界",
    "(laughs)",
    "\n```python",
    "<#0.5#>",
    # ---- 扩展：围栏 / 块 / 未闭合 / 空标签 / 行首 ----
    "好的。\n```python\nprint(1)\n```",
    "  ```python\n缩进代码\n```",
    "````python\n```不终止\n````",
    "你好<mood></mood>世界",
    "<inner_voice>思考中</inner_voice>然后说",
    "看这个<|code|>x=1</|code|>示例",
    "<|markdown|>内容</|markdown|>",
    "（眨眼）",
    "你好<mood>开心",
    "<inner_voice>思考中",
    "好的<#",
    "<#100元",
    "价格<",
    "``",
    "`",
    "````",
    "第一段。\n```python\n代码\n```",
    "（长中文指示没有闭口",
    "(" + "a" * 20,  # K 窗口早退（无 CJK）
    "（" + "啊" * 70,  # M 窗口回落（含 CJK）
]

_POOL = (
    "中文abcXYZ123，。！？\n ()（）<>#`|/_—中内心情说"
)


class TestPropertyInvariant:
    """feed 拆分的 token 序列 == 一次 feed 的序列。"""

    def test_every_split_point(self):
        """语料 × 每个单切分点（含 0 与 len 两端）。"""
        for s in PROPERTY_CORPUS:
            expected = _whole(s)
            for i in range(len(s) + 1):
                got = _feed_split([s[:i], s[i:]])
                assert got == expected, f"S={s!r} split={i!r}\n  whole={expected}\n  got={got}"

    def test_char_by_char(self):
        """逐字符喂入同样满足不变式。"""
        for s in PROPERTY_CORPUS:
            expected = _whole(s)
            got = _feed_split(list(s))
            assert got == expected, f"S={s!r} char-by-char\n  whole={expected}\n  got={got}"

    def test_random_multisplit(self):
        """随机字符串 × 随机 1-4 切分点，固定种子可复现。"""
        rng = random.Random(20260801)
        for _ in range(80):
            s = "".join(rng.choice(_POOL) for _ in range(rng.randint(0, 36)))
            expected = _whole(s)
            n = len(s)
            cuts = sorted(set(rng.randint(0, n) for _ in range(rng.randint(1, 4))))
            pieces = []
            prev = 0
            for cut in cuts:
                pieces.append(s[prev:cut])
                prev = cut
            pieces.append(s[prev:])
            got = _feed_split(pieces)
            assert got == expected, f"S={s!r} pieces={pieces!r}\n  whole={expected}\n  got={got}"


# ============================================================================
# Group 1: PAUSE（TTS 停顿标签）
# ============================================================================

class TestPause:
    def test_complete_single_token(self):
        assert _whole("<#0.5#>") == [Token(TokenKind.PAUSE, "<#0.5#>")]

    def test_inline_pause_not_cut(self):
        toks = _whole("<#0.5#>好的，等一下<#1#>然后说")
        assert toks == [
            Token(TokenKind.PAUSE, "<#0.5#>"),
            Token(TokenKind.SPEECH, "好的，等一下"),
            Token(TokenKind.PAUSE, "<#1#>"),
            Token(TokenKind.SPEECH, "然后说"),
        ]

    def test_close_hash_split(self):
        """闭口 # 后、> 前切分 → 仍整体成 PAUSE。"""
        tz = StreamTokenizer()
        assert tz.feed("<#0.5#") == []
        assert tz.feed(">") == [Token(TokenKind.PAUSE, "<#0.5#>")]

    def test_non_digit_releases_as_speech(self):
        """<#100元 遇非数字字符 → 释放为 SPEECH（不扣住、不误判）。"""
        toks = _whole("价格<#100元")
        assert all(t.kind == TokenKind.SPEECH for t in toks)
        assert "".join(t.text for t in toks) == "价格<#100元"

    def test_half_pause_flush_stripped(self):
        """半截 <#0.5 在 flush 时剥离（不输出）。"""
        assert _whole("好的<#0.5") == [Token(TokenKind.SPEECH, "好的")]

    def test_bare_start_flush_stripped(self):
        assert _whole("好的<#") == [Token(TokenKind.SPEECH, "好的")]


# ============================================================================
# Group 2: STAGE_CN（中文舞台指示）
# ============================================================================

class TestStageCn:
    def test_fullwidth_single_token(self):
        assert _whole("（眨眼）") == [Token(TokenKind.STAGE_CN, "（眨眼）")]

    def test_halfwidth_single_token(self):
        """半角 (眨眼) 同样入 STAGE_CN（对齐现状 #6）。"""
        assert _whole("(眨眼)") == [Token(TokenKind.STAGE_CN, "(眨眼)")]

    def test_internal_punctuation_kept(self):
        """（我有点，犹豫。）含内部标点 → span 整体保护（#6a）。"""
        assert _whole("（我有点，犹豫。）") == [
            Token(TokenKind.STAGE_CN, "（我有点，犹豫。）")
        ]

    def test_english_tone_released(self):
        """(laughs) 闭合内容无 CJK → 回落 SPEECH 保留（行为表 #10）。"""
        assert _whole("(laughs)") == [Token(TokenKind.SPEECH, "(laughs)")]

    def test_k_window_early_exit(self):
        """纯英文无闭口超 K=16 → 提前回落 SPEECH，不延迟。"""
        toks = _whole("(" + "a" * 20)
        assert toks == [
            Token(TokenKind.SPEECH, "(" + "a" * 16),
            Token(TokenKind.SPEECH, "a" * 4),
        ]

    def test_m_window_cjk_fallback(self):
        """含 CJK 无闭口超 M=64 → 回落 SPEECH。"""
        toks = _whole("（" + "啊" * 70)
        assert toks == [
            Token(TokenKind.SPEECH, "（" + "啊" * 64),
            Token(TokenKind.SPEECH, "啊" * 6),
        ]

    def test_cjk_within_k_not_truncated(self):
        """CJK 在 K 内不截断——(aaaaa眨) 闭合时内容含 CJK → STAGE_CN。"""
        s = "(" + "a" * 12 + "眨)"
        assert _whole(s) == [Token(TokenKind.STAGE_CN, s)]

    def test_unclosed_flush_fallback(self):
        """未闭口（长中文指示 → flush 回落 SPEECH（不完整括号按文本显示）。"""
        assert _whole("（长中文指示没有闭口") == [
            Token(TokenKind.SPEECH, "（长中文指示没有闭口")
        ]


# ============================================================================
# Group 3: META（<mood> / <inner_voice>）
# ============================================================================

class TestMeta:
    def test_mood_mid_sentence_stripped(self):
        """句中 <mood> 剥离为 META，前后语音连续（修复 #3）。"""
        toks = _whole("你好<mood>开心</mood>世界")
        assert toks == [
            Token(TokenKind.SPEECH, "你好"),
            Token(TokenKind.META, "开心", meta_kind="mood"),
            Token(TokenKind.SPEECH, "世界"),
        ]

    def test_empty_mood_token(self):
        """空 <mood></mood> → META token 但 text 为空。"""
        assert _whole("<mood></mood>") == [Token(TokenKind.META, "", meta_kind="mood")]

    def test_unclosed_mood_dropped(self):
        """未闭合 <mood> 残留丢弃、不发事件（行为表 #7）。"""
        assert _whole("你好<mood>开心") == [Token(TokenKind.SPEECH, "你好")]

    def test_unclosed_inner_voice_residual(self):
        """未闭合 <inner_voice> → tokenizer 产出残留 token（协调器广播，行为表 #8）。"""
        toks = _whole("你好<inner_voice>思考中")
        assert toks == [
            Token(TokenKind.SPEECH, "你好"),
            Token(TokenKind.META, "思考中", meta_kind="inner_voice"),
        ]

    def test_two_inner_voices(self):
        """两个 <inner_voice> → 两个 META token（修复 #4）。"""
        toks = _whole("你<inner_voice>一</inner_voice>好<inner_voice>二</inner_voice>！")
        assert toks == [
            Token(TokenKind.SPEECH, "你"),
            Token(TokenKind.META, "一", meta_kind="inner_voice"),
            Token(TokenKind.SPEECH, "好"),
            Token(TokenKind.META, "二", meta_kind="inner_voice"),
            Token(TokenKind.SPEECH, "！"),
        ]


# ============================================================================
# Group 4: CODE / MARKDOWN 块
# ============================================================================

class TestCodeBlocks:
    def test_fence_line_start(self):
        """行首 ```python 入 CODE，lang=python。"""
        assert _whole("```python\n代码\n```") == [
            Token(TokenKind.CODE, "代码\n", lang="python")
        ]

    def test_fence_mid_line_is_speech(self):
        """行中 ``` → SPEECH（at_line_start=False，修复 #2 反面）。"""
        toks = _whole("看 ```inline``` 示例")
        assert all(t.kind == TokenKind.SPEECH for t in toks)
        assert "".join(t.text for t in toks) == "看 ```inline``` 示例"

    def test_fence_indented(self):
        """缩进 ≤3 空格 → 入 CODE。"""
        assert _whole("  ```python\n缩进\n```") == [
            Token(TokenKind.SPEECH, " "),
            Token(TokenKind.SPEECH, " "),
            Token(TokenKind.CODE, "缩进\n", lang="python"),
        ]

    def test_four_space_indent_not_fence(self):
        """缩进 4 空格 → 非行首 → 全部按普通文本（CommonMark）。"""
        toks = _whole("    ```python\n代码")
        assert all(t.kind == TokenKind.SPEECH for t in toks)
        assert "".join(t.text for t in toks) == "    ```python\n代码"

    def test_shorter_fence_line_does_not_close(self):
        """开围栏 4 反引号 + 内容行首 3 反引号 → 不终止（长度不足）。"""
        assert _whole("````python\n```不终止\n````") == [
            Token(TokenKind.CODE, "```不终止\n", lang="python")
        ]

    def test_content_mid_line_backticks(self):
        """代码内容里行中反引号不终止。"""
        assert _whole("```\na ``` b\n```") == [
            Token(TokenKind.CODE, "a ``` b\n", lang="")
        ]

    def test_fence_lang_first_token(self):
        """lang = 起始标记后第一个空白分隔 token（不得把整行当 language）。"""
        assert _whole("```python 注意这个函数\nx\n```") == [
            Token(TokenKind.CODE, "x\n", lang="python")
        ]

    def test_code_tag(self):
        assert _whole("<|code|python>x = 1</|code|>") == [
            Token(TokenKind.CODE, "x = 1", lang="python")
        ]

    def test_markdown_tag(self):
        assert _whole("<|markdown|># 标题</|markdown|>") == [
            Token(TokenKind.MARKDOWN, "# 标题")
        ]

    def test_no_nested_parse_in_block(self):
        """块内容不嵌套解析——<mood>x</mood> 按普通内容。"""
        assert _whole("<|code|><mood>x</mood></|code|>") == [
            Token(TokenKind.CODE, "<mood>x</mood>", lang="")
        ]

    def test_unclosed_block_dropped(self):
        """未闭合 CODE/MARKDOWN 整体丢弃（行为表 #11）。"""
        assert _whole("```python\n代码") == []
        assert _whole("<|code|>内容") == []
        assert _whole("<|markdown|>内容") == []

    def test_fence_closed_then_more_text_not_lost(self):
        """围栏关闭后继续说话——后续文本不得被幽灵 FENCE 吞掉（final review 严重 bug）。

        回归：_enter_fence 未清空 _held 残留反引号 → 关闭回 NORMAL 后，
        下一行首非反引号字符触发 run_len>=3 幽灵 FENCE，吞掉后续文本。
        """
        assert _whole("第一段。\n```python\nx = 1\n```\n然后继续说。") == [
            Token(TokenKind.SPEECH, "第一段。"),
            Token(TokenKind.SPEECH, "\n"),
            Token(TokenKind.CODE, "x = 1\n", lang="python"),
            # 收尾围栏后的 \n 作为 CommonMark 收尾行 trailing 被消费，后续文本完整保留
            Token(TokenKind.SPEECH, "然后继续说。"),
        ]

    def test_fence_closed_then_more_text_cross_chunk(self):
        """围栏后继续文本跨 chunk 到达同样完整（防持有路径残留）。"""
        toks = _feed_split(["第一段。\n```python\nx=1\n```", "\n然后继续说。"])
        joined = "".join(t.text for t in toks if t.kind == TokenKind.SPEECH)
        assert "然后继续说。" in joined
        assert any(t.kind == TokenKind.CODE for t in toks)


# ============================================================================
# Group 5: 行首双状态 / 持有上界
# ============================================================================

class TestLineStart:
    def test_newline_emitted_immediately_before_fence(self):
        """行首状态跟踪不持有 \n——句子不被 fence 检测延迟。"""
        tz = StreamTokenizer()
        assert tz.feed("好的。\n```python\n") == [
            Token(TokenKind.SPEECH, "好的。"),
            Token(TokenKind.SPEECH, "\n"),
        ]

    def test_backtick_prefix_cross_chunk(self):
        """反引号前缀跨 chunk 持有：\n + `` 后第 3 个反引号补足成围栏。"""
        tz = StreamTokenizer()
        assert tz.feed("\n") == [Token(TokenKind.SPEECH, "\n")]
        assert tz.feed("``") == []
        assert tz.feed("`python\n代码\n```") == []
        # 收尾围栏行由 EOF 终止 → flush 时确认闭合
        assert tz.flush() == [Token(TokenKind.CODE, "代码\n", lang="python")]

    def test_two_backticks_release_as_text(self):
        """行首 2 反引号后接非反引号 → 非围栏，按普通文本释放。"""
        toks = _whole("``x")
        assert all(t.kind == TokenKind.SPEECH for t in toks)
        assert "".join(t.text for t in toks) == "``x"

    def test_literal_less_than_not_held(self):
        """价格<100元：< 后数字 1 不构成任何起始模式前缀 → 放行（测试策略 5）。"""
        toks = _whole("价格<100元")
        assert all(t.kind == TokenKind.SPEECH for t in toks)
        assert "".join(t.text for t in toks) == "价格<100元"

    def test_trailing_less_than_released(self):
        """flush 残留字面 < → 回落 SPEECH（spec「未知模式 → 回落 SPEECH」）。"""
        toks = _whole("价格<")
        assert all(t.kind == TokenKind.SPEECH for t in toks)
        assert "".join(t.text for t in toks) == "价格<"
        assert Token(TokenKind.SPEECH, "<") in toks

    def test_partial_tag_prefix_released(self):
        """持有但未进入标签内容态的碎片（<mo）→ flush 回落 SPEECH。"""
        toks = _whole("<mo")
        assert all(t.kind == TokenKind.SPEECH for t in toks)
        assert "".join(t.text for t in toks) == "<mo"

    def test_unclosed_fence_marker_discarded(self):
        """行首 3+ 反引号 = 围栏已开启未闭合 → flush 丢弃（非文本）。"""
        assert _whole("```") == []
        assert _whole("````") == []


# ============================================================================
# Group 6: 接口与状态
# ============================================================================

class TestInterface:
    def test_feed_empty_returns_empty(self):
        tz = StreamTokenizer()
        assert tz.feed("") == []

    def test_reset_reuse(self):
        tz = StreamTokenizer()
        tz.feed("<#0.5#>")
        tz.reset()
        assert tz.feed("（眨眼）") == [Token(TokenKind.STAGE_CN, "（眨眼）")]

    def test_token_is_frozen(self):
        t = Token(TokenKind.SPEECH, "x")
        with pytest.raises(Exception):
            t.text = "y"  # type: ignore[misc]
