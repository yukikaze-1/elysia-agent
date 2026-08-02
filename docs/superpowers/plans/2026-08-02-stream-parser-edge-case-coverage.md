# 流式解析器边界情况覆盖补强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec 补强流式解析器（`core/text/`）边界情况测试覆盖——跨标签序列、多代码块链路、Loop 层集成边界、自写 fuzz，并为无界缓冲加内存保护（唯一实现改动）。

**Architecture:** 5 组测试补强 + 1 处实现改动，全部落在 `core/text/tests/`、`core/cognitive/tests/test_empty_response_guard.py` 与 `core/text/tokenizer.py`。纯测试任务（Task 1-4）对现有代码应全绿；Task 5（无界缓冲加固）用 TDD（先写失败测试再实现）。断言值已在本计划中给出精确期望（经当前代码实测验证）。

**Tech Stack:** Python 3.11+ / pytest（`pytest.mark.unit`）、无新依赖。测试运行：`conda activate Elysia` 后 `python -m pytest <file> -v`。

## Global Constraints

- 全部新测试标记 `pytestmark = pytest.mark.unit`
- 每任务结束跑该文件全部测试 + `make test-quick` 保持全绿
- 代码注释用中文（CLAUDE.md 规则 2）
- **围栏示例必须用多行 `\n` 形式**：围栏开启要求 `_at_line_start`（tokenizer.py:248）、闭合要求收尾行尾部的 `\n`（tokenizer.py:510-516）。单行 ```` ```py 代码 ``` ```` 会在 info 态把整行吞进 `_fence_infostr`，flush 整体丢弃
- 不引入任何新依赖
- 参考 spec：`docs/superpowers/specs/2026-08-02-stream-parser-edge-case-coverage-design.md`
- 断言值在本计划中的期望均为**当前代码实测结果**——若实现/测试时运行结果不一致，以实际为准并记录偏差，不要强行改断言凑测试

---

### Task 1: A — 跨标签序列测试（tokenizer + stream_processor）

**Files:**
- Modify: `core/text/tests/test_tokenizer.py`（文件末尾追加两个测试类）
- Modify: `core/text/tests/test_stream_processor.py`（文件末尾追加一个测试类）

**Interfaces:**
- Consumes: `StreamTokenizer.feed(str) -> list[Token]`、`.flush() -> list[Token]`、`Token`、`TokenKind`（自 `core.text.tokenizer`，test_tokenizer.py:14 已导入）；`StreamProcessor.feed/flush/get_blocks`（自 `core.text.stream_processor`）；既有 helper `_whole`/`_feed_split`（各文件已有）
- Produces: 无（纯测试）

- [ ] **Step 1: 追加 tokenizer 级 A1 + A2 测试**

在 `core/text/tests/test_tokenizer.py` 文件末尾追加：

```python
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
```

- [ ] **Step 2: 运行验证 A1/A2 tokenizer 级测试**

Run: `python -m pytest core/text/tests/test_tokenizer.py -v --tb=short`
Expected: 全部 PASS（含既有用例）。若新增用例失败，检查失败信息——期望值是实测值，若真不一致说明实现有回归，先排查再继续。

- [ ] **Step 3: 追加 stream_processor 级序列事件测试**

在 `core/text/tests/test_stream_processor.py` 文件末尾追加：

```python
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
```

- [ ] **Step 4: 运行验证 stream_processor 级测试**

Run: `python -m pytest core/text/tests/test_stream_processor.py -v --tb=short`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add core/text/tests/test_tokenizer.py core/text/tests/test_stream_processor.py
git commit -m "test(text): A 跨标签序列测试——进入/退出/再进入 + 紧邻转换跨 chunk"
```

---

### Task 2: B — 多代码块链路

**Files:**
- Modify: `core/text/tests/test_stream_processor.py`（文件末尾追加一个测试类）

**Interfaces:**
- Consumes: `StreamProcessor.feed/flush/get_blocks`；`build_parts(all_sentences, blocks) -> (parts, content_type)`（自 `core.text.parts`）；`StreamEvent`/`StreamEventType`（已导入）
- Produces: 无（纯测试）

- [ ] **Step 1: 追加多代码块链路测试**

在 `core/text/tests/test_stream_processor.py` 文件末尾追加：

```python
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
```

- [ ] **Step 2: 运行验证**

Run: `python -m pytest core/text/tests/test_stream_processor.py -v --tb=short`
Expected: 全部 PASS。

- [ ] **Step 3: Commit**

```bash
git add core/text/tests/test_stream_processor.py
git commit -m "test(text): B 多代码块链路——after_sentence_idx 跨块计数 + parts 交错"
```

---

### Task 3: D — Loop 层集成边界（tags-only / 纯空白 / 纯 emoji）

**Files:**
- Modify: `core/cognitive/tests/test_empty_response_guard.py`（文件末尾追加三个测试函数）

**Interfaces:**
- Consumes: 既有 `_make_loop()`、`_stream_side_effect()`、`_EMPTY`、`_EMPTY_RESPONSE_FALLBACK`/`_EMPTY_RESPONSE_NUDGE`（test_empty_response_guard.py 已定义/导入）；`CognitiveLoop._think_loop`（stub 驱动）
- Produces: 无（纯测试）

- [ ] **Step 1: 追加 tags-only / 纯空白 / 纯 emoji 测试**

在 `core/cognitive/tests/test_empty_response_guard.py` 文件末尾追加：

```python
@pytest.mark.asyncio
async def test_tags_only_retries_then_falls_back():
    """只输出 <mood>/<inner_voice> 标签、无 speech → 守卫触发 → 重试 → 兜底。

    acc_text 是含标签的非空输出（loop.py:425 'LLM 原始输出全文（含标签）'），
    钉死'守卫基于 public_text 而非 acc_text'——若误改 acc_text.strip() 判断，
    此测试立即失败（acc_text 非空 → 守卫不触发）。
    """
    loop = _make_loop()
    tags_only = (
        "<mood>开心</mood><inner_voice>我有点累</inner_voice>",  # acc_text（含标签）
        [],                       # raw_tool_calls
        False,                    # speaking_started
        [],                       # all_sentences
        "我有点累",               # iv_think
        [],                       # blocks
        "",                       # public_text（无 speech）
    )
    loop._stream_llm_response = AsyncMock(
        side_effect=_stream_side_effect(tags_only, tags_only))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 2
    assert loop._finalize_turn.await_count == 1
    args = loop._finalize_turn.await_args.args
    assert args[0] == _EMPTY_RESPONSE_FALLBACK      # acc_text → 兜底
    assert args[1] == [_EMPTY_RESPONSE_FALLBACK]    # all_sentences
    assert args[2] == "我有点累"                    # iv_think 保留
    assert args[7] == _EMPTY_RESPONSE_FALLBACK      # public_text


@pytest.mark.asyncio
async def test_whitespace_only_triggers_guard():
    """纯空白 public_text → 守卫触发 → nudge 重试。"""
    loop = _make_loop()
    ws = ("", [], False, [], "", [], "\n\n  ")
    valid = ("我在", [], True, ["我在"], "", [], "我在")
    loop._stream_llm_response = AsyncMock(
        side_effect=_stream_side_effect(ws, valid))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 2
    assert loop._finalize_turn.await_count == 1


@pytest.mark.asyncio
async def test_emoji_only_no_guard():
    """纯 emoji 是可见回复 → 守卫不触发 → 直接收尾。"""
    loop = _make_loop()
    emoji = ("😊", [], False, ["😊"], "", [], "😊")
    loop._stream_llm_response = AsyncMock(side_effect=_stream_side_effect(emoji))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 1
    assert loop._finalize_turn.await_count == 1
    # 未注入 nudge
    stream_args = loop._stream_llm_response.await_args.args
    assert all(m.get("content") != _EMPTY_RESPONSE_NUDGE for m in stream_args[0])
```

- [ ] **Step 2: 运行验证**

Run: `python -m pytest core/cognitive/tests/test_empty_response_guard.py -v --tb=short`
Expected: 全部 PASS（含既有 4 个守卫测试）。

- [ ] **Step 3: Commit**

```bash
git add core/cognitive/tests/test_empty_response_guard.py
git commit -m "test(cognitive): D Loop 层集成边界——tags-only/纯空白/纯 emoji 守卫行为"
```

---

### Task 4: E — 自写 fuzz（确定性随机，无新依赖）

**Files:**
- Create: `core/text/tests/test_fuzz_sequences.py`

**Interfaces:**
- Consumes: `StreamTokenizer.feed/flush`、`Token`/`TokenKind`、`StreamProcessor`；模块级常量 `STAGE_M` 等不需要——fuzz 只生成短序列（0-8 项），不触及上限
- Produces: 无（纯测试）

- [ ] **Step 1: 创建 fuzz 测试文件**

创建 `core/text/tests/test_fuzz_sequences.py`：

```python
"""自写确定性 fuzz — 随机标签序列 × 随机切分点。

对齐 spec 2026-08-02 E 节。两条不变式：
1. 跨 chunk 等价：任意序列整串 vs 随机切分 → token 流一致
2. 序列后状态干净：平衡序列 + sentinel 精确匹配，暴露状态残留（幽灵 FENCE）

固定种子可复现，300 组，总耗时 <5s。
"""

import random

import pytest

from core.text.tokenizer import StreamTokenizer, Token, TokenKind

pytestmark = pytest.mark.unit

SEED = 20260802
ITERATIONS = 300
# 哨兵必须用生成器无法产生的文本：语音池含"你好世界"，若哨兵与其相同，
# 序列自身的"你好世界" token 会满足 any(...) 断言（幽灵 FENCE 吞掉真实
# 哨兵时仍被逃过）。"哨兵#SENT#" 无分隔符、无标签前缀，恒为单一 SPEECH token。
_SENTINEL = "\n哨兵#SENT#"  # 前导 \n 硬分隔符 → sentinel 独立成句


def _random_closed_tag(rng) -> str:
    """返回一个闭合的随机标签串。"""
    kind = rng.choice(["pause", "stage_cn", "stage_en", "angle_en",
                       "mood", "inner_voice", "code_tag", "markdown_tag"])
    if kind == "pause":
        return "<#0.5#>"
    if kind == "stage_cn":
        return rng.choice(["（眨眼）", "(叹气)", "（我有点，犹豫。）"])
    if kind == "stage_en":
        return rng.choice(["(laughs)", "(sighs)", "(clears throat)"])
    if kind == "angle_en":
        return rng.choice(["<pant>", "<laughs>"])
    if kind == "mood":
        return f"<mood>{rng.choice(['开心', '累', '平静'])}</mood>"
    if kind == "inner_voice":
        return f"<inner_voice>{rng.choice(['思考中', '犹豫', '决定'])}</inner_voice>"
    if kind == "code_tag":
        return f"<|code|>{rng.choice(['x=1', 'def f(): pass'])}</|code|>"
    return f"<|markdown|>{rng.choice(['**粗**', '# 标题'])}</|markdown|>"


def _random_fence(rng) -> str:
    """闭合围栏（多行 \\n 形式，前导 \\n 保证行首开启）。"""
    lang = rng.choice(["python", "js", ""])
    body = rng.choice(["code1", "x = 1", "print(1)"])
    return f"\n```{lang}\n{body}\n```\n"


def _random_speech(rng) -> str:
    """随机语音段（含分句标点 + 可选 ♪/♡）。"""
    return rng.choice(["你好世界", "说句话吧。", "嗯？", "哈哈！", "好吧。"]) + \
        rng.choice(["", "♪", "♡"])


def _balanced_sequence(rng) -> str:
    """平衡序列：所有开的标签必闭，供状态干净性测试。"""
    return "".join(
        rng.choice([lambda: _random_closed_tag(rng),
                    lambda: _random_fence(rng),
                    lambda: _random_speech(rng)])()
        for _ in range(rng.randint(0, 8))
    )


def _arbitrary_sequence(rng) -> str:
    """任意序列：可含未闭合标签，供跨 chunk 等价测试。"""
    items: list[str] = []
    for _ in range(rng.randint(0, 8)):
        kind = rng.randint(0, 3)
        if kind == 0:
            items.append(_random_closed_tag(rng))
        elif kind == 1:
            items.append(_random_fence(rng))
        elif kind == 2:
            items.append(_random_speech(rng))
        else:
            items.append(rng.choice(["<#", "<mood>开", "```", "<inner_voice>", "（长"]))
    return "".join(items)


def _random_cuts(rng, n: int) -> list[int]:
    if n <= 1:
        return []
    k = rng.randint(1, min(5, n))
    return sorted(set(rng.randint(0, n) for _ in range(k)))


def _token_stream(s: str, cuts: list[int]) -> list[Token]:
    """按切分点依次 feed，再 flush，返回 token 序列。"""
    tz = StreamTokenizer()
    pieces: list[str] = []
    prev = 0
    for cut in cuts:
        pieces.append(s[prev:cut])
        prev = cut
    pieces.append(s[prev:])
    toks: list[Token] = []
    for p in pieces:
        toks.extend(tz.feed(p))
    toks.extend(tz.flush())
    return toks


def _token_key(t: Token):
    return (t.kind.name, t.text, t.meta_kind, t.lang)


def _has_exact_sentinel(toks: list[Token]) -> bool:
    return any(t.kind == TokenKind.SPEECH and t.text == "哨兵#SENT#" for t in toks)


class TestCrossChunkEquivalence:
    """不变式 1：任意序列整串 vs 随机切分 → token 流一致。"""

    def test_arbitrary_sequence(self):
        rng = random.Random(SEED)
        for i in range(ITERATIONS):
            s = _arbitrary_sequence(rng)
            expected = [_token_key(t) for t in _token_stream(s, [])]
            cuts = _random_cuts(rng, len(s))
            got = [_token_key(t) for t in _token_stream(s, cuts)]
            assert got == expected, f"iter={i} S={s!r} cuts={cuts!r}\n  whole={expected}\n  got={got}"

    def test_balanced_sequence(self):
        rng = random.Random(SEED + 1)
        for i in range(ITERATIONS):
            s = _balanced_sequence(rng) + _SENTINEL
            expected = [_token_key(t) for t in _token_stream(s, [])]
            cuts = _random_cuts(rng, len(s))
            got = [_token_key(t) for t in _token_stream(s, cuts)]
            assert got == expected, f"iter={i} S={s!r} cuts={cuts!r}"


class TestStateCleanliness:
    """不变式 2：平衡序列后状态干净——sentinel 精确出现，未被吞进/误判/并入。"""

    def test_sentinel_exact_match_after_balanced(self):
        rng = random.Random(SEED + 2)
        for i in range(ITERATIONS):
            s = _balanced_sequence(rng)
            toks = _token_stream(s + _SENTINEL, [])
            assert _has_exact_sentinel(toks), \
                f"iter={i} S={s!r}\n toks={[_token_key(t) for t in toks]}"

    def test_sentinel_exact_match_split_feed(self):
        rng = random.Random(SEED + 3)
        for i in range(ITERATIONS):
            s = _balanced_sequence(rng) + _SENTINEL
            cuts = _random_cuts(rng, len(s))
            toks = _token_stream(s, cuts)
            assert _has_exact_sentinel(toks), \
                f"iter={i} S={s!r} cuts={cuts!r}\n toks={[_token_key(t) for t in toks]}"
```

- [ ] **Step 2: 运行验证 + 计时**

Run: `python -m pytest core/text/tests/test_fuzz_sequences.py -v --tb=short`
Expected: 全部 PASS，总耗时 <5s（`--durations=1` 查看）。

- [ ] **Step 3: fuzz 发现真实 bug 时的处理（contingency）**

如果 fuzz 暴露真实解析缺陷（断言失败且非测试自身 bug）：先记录最小复现输入（`iter=N S=... cuts=...`），**在 tokenizer/stream_processor 修实现**，再把该输入固化为回归测试追加到对应测试文件。修复后重跑 fuzz 直至全绿。如果确认是测试生成器自身问题（如生成了不合法的期望），修正生成器后重跑。

- [ ] **Step 4: Commit**

```bash
git add core/text/tests/test_fuzz_sequences.py
git commit -m "test(text): E 自写 fuzz——跨 chunk 等价 + 序列后状态干净（sentinel 精确匹配）"
```

---

### Task 5: C — 无界缓冲加固（唯一实现改动，TDD）

**Files:**
- Modify: `core/text/tokenizer.py`
- Modify: `core/text/tests/test_tokenizer.py`（文件末尾追加一个测试类）

**Interfaces:**
- Consumes: `StreamTokenizer` 内部状态字段（`_fence_content`/`_fence_pending`/`_fence_infostr`/`_code_content`/`_markdown_content`/`_meta_content`/`_pause_buf`）
- Produces: 三个新模块级常量 + 两个新实例方法（供本文件内部使用）

- [ ] **Step 1: 写失败测试**

在 `core/text/tests/test_tokenizer.py` 文件末尾追加。**测试文件顶部需要 `import core.text.tokenizer as tokenizer_module`**（用于 monkeypatch 常量）：

```python
# ============================================================================
# Group: 无界缓冲加固（spec 2026-08-02 C 节）
# 用 monkeypatch 把 FENCE_MAX_CONTENT 调小（16）加速测试；语义与 64KB 相同。
# ============================================================================

class TestBoundedBuffers:
    """达上限后截断：结构化闭合保头、endswith 闭合保尾、闭合仍检测、内存有界。"""

    def test_unclosed_fence_head_truncated(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "FENCE_MAX_CONTENT", 16)
        tz = StreamTokenizer()
        for ch in "```\n" + "x" * 40:
            tz.feed(ch)
        # flush() 末尾 reset() 会清空缓冲（tokenizer.py:200/222），缓冲断言必须在 flush 前
        assert tz._fence_content == "x" * 16                 # 保头截断
        toks = list(tz.flush())
        assert not any(t.kind == TokenKind.CODE for t in toks)  # 未闭合丢弃

    def test_closed_fence_over_cap_head_truncated(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "FENCE_MAX_CONTENT", 16)
        tz = StreamTokenizer()
        toks: list[Token] = []
        for ch in "```python\n" + "x" * 40 + "\n```\n":
            toks.extend(tz.feed(ch))
        toks.extend(tz.flush())
        codes = [t for t in toks if t.kind == TokenKind.CODE]
        assert len(codes) == 1
        assert codes[0].text == "x" * 16                     # 保头：前 16 字符
        assert codes[0].lang == "python"

    def test_closed_code_tag_over_cap_tail_truncated(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "FENCE_MAX_CONTENT", 16)
        tz = StreamTokenizer()
        toks: list[Token] = []
        for ch in "<|code|>" + "x" * 40 + "</|code|>":
            toks.extend(tz.feed(ch))
        toks.extend(tz.flush())
        codes = [t for t in toks if t.kind == TokenKind.CODE]
        # 闭合仍被检测（endswith 靠保尾存活），内容有界
        assert len(codes) == 1
        assert len(codes[0].text) <= 16

    def test_closed_markdown_over_cap_tail_truncated(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "FENCE_MAX_CONTENT", 16)
        tz = StreamTokenizer()
        toks: list[Token] = []
        for ch in "<|markdown|>" + "x" * 40 + "</|markdown|>":
            toks.extend(tz.feed(ch))
        toks.extend(tz.flush())
        marks = [t for t in toks if t.kind == TokenKind.MARKDOWN]
        assert len(marks) == 1
        assert len(marks[0].text) <= 16

    def test_unclosed_mood_over_cap_tail_truncated(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "FENCE_MAX_CONTENT", 16)
        tz = StreamTokenizer()
        for ch in "<mood>" + "x" * 40:
            tz.feed(ch)
        assert tz._meta_content == "x" * 16                  # 保尾截断（flush 前断言，flush 会清空）
        toks = list(tz.flush())
        assert not any(t.kind == TokenKind.META for t in toks)  # 未闭合不发事件

    def test_unclosed_code_tag_over_cap(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "FENCE_MAX_CONTENT", 16)
        tz = StreamTokenizer()
        for ch in "<|code|>" + "x" * 40:
            tz.feed(ch)
        assert tz._code_content == "x" * 16                  # 保尾截断（flush 前）
        toks = list(tz.flush())
        assert not any(t.kind == TokenKind.CODE for t in toks)  # 未闭合整体丢弃

    def test_unclosed_markdown_over_cap(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "FENCE_MAX_CONTENT", 16)
        tz = StreamTokenizer()
        for ch in "<|markdown|>" + "x" * 40:
            tz.feed(ch)
        assert tz._markdown_content == "x" * 16              # 保尾截断（flush 前）
        toks = list(tz.flush())
        assert not any(t.kind == TokenKind.MARKDOWN for t in toks)  # 未闭合整体丢弃

    def test_pause_overflow_releases_as_speech(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "PAUSE_MAX", 8)
        tz = StreamTokenizer()
        toks: list[Token] = []
        for ch in "<#1111111111":
            toks.extend(tz.feed(ch))
        toks.extend(tz.flush())
        assert not any(t.kind == TokenKind.PAUSE for t in toks)
        assert any(t.kind == TokenKind.SPEECH for t in toks)

    def test_fence_infostr_over_cap_keeps_first(self, monkeypatch):
        monkeypatch.setattr(tokenizer_module, "FENCE_INFO_MAX", 8)
        tz = StreamTokenizer()
        toks: list[Token] = []
        for ch in "```" + "a" * 20 + "\ncode\n```\n":
            toks.extend(tz.feed(ch))
        toks.extend(tz.flush())
        codes = [t for t in toks if t.kind == TokenKind.CODE]
        assert len(codes) == 1
        assert codes[0].lang == "a" * 8                      # 首段保留（保头）

    def test_normal_under_cap_unchanged(self):
        """<上限行为与现状一致（回归）。"""
        tz = StreamTokenizer()
        toks: list[Token] = []
        for ch in "```python\nprint(1)\n```\n":
            toks.extend(tz.feed(ch))
        toks.extend(tz.flush())
        codes = [t for t in toks if t.kind == TokenKind.CODE]
        assert len(codes) == 1
        assert codes[0].text == "print(1)\n"
        assert codes[0].lang == "python"
```

- [ ] **Step 2: 运行验证测试失败**

Run: `python -m pytest core/text/tests/test_tokenizer.py::TestBoundedBuffers -v --tb=short`
Expected: 失败（断言 `_fence_content`/`_meta_content` 无界 → 超过 16；PAUSE/INFO_MAX 未实现）。

- [ ] **Step 3: 实现——新增常量**

在 `core/text/tokenizer.py` 的 `ANGLE_MAX: int = 16`（第 55 行）之后新增：

```python
# 无界缓冲上限（spec 2026-08-02 C 节，行为表 #12）：
# - 块/元内容缓冲（_fence_content/_code_content/_markdown_content/_meta_content）：
#   码点数上限，典型代码块 <4KB，16× 富余
# - _fence_pending：等待行尾判定的累积窗口
# - _fence_infostr：围栏首行（lang 取首段 token，保头）
# - _pause_buf：PAUSE 数字串
FENCE_MAX_CONTENT: int = 64 * 1024
FENCE_INFO_MAX: int = 256
PAUSE_MAX: int = 32
```

- [ ] **Step 4: 实现——新增两个 helper**

在 `StreamTokenizer` 类内（建议放在 `reset()` 方法附近）新增：

```python
    def _append_head_bounded(self, attr: str, text: str) -> None:
        """保头截断追加：结构化闭合缓冲（_fence_content/_fence_pending）。

        围栏闭合靠换行 + 反引号串判定，不读内容缓冲，可安全截断保头——
        保留代码开头（函数头）等更有信息量的部分。
        """
        buf = getattr(self, attr) + text
        if len(buf) > FENCE_MAX_CONTENT:
            buf = buf[:FENCE_MAX_CONTENT]
        setattr(self, attr, buf)

    def _append_tail_bounded(self, attr: str, text: str) -> None:
        """保尾截断追加：endswith 闭合缓冲（_code_content/_markdown_content/_meta_content）。

        闭合标签靠缓冲尾部匹配（endswith），截断保头会让闭合标签永远进不了
        缓冲 → 永不闭合 → flush 整块丢弃（比现状更糟）。保尾截断保留闭合标签。
        """
        buf = getattr(self, attr) + text
        if len(buf) > FENCE_MAX_CONTENT:
            buf = buf[-FENCE_MAX_CONTENT:]
        setattr(self, attr, buf)
```

- [ ] **Step 5: 实现——替换调用点**

按以下规则替换 `tokenizer.py` 内的累积语句（**每个替换点加注释说明所属策略**）：

1. `_feed_fence_body` 内全部 **7 处** `self._fence_content += X`（约 519/532/541/551/554/560/564 行）→ `self._append_head_bounded("_fence_content", X)`。
2. `_feed_fence_body` 内全部 **4 处** `self._fence_pending += c`（strong 态约 508、run 态约 525、indent 态约 538/546 行）→ `self._append_head_bounded("_fence_pending", c)`。**508/525 是无界累积点**（空白串/反引号串）；538/546 受 indent 逻辑约束（≤3 空格 + 单反引号转 run）但有界化无害。**注意 526 行是 `self._fence_run_len += 1`（围栏闭合计数器，勿改）**。
3. `_feed_fence` info 分支的 `self._fence_infostr += c`（约 499 行）→ 保头：
   ```python
   else:
       if len(self._fence_infostr) < FENCE_INFO_MAX:
           self._fence_infostr += c
       return
   ```
4. `_feed_meta` 的 `self._meta_content += c`（约 468 行）→ `self._append_tail_bounded("_meta_content", c)`。
5. `_feed_code_tag` 的 `self._code_content += c`（约 476 行）→ `self._append_tail_bounded("_code_content", c)`。
6. `_feed_markdown` 的 `self._markdown_content += c`（约 483 行）→ `self._append_tail_bounded("_markdown_content", c)`。
7. `_feed_pause` 的 `self._pause_buf = candidate`（约 399 行）→ 超窗释放：
   ```python
   if PAUSE_TAIL_RE.search(candidate):
       if len(candidate) >= PAUSE_MAX:
           # 超窗：长数字串不是真实 pause，回落 SPEECH
           self._emit_speech_text(candidate, out)
           self._exit_pause()
       else:
           self._pause_buf = candidate
       return
   ```
8. **同步 docstring**：`tokenizer.py:14` 的"PAUSE：尾串正则 `PAUSE_TAIL_RE` 驱动（内容无上界，固定前缀框不住）"——PAUSE_MAX 实现后"内容无上界"已不准确，改为"内容上界 `PAUSE_MAX`"。另在文件顶部"持有上界"列表补一句：围栏/块/元内容缓冲有界 `FENCE_MAX_CONTENT`、信息串 `FENCE_INFO_MAX`。

> 注意：替换后重新运行既有 tokenizer 测试确认 0 回归（所有 <上限 输入行为不变）。

- [ ] **Step 6: 运行验证测试通过**

Run: `python -m pytest core/text/tests/test_tokenizer.py -v --tb=short`
Expected: 全部 PASS（既有用例 + 新增 10 个 C 用例）。

- [ ] **Step 7: Commit**

```bash
git add core/text/tokenizer.py core/text/tests/test_tokenizer.py
git commit -m "fix(text): C 无界缓冲加固——FENCE_MAX_CONTENT 上限，围栏保头/tag 保尾截断"
```

---

### Task 6: Final verification

- [ ] **Step 1: 全量 test-quick**

Run: `conda activate Elysia && make test-quick`
Expected: 全绿（既有全部用例保持绿，新增 **33 个**用例：Task1=13、Task2=3、Task3=3、Task4=4、Task5=10）。

- [ ] **Step 2: 全量 text + cognitive 专项**

Run: `python -m pytest core/text/tests core/cognitive/tests/test_empty_response_guard.py -v --tb=short`
Expected: 全绿。

- [ ] **Step 3: 检查 fuzz 计时**

Run: `python -m pytest core/text/tests/test_fuzz_sequences.py --durations=3`
Expected: fuzz **4 个**测试总耗时 <5s（若超时，把 `ITERATIONS` 从 300 降到 200 并记录）。

- [ ] **Step 4: Commit（如无变更则跳过）**

若 Task 1-5 已各自提交，本任务无新变更。若有收尾改动（如 fuzz 计时调整），单独提交。

---

## Self-Review 记录

**Spec 覆盖核对**：
- A（跨标签序列 A1/A2）→ Task 1 ✓
- B（多代码块链路）→ Task 2 ✓
- D（Loop 层集成边界 D1-D3）→ Task 3 ✓（D4 已按 review 删除，由既有 splitter `test_forced_split_*` 覆盖）
- E（自写 fuzz 两条不变式）→ Task 4 ✓
- C（无界缓冲加固，保头/保尾分流）→ Task 5 ✓（已登记 08-01 行为表 #12）

**占位符扫描**：无 TBD/TODO/相似任务引用；所有测试代码完整给出。

**类型一致性**：`_append_head_bounded(attr, text)`/`_append_tail_bounded(attr, text)` 签名在 Task 5 定义与调用一致；`build_parts(all_sentences, blocks)` 返回 `(parts, content_type)` 与 Task 2 使用一致；`get_blocks()` 返回 `(kind, content, lang, after_sentence_idx)` 四元组（`b[0]`/`b[2]`/`b[3]` 索引与 Task 1/2 一致）。

**已知假设**：
- Task 1/2 的 after_idx、token 文本等期望值是计划撰写时对当前代码实测所得。若实现后运行不一致，以实际为准并记录偏差。
- Task 4 fuzz 若命中真实 bug，走 contingency（Step 3）。

**计划 review 修订（2026-08-02）**：
- Task 5 两个内部缓冲断言移到 **flush() 前**（flush 末尾 `reset()` 清空 `_fence_content`/`_meta_content`，flush 后断言恒失败）
- Task 5 补未闭合 code_tag/markdown 超限测试（Task 5 共 10 个用例）
- Task 5 `_fence_pending` 替换点更正为 4 处（508/525 无界、538/546 有界无害；526 是 `_fence_run_len` 计数器勿改）+ docstring 同步
- Task 4 哨兵改 `"\n哨兵#SENT#"`（语音池含"你好世界"，原哨兵会被序列自身 token 满足，幽灵 FENCE 注入实测 100/300 逃过）
- Task 1 A2 语料收紧为真紧邻（去掉中间插"说"的两个条目）
- Task 6 计数修正：新增 33 个用例、fuzz 4 个测试、去掉过时 1768 基线
