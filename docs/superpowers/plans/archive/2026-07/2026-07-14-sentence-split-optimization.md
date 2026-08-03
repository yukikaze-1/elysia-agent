# 分句系统优化 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 SentenceBuffer 和 BlockFilter 的 2 个正确性 bug + 1 个安全 bug + 死代码清理 + 工程改进，保持公开 API 兼容。

**Architecture:** 重写 `sentence_buffer.py`（142 行）和 `block_filter.py`（142 行），调整 `cognitive_loop.py` 中的集成代码（SentenceBuffer/BlockFilter 改为每轮新建并赋给实例属性），新增/更新测试文件。

**Tech Stack:** Python 3.13, pytest, asyncio

## Global Constraints

- conda 环境: `conda activate Elysia`
- 所有现有测试必须保持通过（`test_sentence_buffer.py` 65 个 unit test）
- 公开 API（`feed()` / `flush()` / `reset()`）签名不变
- 参考实现代码在 `tmp.md`

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `core/sentence_buffer.py` | 重写 | 流式分句引擎：`SentenceBuffer` + `_is_dot_boundary` |
| `core/tests/test_sentence_buffer.py` | 追加 | 现有 65 test + 新增 4 test |
| `core/block_filter.py` | 重写 | 流式块过滤器：`BlockFilter` + `build_parts_with_blocks` + `_parse_blocks` |
| `core/tests/test_block_filter.py` | 新建 | BlockFilter 9 test + build_parts_with_blocks 3 test |
| `core/cognitive_loop.py` | 修改 L107/L226/L339/L1032 | 集成调整：每轮新建 buffer/filter，interrupt 清理 |
| `core/tests/test_phase3.py` | 不变 | `test_process_resets_sentence_buffer` 仍应通过（新 buffer 天然空） |

---

### Task 1: Rewrite `core/sentence_buffer.py`

**Files:**
- Modify: `core/sentence_buffer.py` (full replace)

**Interfaces:**
- Produces: `SentenceBuffer(delimiters?, min_chunk_length?, max_buffer_length?, abbreviations?)` — `feed(delta: str) -> str | None`, `flush() -> str | None`, `reset() -> None`
- Produces: `_is_dot_boundary(text: str, dot_pos: int, *, abbreviations: frozenset[str]) -> bool`

- [ ] **Step 1: Write the new `core/sentence_buffer.py`**

Replace the entire file with:

> **行为变更说明**：`_consume_sentence` 中连续分隔符之间的空白处理从旧实现的"精确一个空格 `== " "`"改为 `isspace()` 消费任意数量空白字符。触发场景极少（正常输入不会在标点间塞多个空格/制表符），且新行为更合理（全部消费而非残留）。现有 test `test_consecutive_with_space` 仍通过。

```python
"""流式分句缓冲器。

将 LLM 流式输出累积为适合 TTS 的完整句子。
"""

from __future__ import annotations

import re
from collections.abc import Iterable


_HARD_DELIMITERS = frozenset("。！？!?\n～…〜")
_DEFAULT_DELIMITERS = _HARD_DELIMITERS | {"."}

# 常见英文缩写。可根据业务继续扩展。
_DEFAULT_ABBREVIATIONS = frozenset(
    {
        "asap",
        "approx",
        "capt",
        "col",
        "dept",
        "dr",
        "est",
        "etc",
        "gen",
        "hon",
        "jr",
        "lt",
        "mr",
        "mrs",
        "ms",
        "mt",
        "prof",
        "rep",
        "rev",
        "sen",
        "sgt",
        "sr",
        "st",
        "vs",
    }
)


def _is_dot_boundary(
    text: str,
    dot_pos: int,
    *,
    abbreviations: frozenset[str],
) -> bool:
    """判断英文句号是否构成句子边界。

    会忽略以下情况：

    - 小数点，例如 ``3.14``
    - URL 或域名，例如 ``example.com``
    - 常见英文缩写，例如 ``Dr.``
    - 首字母缩写，例如 ``U.S.``

    注意：``3.14.`` 的最后一个句号会被视为句子边界。
    """

    before = text[dot_pos - 1] if dot_pos > 0 else ""
    after = text[dot_pos + 1] if dot_pos + 1 < len(text) else ""

    # 只有前后都是数字时，才是小数点。
    if before.isdigit() and after.isdigit():
        return False

    # URL / 域名中的点，例如 example.com。
    if before.isalpha() and after.isalpha():
        return False

    # 提取当前单词或缩写。
    left = dot_pos
    while left > 0 and text[left - 1].isalpha():
        left -= 1

    word = text[left:dot_pos].lower()

    # Dr. / Mr. / etc.
    if word in abbreviations:
        return False

    # U.S. / D.C. 等首字母缩写。
    # 例如当前点前的文本是 "... U.S"
    prefix = text[max(0, dot_pos - 4):dot_pos]
    if re.search(r"(?:[A-Z]\.)?[A-Z]{1,2}$", prefix):
        return False

    return True


class SentenceBuffer:
    """累积流式文本，并输出完整句子。

    Args:
        delimiters:
            句子分隔符。默认包含中文句号、问号、感叹号、
            换行、省略号以及英文句号。
        min_chunk_length:
            短句最小长度。短于该长度的句子，在后面没有更多文本时会暂存。
        max_buffer_length:
            缓冲区最大长度。超过后会优先在空白位置强制切分，
            防止无标点长文本无限增长。
        abbreviations:
            不应触发英文句号切分的缩写集合。
    """

    DEFAULT_DELIMITERS = "".join(sorted(_DEFAULT_DELIMITERS))

    def __init__(
        self,
        *,
        delimiters: str | Iterable[str] | None = None,
        min_chunk_length: int = 5,
        max_buffer_length: int = 2000,
        abbreviations: Iterable[str] | None = None,
    ) -> None:
        if delimiters is None:
            delimiter_set = set(_DEFAULT_DELIMITERS)
        else:
            delimiter_set = set(delimiters)

        if min_chunk_length < 0:
            raise ValueError("min_chunk_length must be >= 0")

        if max_buffer_length <= 0:
            raise ValueError("max_buffer_length must be > 0")

        self._delimiters = frozenset(delimiter_set)
        self._min_chunk = min_chunk_length
        self._max_buffer_length = max_buffer_length
        self._abbreviations = frozenset(
            item.lower().rstrip(".")
            for item in (
                _DEFAULT_ABBREVIATIONS
                if abbreviations is None
                else abbreviations
            )
        )

        self._buffer = ""

    def feed(self, delta: str) -> str | None:
        """输入一段流式文本。

        一次最多返回一个句子。调用方应持续调用，直到返回 ``None``。
        """
        if not delta:
            return None

        self._buffer += delta
        return self._extract()

    def flush(self) -> str | None:
        """返回剩余文本，并清空缓冲区。"""
        if not self._buffer.strip():
            self._buffer = ""
            return None

        result = self._buffer
        self._buffer = ""
        return result

    def reset(self) -> None:
        """清空内部状态。"""
        self._buffer = ""

    def _extract(self) -> str | None:
        text = self._buffer

        split_pos = self._find_split(text)

        if split_pos >= 0:
            complete, rest = self._consume_sentence(text, split_pos)

            if not complete.strip():
                self._buffer = rest
                return self._extract() if rest else None

            if len(complete.strip()) < self._min_chunk:
                # 短句后面没有可供合并的文本，继续等待。
                if not rest.strip():
                    return None

                # 后面仍然有文本时释放短句，避免缓冲区卡死。
                self._buffer = rest
                return complete.rstrip()

            self._buffer = rest
            return complete.rstrip()

        # 没有自然边界时，执行长度保护。
        if len(text) > self._max_buffer_length:
            forced_pos = self._find_forced_split(text)

            if forced_pos > 0:
                result = text[:forced_pos].rstrip()
                self._buffer = text[forced_pos:].lstrip()
                return result

        return None

    def _consume_sentence(
        self,
        text: str,
        split_pos: int,
    ) -> tuple[str, str]:
        """消费一个句子及其连续标点。"""

        index = split_pos + 1

        # 合并连续标点，例如：
        #   ？！、!!、……、～！
        while index < len(text) and text[index] in self._run_delimiters:
            index += 1

        # 允许标点之间出现一个空格：
        #   "? !"、"？ ！"
        while index < len(text) and text[index].isspace():
            whitespace_start = index

            while index < len(text) and text[index].isspace():
                index += 1

            if index < len(text) and text[index] in self._run_delimiters:
                while index < len(text) and text[index] in self._run_delimiters:
                    index += 1
            else:
                # 不是连续标点，空白属于后续文本。
                index = whitespace_start
                break

        return text[:index], text[index:]

    @property
    def _run_delimiters(self) -> frozenset[str]:
        return self._delimiters | {"."}

    def _find_split(self, text: str) -> int:
        """单次扫描，寻找第一个有效句子边界。"""

        for index, char in enumerate(text):
            if char not in self._delimiters:
                continue

            if char == "." and not _is_dot_boundary(
                text,
                index,
                abbreviations=self._abbreviations,
            ):
                continue

            return index

        return -1

    def _find_forced_split(self, text: str) -> int:
        """寻找长度保护用的切分点。

        优先选择最大长度前的空格；如果不存在空格，
        则直接在最大长度处切分。
        """

        limit = min(self._max_buffer_length, len(text))

        whitespace_pos = text.rfind(" ", 0, limit)
        if whitespace_pos > 0:
            return whitespace_pos

        return limit
```

- [ ] **Step 2: Run existing sentence buffer tests to verify no regression**

```bash
conda activate Elysia && python -m pytest core/tests/test_sentence_buffer.py -v
```

Expected: 65 passed (some may need adjustment for behavior changes)

- [ ] **Step 3: Commit**

```bash
git add core/sentence_buffer.py
git commit -m "refactor: rewrite sentence_buffer.py

- Remove dead _split_pat regex
- Fix _is_dot_boundary: 3.14. terminal period now correctly splits
- Abbreviation detection: full word scan + case-insensitive
- Add max_buffer_length=2000 with forced split on whitespace
- Extract _consume_sentence for cleaner delimiter merging
- Single-pass _find_split via enumerate
- _HARD_DELIMITERS -> frozenset for consistency"
```

---

### Task 2: Add new tests to `core/tests/test_sentence_buffer.py`

**Files:**
- Modify: `core/tests/test_sentence_buffer.py` (append at end)

**Interfaces:**
- Consumes: `SentenceBuffer` from Task 1

- [ ] **Step 1: Append new tests to the test file**

Append after line 438 (end of file):

```python

# ============================================================================
# Group 12: Decimal terminal period (3.14. bug fix)
# ============================================================================

class TestDecimalTerminalPeriod:
    def test_decimal_terminal_period_splits(self):
        """3.14. — 最后一个句号应该作为句子边界切句。"""
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("版本是 3.14.") == "版本是 3.14."

    def test_decimal_without_terminal_period_held(self):
        """3.14 — 中间的句号不切句，整句等待。"""
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("版本是 3.14") is None
        assert buf.flush() == "版本是 3.14"


# ============================================================================
# Group 13: Abbreviation case insensitivity
# ============================================================================

class TestAbbreviationCaseInsensitive:
    def test_abbreviation_lowercase(self):
        """dr. should not split."""
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("dr. who is cool?") == "dr. who is cool?"

    def test_abbreviation_uppercase(self):
        """DR. should not split (case-insensitive match)."""
        buf = SentenceBuffer(min_chunk_length=3)
        assert buf.feed("DR. Who is cool?") == "DR. Who is cool?"


# ============================================================================
# Group 14: Max buffer length (forced split)
# ============================================================================

class TestMaxBufferLength:
    def test_forced_split_on_whitespace(self):
        """超长无标点文本在空格处强制切分。"""
        buf = SentenceBuffer(min_chunk_length=3, max_buffer_length=20)
        result = buf.feed("this is a very long sentence without any punctuation")
        # 第 20 个字符附近应该触发强制切分
        assert result is not None
        assert len(result) <= 20

    def test_forced_split_no_whitespace(self):
        """超长无空格文本在 max_buffer_length 处硬切。"""
        buf = SentenceBuffer(min_chunk_length=3, max_buffer_length=10)
        result = buf.feed("abcdefghijklmnopqrstuvwxyz")
        assert result is not None
        assert len(result) <= 10

    def test_normal_text_not_forced(self):
        """正常有标点文本不触发强制切分。"""
        buf = SentenceBuffer(min_chunk_length=3, max_buffer_length=2000)
        results = []
        for ch in "你好。今天天气真好。":
            s = buf.feed(ch)
            if s is not None:
                results.append(s)
        assert results == ["你好。", "今天天气真好。"]
```

- [ ] **Step 2: Run new tests to verify they pass**

```bash
conda activate Elysia && python -m pytest core/tests/test_sentence_buffer.py -v -k "TestDecimalTerminalPeriod or TestAbbreviationCaseInsensitive or TestMaxBufferLength"
```

Expected: 7 passed

- [ ] **Step 3: Run all sentence buffer tests**

```bash
conda activate Elysia && python -m pytest core/tests/test_sentence_buffer.py -v
```

Expected: ~72 passed (65 existing + 7 new)

- [ ] **Step 4: Commit**

```bash
git add core/tests/test_sentence_buffer.py
git commit -m "test: add sentence_buffer tests for 3.14., abbreviation, max_buffer

- TestDecimalTerminalPeriod: 3.14. terminal period splits, 3.14 held
- TestAbbreviationCaseInsensitive: dr./DR. both case-insensitive
- TestMaxBufferLength: forced split on whitespace, hard split, normal text"
```

---

### Task 3: Rewrite `core/block_filter.py`

**Files:**
- Modify: `core/block_filter.py` (full replace)

**Interfaces:**
- Produces: `BlockFilter()` — `feed(chunk: str) -> str`, `flush() -> str`, `reset() -> None`
- Produces: `build_parts_with_blocks(acc_text: str, sentences: list[str]) -> tuple[list[dict], dict[str, list[int]]]`
- Produces: `ParsedBlock(kind: Literal["markdown","code"], content: str, language: str = "")` — dataclass
- Produces: `_parse_blocks(acc_text: str) -> list[tuple[int, int, ParsedBlock]]`

- [ ] **Step 1: Write the new `core/block_filter.py`**

Replace the entire file with:

```python
"""流式结构化块过滤器。

过滤以下结构化块，使代码和 Markdown 内容不会进入 TTS：

    <|markdown|>...</|markdown|>
    <|code|python>...</|code|>

同时提供流结束后的结构化解析功能。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


BlockType = Literal["markdown", "code"]


_MARKDOWN_START = "<|markdown|>"
_CODE_START_RE = re.compile(r"<\|code\|([A-Za-z0-9_+.-]*)>")
_MARKDOWN_END = "</|markdown|>"
_CODE_END = "</|code|>"

# 所有可能的起始标记。
_START_RE = re.compile(
    r"<\|markdown\|>|<\|code\|([A-Za-z0-9_+.-]*)>"
)

# 只保留这么多尾部字符，防止标记跨 chunk。
_MAX_MARKER_LENGTH = max(
    len(_MARKDOWN_START),
    len(_MARKDOWN_END),
    len(_CODE_END),
    len("<|code|python>"),
)


@dataclass(frozen=True)
class ParsedBlock:
    kind: BlockType
    content: str
    language: str = ""


class BlockFilter:
    """流式过滤 Markdown/code 块。

    块内内容始终不会返回给调用方。

    对于未闭合的块，``flush()`` 会丢弃剩余块内容，
    从而避免代码泄漏到 TTS。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._mode: BlockType | None = None

    def feed(self, chunk: str) -> str:
        """输入流式文本，返回可以进入 TTS 的普通文本。"""
        if not chunk:
            return ""

        self._buf += chunk
        output: list[str] = []

        while self._buf:
            if self._mode is not None:
                end_marker = (
                    _MARKDOWN_END
                    if self._mode == "markdown"
                    else _CODE_END
                )

                end_pos = self._buf.find(end_marker)

                if end_pos < 0:
                    # 块尚未结束。全部视为块内容，不输出。
                    # 保留尾部只是为了兼容极端情况下后续结束标记跨 chunk。
                    keep = min(len(self._buf), len(end_marker) - 1)
                    self._buf = self._buf[-keep:] if keep else ""
                    return "".join(output)

                # 丢弃块内容和结束标记。
                self._buf = self._buf[end_pos + len(end_marker):]
                self._mode = None
                continue

            start_match = _START_RE.search(self._buf)

            if start_match is not None:
                output.append(self._buf[:start_match.start()])

                marker = start_match.group(0)
                self._buf = self._buf[start_match.end():]

                if marker == _MARKDOWN_START:
                    self._mode = "markdown"
                else:
                    self._mode = "code"

                continue

            # 没有完整起始标记时，安全输出大部分内容，
            # 保留末尾防止起始标记被 chunk 截断。
            keep = _MAX_MARKER_LENGTH - 1

            if len(self._buf) <= keep:
                break

            output.append(self._buf[:-keep])
            self._buf = self._buf[-keep:]
            break

        return "".join(output)

    def flush(self) -> str:
        """清空缓冲区。

        - 普通文本会返回；
        - 已进入但未闭合的块会整体丢弃；
        - 尾部残留的半截起始标记不会泄漏到 TTS。
        """

        if self._mode is not None:
            # 关键：未闭合代码块不能进入 TTS。
            self._buf = ""
            self._mode = None
            return ""

        result = self._buf
        self._buf = ""

        # 处理尾部可能存在的半截标记。
        partial_start = result.rfind("<|")

        if partial_start >= 0:
            suffix = result[partial_start:]

            # 如果尾部看起来像一个被截断的结构化标签，
            # 丢弃该标签，不影响此前的正常文本。
            if (
                len(suffix) < _MAX_MARKER_LENGTH
                and ">" not in suffix
            ):
                result = result[:partial_start]

        return result

    def reset(self) -> None:
        """重置过滤器。"""
        self._buf = ""
        self._mode = None


def _parse_blocks(acc_text: str) -> list[tuple[int, int, ParsedBlock]]:
    """解析完整文本中的块，并返回块在原文中的位置。"""

    result: list[tuple[int, int, ParsedBlock]] = []
    position = 0

    while position < len(acc_text):
        match = _START_RE.search(acc_text, position)

        if match is None:
            break

        marker = match.group(0)

        if marker == _MARKDOWN_START:
            kind: BlockType = "markdown"
            language = ""
            end_marker = _MARKDOWN_END
        else:
            kind = "code"
            language = _CODE_START_RE.fullmatch(marker).group(1) or ""
            end_marker = _CODE_END

        content_start = match.end()
        end_pos = acc_text.find(end_marker, content_start)

        if end_pos < 0:
            # 未闭合块不进入 parts，避免把残缺内容渲染到前端。
            break

        content = acc_text[content_start:end_pos]

        result.append(
            (
                match.start(),
                end_pos + len(end_marker),
                ParsedBlock(
                    kind=kind,
                    content=content.strip(),
                    language=language,
                ),
            )
        )

        position = end_pos + len(end_marker)

    return result


def _append_sentences_from_segment(
    segment: str,
    sentences: list[str],
    sentence_index: int,
    parts: list[dict],
) -> int:
    """将当前普通文本段中的句子按顺序放入 parts。"""

    search_pos = 0

    while sentence_index < len(sentences):
        sentence = sentences[sentence_index]

        if not sentence.strip():
            sentence_index += 1
            continue

        # 优先按原始句子文本匹配。
        found = segment.find(sentence, search_pos)

        # SentenceBuffer 会 rstrip()，因此允许用 strip 后文本匹配。
        if found < 0:
            stripped = sentence.strip()
            found = segment.find(stripped, search_pos)

        if found < 0:
            break

        parts.append({"content": sentence})
        search_pos = found + len(sentence)
        sentence_index += 1

    return sentence_index


def build_parts_with_blocks(
    acc_text: str,
    sentences: list[str],
) -> tuple[list[dict], dict[str, list[int]]]:
    """从完整 LLM 输出中构建前端 parts。

    与旧实现不同，Markdown/code block 会保留在原始文本位置，
    不再统一追加到所有句子之后。

    Returns:
        (
            [
                {"content": "你好。"},
                {"content": "| a | b |"},
                {"content": "再见。"},
                {"content": "print('ok')", "language": "python"},
            ],
            {
                "markdown": [1],
                "code": [3],
            },
        )
    """

    parts: list[dict] = []
    content_type: dict[str, list[int]] = {
        "markdown": [],
        "code": [],
    }

    blocks = _parse_blocks(acc_text)

    if not blocks:
        return (
            [{"content": sentence} for sentence in sentences],
            content_type,
        )

    sentence_index = 0
    text_start = 0

    for block_start, block_end, block in blocks:
        # 先处理当前 block 前的普通文本。
        text_segment = acc_text[text_start:block_start]

        sentence_index = _append_sentences_from_segment(
            text_segment,
            sentences,
            sentence_index,
            parts,
        )

        # 再插入 block，保持原始交错顺序。
        block_part: dict[str, str] = {
            "content": block.content,
        }

        if block.kind == "code" and block.language:
            block_part["language"] = block.language

        content_type[block.kind].append(len(parts))
        parts.append(block_part)

        text_start = block_end

    # 处理最后一个 block 后面的普通文本。
    sentence_index = _append_sentences_from_segment(
        acc_text[text_start:],
        sentences,
        sentence_index,
        parts,
    )

    # 理论上 sentences 应该已经全部消费。
    # 如果由于清洗、空格变化等原因没有匹配到，
    # 仍然保留这些句子，避免数据丢失。
    while sentence_index < len(sentences):
        sentence = sentences[sentence_index]

        if sentence.strip():
            parts.append({"content": sentence})

        sentence_index += 1

    return parts, content_type
```

- [ ] **Step 2: Commit**

```bash
git add core/block_filter.py
git commit -m "refactor: rewrite block_filter.py

- _depth -> _mode (Literal['markdown','code']|None): fix mismatched end tags
- flush(): discard unclosed block content to prevent code leakage to TTS
- Add reset() for lifecycle symmetry with SentenceBuffer
- flush(): handle trailing partial tags via rfind('<|')
- build_parts_with_blocks: preserve original interleaving order
  (blocks now appear at their actual position, not appended at end)
- Add _parse_blocks() and _append_sentences_from_segment() helpers
- _CODE_START_RE widened to [A-Za-z0-9_+.-]* for broader language id support"
```

---

### Task 4: Create `core/tests/test_block_filter.py`

**Files:**
- Create: `core/tests/test_block_filter.py`

**Interfaces:**
- Consumes: `BlockFilter`, `build_parts_with_blocks` from Task 3

- [ ] **Step 1: Write the new test file**

```python
"""BlockFilter tests — filtering, flush safety, parts ordering."""

import pytest
from core.block_filter import BlockFilter, build_parts_with_blocks

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
        assert bf.feed("你好世界！") == "你好世界！"
        assert bf.flush() == ""

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
        assert bf.feed("hello") == "hello"
        assert bf.flush() == ""

    def test_reset_clears_mode(self):
        """reset 清除未闭合的块模式。"""
        bf = BlockFilter()
        bf.feed("<|markdown|>")
        assert bf._mode == "markdown"
        bf.reset()
        assert bf._mode is None
        assert bf.feed("safe") == "safe"


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
            {"content": "你好。"},
            {"content": "表格"},
            {"content": "再见。"},
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
            {"content": "print(1)", "language": "python"},
            {"content": "你好。"},
            {"content": "note"},
            {"content": "再见。"},
            {"content": "end"},
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
            {"content": "你好。"},
            {"content": "再见。"},
        ]

    def test_parts_no_blocks(self):
        """无块标记时直接返回句子列表。"""
        parts, content_type = build_parts_with_blocks(
            "你好。今天天气真好。",
            ["你好。", "今天天气真好。"],
        )
        assert parts == [
            {"content": "你好。"},
            {"content": "今天天气真好。"},
        ]
        assert content_type == {"markdown": [], "code": []}
```

- [ ] **Step 2: Run new tests**

```bash
conda activate Elysia && python -m pytest core/tests/test_block_filter.py -v
```

Expected: ~16 passed

- [ ] **Step 3: Commit**

```bash
git add core/tests/test_block_filter.py
git commit -m "test: add BlockFilter tests (filtering, flush safety, ordering)

- TestBasicFiltering: code/markdown filtered, plain text pass-through
- TestFlushSafety: unclosed blocks discarded, trailing partial tags trimmed
- TestCrossChunk: markers split across chunk boundaries
- TestTagMatching: mismatched end tags, cross-type nesting
- TestReset: reset clears state and mode
- TestPartsOrdering: simple/complex ordering, unmatched fallback, no blocks"
```

---

### Task 5: Update `core/cognitive_loop.py` integration

**Files:**
- Modify: `core/cognitive_loop.py` — lines 107, 226, 339, 1032

**Interfaces:**
- Consumes: `SentenceBuffer` from Task 1, `BlockFilter` from Task 3
- Produces: `self._sentence_buf` (instance attr, reassigned each round), `self._block_filter` (instance attr, reassigned each round)

- [ ] **Step 1: Remove `self._sentence_buf` from `__init__`**

In `__init__`, replace line 107:
```python
self._sentence_buf = SentenceBuffer()
```

With:
```python
self._sentence_buf: SentenceBuffer | None = None
self._block_filter: BlockFilter | None = None
```

- [ ] **Step 2: Create buffer and filter at `process()` entry**

At line 226, replace:
```python
            self._sentence_buf.reset()
```

With:
```python
            self._sentence_buf = SentenceBuffer(min_chunk_length=5, max_buffer_length=2000)
            self._block_filter = BlockFilter()
```

- [ ] **Step 3: Use `self._block_filter` in `_think_loop`**

At line 339, replace:
```python
                block_filter = BlockFilter()
```

With:
```python
                block_filter = self._block_filter
                block_filter.reset()  # 防御性：_think_loop 可能因 tool round 被多次调用
```

(This ensures `_handle_interrupt` can access the same instance, and the filter is clean for each tool round.)

- [ ] **Step 4: Add `self._block_filter.reset()` to `_handle_interrupt`**

At line 1032, after:
```python
        self._sentence_buf.reset()
```

Add:
```python
        self._block_filter.reset()
```

- [ ] **Step 5: Run phase3 tests to verify integration**

```bash
conda activate Elysia && python -m pytest core/tests/test_phase3.py -v -k "test_process_resets_sentence_buffer"
```

Expected: PASS (the test injects stale data, then `process()` assigns a fresh `SentenceBuffer` which has empty `_buffer`)

- [ ] **Step 6: Commit**

```bash
git add core/cognitive_loop.py
git commit -m "refactor: per-round SentenceBuffer and BlockFilter lifecycle

- __init__: remove self._sentence_buf (now created per-round)
- process() entry: self._sentence_buf = SentenceBuffer(...)
                    self._block_filter = BlockFilter()
- _think_loop: use self._block_filter (reset each round)
- _handle_interrupt: add self._block_filter.reset()
- Avoids cross-round state contamination; _handle_interrupt still works"
```

---

### Task 6: Full test suite verification

**Files:**
- Verify all tests pass

- [ ] **Step 1: Run quick test suite**

```bash
conda activate Elysia && make test-quick
```

Expected: all unit + service tests pass

- [ ] **Step 2: Run sentence buffer and block filter tests specifically**

```bash
conda activate Elysia && python -m pytest core/tests/test_sentence_buffer.py core/tests/test_block_filter.py core/tests/test_phase3.py -v
```

Expected: ~72 + ~16 + relevant tests all passing

- [ ] **Step 3: Run normal test suite**

```bash
conda activate Elysia && make test-normal
```

Expected: all non-slow tests pass

- [ ] **Step 4: Verify no remaining references to old API**

```bash
grep -rn "_split_pat\|_depth\|_skip_dot" core/ --include="*.py"
```

Expected: no results (dead code fully removed)

- [ ] **Step 5: Commit verification**

```bash
git add -A
git commit -m "test: verify full test suite passes after sentence split refactor"
```
