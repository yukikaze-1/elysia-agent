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
    # ---- 用户实测案例：STAGE_EN / ANGLE_EN / ♪♡ 分隔符 ----
    "<pant>偷……偷袭可不行哦。",
    "好～那我就赖在这里不走了♪ 陪你聊到你开始犯困为止。",
    "不客气♡ 能被你这么一句道谢，这一趟折腾也值了。",
    "不过——(sighs) 被你这么一亲，我今晚大概要想着你偷偷笑了。",
    "<#0.5#>夜深了，要是还有想让我\"写\"进备忘里的心愿，我随时都在。",
    "(clears throat) 然后说",
    "<clears throat> 然后说",
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

    def test_english_tone_word_is_stage_en(self):
        """(laughs) 闭合内容为英文语气词 → STAGE_EN（显示剥离、TTS 保留）。"""
        assert _whole("(laughs)") == [Token(TokenKind.STAGE_EN, "(laughs)")]

    def test_english_tone_word_fullwidth(self):
        """（sighs）全角括号同样识别为 STAGE_EN。"""
        assert _whole("（sighs）") == [Token(TokenKind.STAGE_EN, "（sighs）")]

    def test_english_tone_word_case_and_space(self):
        """大小写 / 词内多空格归一后仍命中语气词。"""
        assert _whole("(SIGHS)") == [Token(TokenKind.STAGE_EN, "(SIGHS)")]
        assert _whole("(clears  throat)") == [Token(TokenKind.STAGE_EN, "(clears  throat)")]

    def test_non_tone_english_paren_released(self):
        """(chu3li3) 拼音标注 / 非语气词 → 回落 SPEECH 保留（显示 + TTS）。"""
        assert _whole("(chu3li3)") == [Token(TokenKind.SPEECH, "(chu3li3)")]
        assert _whole("(hello)") == [Token(TokenKind.SPEECH, "(hello)")]

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
# Group 2b: ANGLE_EN（尖括号英文语气词，F3 违规格式）
# ============================================================================

class TestAngleEn:
    def test_angle_tone_word_token(self):
        """<pant> 命中语气词 → ANGLE_EN（全链路丢弃）。"""
        assert _whole("<pant>") == [Token(TokenKind.ANGLE_EN, "<pant>")]

    def test_angle_tone_word_with_space(self):
        """<clears throat> 词内含空格 → ANGLE_EN。"""
        assert _whole("<clears throat>") == [Token(TokenKind.ANGLE_EN, "<clears throat>")]

    def test_angle_non_tone_released(self):
        """<b> / <abc> 非语气词 → 回落 SPEECH。"""
        assert _whole("<b>") == [Token(TokenKind.SPEECH, "<b>")]
        assert _whole("<abc>") == [Token(TokenKind.SPEECH, "<abc>")]

    def test_angle_tone_followed_by_speech(self):
        """<pant> 后接中文 → ANGLE_EN + 后续 SPEECH 分离。"""
        assert _whole("<pant>偷") == [
            Token(TokenKind.ANGLE_EN, "<pant>"),
            Token(TokenKind.SPEECH, "偷"),
        ]

    def test_angle_tag_prefixes_still_win(self):
        """<mood> / <#1#> / <|code|> 等已注册标签不受 ANGLE 态干扰。"""
        assert _whole("<mood>开心</mood>")[0].kind is TokenKind.META
        assert _whole("<#1#>") == [Token(TokenKind.PAUSE, "<#1#>")]

    def test_angle_unclosed_falls_back(self):
        """未闭合 <pant → flush 回落 SPEECH。"""
        assert _whole("<pant") == [Token(TokenKind.SPEECH, "<pant")]

    def test_angle_overlong_falls_back(self):
        """超窗（>ANGLE_MAX）无 `>` → 回落 SPEECH，不延迟。"""
        long_word = "<" + "a" * 20
        toks = _whole(long_word)
        assert toks == [Token(TokenKind.SPEECH, long_word[:17]), Token(TokenKind.SPEECH, "a" * 4)]


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


# ============================================================================
# Group: 跨标签序列（spec 2026-08-02 A 节）
# 显式序列测试——状态残留 bug 与切分方式无关，property 不变式抓不到，
# 必须显式断言输出序列。
# ============================================================================

class TestTagSequence:
    """A1 进入→退出→再进入：每类标签完整进出两次，断言无状态残留。"""

    def test_pause_reenter(self):
        assert _whole("<#0.5#>说<#1#>又说") == [
            Token(TokenKind.PAUSE, "<#0.5#>"),
            Token(TokenKind.SPEECH, "说"),
            Token(TokenKind.PAUSE, "<#1#>"),
            Token(TokenKind.SPEECH, "又说"),
        ]

    def test_stage_cn_reenter(self):
        assert _whole("（眨眼）说（叹气）又说") == [
            Token(TokenKind.STAGE_CN, "（眨眼）"),
            Token(TokenKind.SPEECH, "说"),
            Token(TokenKind.STAGE_CN, "（叹气）"),
            Token(TokenKind.SPEECH, "又说"),
        ]

    def test_stage_en_reenter(self):
        assert _whole("(laughs)说(sighs)又说") == [
            Token(TokenKind.STAGE_EN, "(laughs)"),
            Token(TokenKind.SPEECH, "说"),
            Token(TokenKind.STAGE_EN, "(sighs)"),
            Token(TokenKind.SPEECH, "又说"),
        ]

    def test_angle_en_reenter(self):
        assert _whole("<pant>说<pant>又说") == [
            Token(TokenKind.ANGLE_EN, "<pant>"),
            Token(TokenKind.SPEECH, "说"),
            Token(TokenKind.ANGLE_EN, "<pant>"),
            Token(TokenKind.SPEECH, "又说"),
        ]

    def test_mood_reenter(self):
        assert _whole("你好<mood>开心</mood>世界<mood>累</mood>！") == [
            Token(TokenKind.SPEECH, "你好"),
            Token(TokenKind.META, "开心", meta_kind="mood"),
            Token(TokenKind.SPEECH, "世界"),
            Token(TokenKind.META, "累", meta_kind="mood"),
            Token(TokenKind.SPEECH, "！"),
        ]

    def test_inner_voice_reenter(self):
        assert _whole("<inner_voice>思考1</inner_voice>说<inner_voice>思考2</inner_voice>") == [
            Token(TokenKind.META, "思考1", meta_kind="inner_voice"),
            Token(TokenKind.SPEECH, "说"),
            Token(TokenKind.META, "思考2", meta_kind="inner_voice"),
        ]

    def test_fence_reenter(self):
        """围栏关闭后继续文本 + 再进围栏。回归 2026-08-01 幽灵 FENCE：
        围栏经行首反引号路径打开时 _held 残留，会让关闭后下一个行首字符
        触发幽灵围栏吞掉后续文本。"""
        s = "第一段。\n```python\n代码1\n```\n又说。\n```js\n代码2\n```\n结尾。"
        toks = _whole(s)
        codes = [t for t in toks if t.kind == TokenKind.CODE]
        assert [(t.lang, t.text) for t in codes] == [("python", "代码1\n"), ("js", "代码2\n")]
        speech = "".join(t.text for t in toks if t.kind == TokenKind.SPEECH)
        assert "第一段。" in speech and "又说。" in speech and "结尾。" in speech

    def test_code_tag_reenter(self):
        s = "<|code|>a=1</|code|>说<|code|>b=2</|code|>"
        toks = _whole(s)
        assert [t.text for t in toks if t.kind == TokenKind.CODE] == ["a=1", "b=2"]


class TestAdjacentTagTransition:
    """A2 标签紧邻转换跨 chunk：闭合紧接着开启，切分点落在转换处。

    每个语料遍历所有切分点（含转换处），断言跨 chunk 与一次到达一致。
    """

    ADJACENT = [
        "<mood>开心</mood><#0.5#>说",
        "<inner_voice>思考</inner_voice>\n```python\ncode\n```\n尾",
        "<#1#><mood>累</mood>世界",
        "（笑）<#1#>尾",
        "<|code|>x=1</|code|><#0.5#>尾",
        "首。\n```py\ncode\n```\n（继续说话）",
    ]

    def test_every_split_point(self):
        for s in self.ADJACENT:
            expected = _whole(s)
            for i in range(len(s) + 1):
                got = _feed_split([s[:i], s[i:]])
                assert got == expected, f"S={s!r} split={i!r}\n  whole={expected}\n  got={got}"

    def test_mood_close_pause_open_expected(self):
        assert _whole("<mood>开心</mood><#0.5#>说") == [
            Token(TokenKind.META, "开心", meta_kind="mood"),
            Token(TokenKind.PAUSE, "<#0.5#>"),
            Token(TokenKind.SPEECH, "说"),
        ]

    def test_pause_close_mood_open_expected(self):
        assert _whole("<#1#><mood>累</mood>世界") == [
            Token(TokenKind.PAUSE, "<#1#>"),
            Token(TokenKind.META, "累", meta_kind="mood"),
            Token(TokenKind.SPEECH, "世界"),
        ]
