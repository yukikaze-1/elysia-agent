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
