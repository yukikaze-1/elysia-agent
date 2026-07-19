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

from core.schema.part import TextPart


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
            m = _CODE_START_RE.fullmatch(marker)
            language = (m.group(1) or "") if m else ""
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
    parts: list[TextPart],
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

        parts.append(TextPart(content=sentence))
        search_pos = found + len(sentence)
        sentence_index += 1

    return sentence_index


def build_parts_with_blocks(
    acc_text: str,
    sentences: list[str],
) -> tuple[list[TextPart], dict[str, list[int]]]:
    """从完整 LLM 输出中构建 TextPart 数组 + content_type 索引。

    与旧实现不同，Markdown/code block 会保留在原始文本位置，
    不再统一追加到所有句子之后。
    """

    parts: list[TextPart] = []
    content_type: dict[str, list[int]] = {
        "markdown": [],
        "code": [],
    }

    blocks = _parse_blocks(acc_text)

    if not blocks:
        return (
            [TextPart(content=sentence) for sentence in sentences],
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
        content_type[block.kind].append(len(parts))
        parts.append(TextPart(content=block.content))

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
            parts.append(TextPart(content=sentence))

        sentence_index += 1

    return parts, content_type
