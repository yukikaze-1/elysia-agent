"""SpeechSplitter — 结构感知分句器。

消费 StreamTokenizer 的 SPEECH/PAUSE/STAGE_CN token 流，输出完整句子。

设计（对齐 `docs/superpowers/specs/archive/2026-08/2026-08-01-stream-parser-refactor-design.md`）：

- **边界扫描只在 SPEECH 文本上进行**。连续 SPEECH token 先按原序拼接
  （tokenizer 的确定性边界粒度，如 `价格<100元` → [S("价格"), S("<1"), S("00元")]），
  拼接后的文本才做标点扫描。
- **PAUSE/STAGE_CN/STAGE_EN 是受保护 span**：以零宽位置记录（pos = 该 span
  前累计的 SPEECH 字符数），切句时按原序内嵌回句子；span 内部标点永不参与
  边界判定。STAGE_EN（英文语气词）内嵌供 TTS，显示剥离由 parts/tts 路径做。
- **completed_count** = 句边界已消费的句子数，含因 min_chunk 暂留未发出的短句
  （`好。\n` 暂留 → 计数 1）。强制切分（max_buffer 长度保护）发出的句子同样计入，
  保证计数与已发出句子列表对齐（供 `after_sentence_idx` 块定位）。

标点逻辑（`_is_dot_boundary` 缩写/小数/URL 保护、`_consume_sentence` 连续标点
合并、min_chunk 暂留、max_buffer 强制切分）从旧句缓存组件吸收。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from core.text.tokenizer import Token, TokenKind


# 句边界字符集。刻意不含 `\r`：换行归一在 `StreamProcessor.feed`（管道唯一摄入点）
# 收口，到这里只可能是 `\n`——在此补 `\r` 只会掩盖上游未归一的输入。
_HARD_DELIMITERS = frozenset("。！？!?\n♪♡")
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

    Args:
        text: 完整文本。
        dot_pos: 句号字符的索引位置。
        abbreviations: 不触发切分的缩写词集合（小写）。

    Returns:
        True 表示该句号应作为句子边界进行切分。
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

    # Dr. / Mr. 等称谓缩写
    if word in abbreviations:
        return False

    # U.S. / D.C. 等首字母缩写。
    # 取 dot 前最多 3 字符，检查是否为 1-2 个大写字母
    # （如 "U" 匹配 "U.S." 的第一个点，"U.S" 不匹配因为含 .）。
    prefix = text[max(0, dot_pos - 3):dot_pos]
    if re.fullmatch(r"[A-Z]{1,2}", prefix):
        return False

    return True


class SpeechSplitter:
    """结构感知分句器：消费 token 流，产出完整句子（含内嵌 PAUSE/STAGE_CN）。

    Args:
        min_chunk_length:
            短句最小长度。短于该长度的句子在后面没有更多文本时会暂留。
        max_buffer_length:
            缓冲区最大长度。超过后会优先在空白位置强制切分，防止无标点长文本
            无限增长。
        abbreviations:
            不应触发英文句号切分的缩写集合。
    """

    def __init__(
        self,
        *,
        min_chunk_length: int = 5,
        max_buffer_length: int = 2000,
        abbreviations: Iterable[str] | None = None,
    ) -> None:
        """初始化分句器。参数说明见类文档。"""
        if min_chunk_length < 0:
            raise ValueError("min_chunk_length must be >= 0")

        if max_buffer_length <= 0:
            raise ValueError("max_buffer_length must be > 0")

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
        self.reset()

    # ---- 公共接口 ----------------------------------------------------------

    def feed(self, token: Token) -> list[str]:
        """输入一个 token，返回本次能确定的完整句子（含内嵌 span 原序）。

        PAUSE/STAGE_CN 不产生边界，故 span 到达不触发提取；句子边界只由
        SPEECH 文本决定。CODE/MARKDOWN/META 是带外 token，由协调器路由，
        分句器忽略（不冲刷缓冲）。
        """
        if token.kind is TokenKind.SPEECH:
            self._text += token.text
            return self._extract()
        if token.kind in (TokenKind.PAUSE, TokenKind.STAGE_CN, TokenKind.STAGE_EN):
            # 零宽位置：该 span 前累计的 SPEECH 字符数
            self._spans.append((len(self._text), token.text))
            return []
        return []

    def flush(self) -> list[str]:
        """流结束：返回尾句（残留内嵌标签按原序保留；无语音内容则整体丢弃）。"""
        self._front_held = False
        tail_text = self._text[self._consumed :]
        if not tail_text.strip():
            # 尾句无语音内容 → 整体丢弃（含残留 span）
            self._consumed = len(self._text)
            self._spans = []
            return []
        tail = self._build_tail()
        self._consumed = len(self._text)
        self._spans = []
        return [tail]

    def completed_count(self) -> int:
        """已完成句子数：句边界已消费（含 min_chunk 暂留未发出的短句）。"""
        return self._completed

    def reset(self) -> None:
        """清空全部内部状态（每轮对话开始时调用）。"""
        self._text = ""
        self._spans: list[tuple[int, str]] = []
        self._consumed = 0
        self._completed = 0
        self._front_held = False

    # ---- 内部：提取 ---------------------------------------------------------

    def _extract(self) -> list[str]:
        """从语音缓冲提取完整句子。可能一次返回多个。"""
        out: list[str] = []
        while True:
            text = self._text[self._consumed :]
            if not text:
                return out

            split_pos = self._find_split(text)

            if split_pos < 0:
                # 无自然边界 → 长度保护
                if not self._forced_split(out):
                    return out
                continue

            complete, rest = self._consume_sentence(text, split_pos)

            if not complete.strip():
                # 纯标点/空句 → 直接丢弃（不计入 completed）
                self._consume_region(len(complete))
                continue

            # 非空句子：句边界已消费 → 计数（暂留短句不重复计）
            if not self._front_held:
                self._completed += 1

            if len(complete.strip()) < self._min_chunk and not rest.strip():
                # 短句且后面没有可供合并的文本 → 暂留（已计数，未发出）
                self._front_held = True
                return out

            self._front_held = False
            out.append(self._emit_sentence(len(complete)))

    def _forced_split(self, out: list[str]) -> bool:
        """长度保护：无标点长文本在空白位置强制切分。返回是否发生了切分。"""
        text = self._text[self._consumed :]
        if len(text) <= self._max_buffer_length:
            return False

        forced_pos = self._find_forced_split(text)
        if forced_pos <= 0:
            return False

        # 强制切分发出的句子也计入 completed（保证与 all_sentences 对齐，
        # 供 after_sentence_idx 块定位——见类文档）。
        self._completed += 1
        self._front_held = False
        out.append(self._emit_sentence(forced_pos))

        # 对齐旧句缓存组件的 lstrip 语义：跳过剩余文本的前导空白
        rest = self._text[self._consumed :]
        lead = len(rest) - len(rest.lstrip())
        if lead:
            self._consume_region(lead)
        return True

    def _emit_sentence(self, cut: int) -> str:
        """消费 [consumed, consumed+cut) 语音文本，把该区间内的 span 按原序
        内嵌回句子后返回（尾部空白剥离，对齐原 `complete.rstrip()`）。"""
        start = self._consumed
        end = start + cut

        parts: list[str] = []
        cursor = start
        for pos, span_text in self._spans:
            if pos >= end:
                break  # span 已排好序；后续 span 属于下一个句子
            parts.append(self._text[cursor:pos])
            parts.append(span_text)
            cursor = pos
        parts.append(self._text[cursor:end])

        sentence = "".join(parts).rstrip()

        self._consumed = end
        self._spans = [s for s in self._spans if s[0] >= end]
        return sentence

    def _consume_region(self, cut: int) -> None:
        """丢弃 [consumed, consumed+cut) 语音文本，不计入 completed。

        被丢弃的 complete 必为纯空白（含 `\n` 分隔符的空白句）。若其中有
        span（PAUSE/STAGE_CN），不能静默丢弃——重锚到区域末尾（保留在后续
        语音流中），保证行为表 #1 PAUSE 完整性：
        `<#1#>\n继续` → `<#1#>继续`；`好的。<#1#> \n继续。` → `<#1#>继续。`。
        重锚到同一 `end` 的多个 span 保持原相对顺序（list 保序）。
        """
        start = self._consumed
        end = start + cut
        self._consumed = end
        self._spans = [
            (end, text) if pos < end else (pos, text)
            for pos, text in self._spans
        ]

    def _build_tail(self) -> str:
        """按原序内嵌残留 span，构建尾句文本。"""
        start = self._consumed
        end = len(self._text)

        parts: list[str] = []
        cursor = start
        for pos, span_text in self._spans:
            if pos >= end:
                break
            parts.append(self._text[cursor:pos])
            parts.append(span_text)
            cursor = pos
        parts.append(self._text[cursor:end])
        return "".join(parts)

    # ---- 内部：标点扫描（吸收自旧句缓存组件）--------------------------------

    def _find_split(self, text: str) -> int:
        """单次扫描，寻找第一个有效句子边界。"""
        for index, char in enumerate(text):
            if char not in _DEFAULT_DELIMITERS:
                continue
            if char == "." and not _is_dot_boundary(
                text,
                index,
                abbreviations=self._abbreviations,
            ):
                continue
            return index
        return -1

    def _consume_sentence(self, text: str, split_pos: int) -> tuple[str, str]:
        """消费一个句子及其连续标点。"""
        index = split_pos + 1

        # 合并连续标点，例如：？！、!!、……、～！
        while index < len(text) and text[index] in _DEFAULT_DELIMITERS:
            index += 1

        # 允许标点之间出现一个空格："? !"、"？ ！"
        while index < len(text) and text[index].isspace():
            whitespace_start = index

            while index < len(text) and text[index].isspace():
                index += 1

            if index < len(text) and text[index] in _DEFAULT_DELIMITERS:
                while index < len(text) and text[index] in _DEFAULT_DELIMITERS:
                    index += 1
            else:
                # 不是连续标点，空白属于后续文本。
                index = whitespace_start
                break

        return text[:index], text[index:]

    def _find_forced_split(self, text: str) -> int:
        """寻找长度保护用的切分点：优先最大长度前的空格，否则在最大长度处切。"""
        limit = min(self._max_buffer_length, len(text))

        whitespace_pos = text.rfind(" ", 0, limit)
        if whitespace_pos > 0:
            return whitespace_pos

        return limit
