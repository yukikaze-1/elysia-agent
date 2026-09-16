"""StreamProcessor — 新协调器：tokenizer + splitter + 事件发射 + get_blocks + public_text。

对齐设计文档 `docs/superpowers/specs/archive/2026-08/2026-08-01-stream-parser-refactor-design.md`
「事件面」「数据流」「路由规则」「错误处理」。替代旧
`core/cognitive/stream_processor.py`（Task 7 删除），事件面与旧完全一致：
MOOD / INNER_VOICE / SENTENCE / TEXT_DELTA / TTFT（无新增事件类型）。

数据流（spec「数据流」一节）：

    feed(delta) → tokenizer.feed → token 流（跨 chunk 缓冲，只吐完整 token）
        按 kind 路由：
            SPEECH           → TTFT + TEXT_DELTA + splitter（SENTENCE）
            PAUSE            → splitter（显示剥离，TTS 停顿）
            STAGE_CN         → TTFT + TEXT_DELTA + splitter（显示保留，TTS 剥离）
            STAGE_EN         → splitter（显示剥离，TTS 语气词保留）
            ANGLE_EN         → 丢弃（违规格式，显示 + TTS 全剥）
            CODE/MARKDOWN    → 收集为块（闭合时记 splitter.completed_count）
            META             → MOOD / INNER_VOICE 事件（空内容不发）
    flush() → 处理残留（未闭合 inner_voice 广播、尾句、emoji-only 合并）

关键语义：
- TEXT_DELTA 随 SPEECH token 即时发出（不等待分句）；SENTENCE 只在分句完成时发出。
- TTFT 在首个非 META 可见文本（SPEECH/STAGE_CN）到达时发一次（mood/inner_voice 不含）。
- 块闭合时记录 `after_sentence_idx = splitter.completed_count()`（含 min_chunk 暂留
  与 max_buffer 强制切分的句子，spec v3.6.3），get_blocks()/rebuild_public_text()
  复用 core.text.parts（BlockRef + rebuild_public_text）。
- **换行归一**：`feed()` 是管道唯一摄入点，`\\r\\n` / 裸 `\\r` 在此归一为 `\\n`
  （跨 chunk 安全，chunk 末尾的 `\\r` 扣到下一 chunk / flush 定论）——见
  `_normalize_newlines`。修前 CRLF 会让围栏收尾行永不闭合（块 + 块后文本静默消失），
  裸 `\\r` 会让行首状态无法重建（围栏被当可见文本播报）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from core.text.parts import BlockRef
from core.text.parts import rebuild_public_text
from core.text.splitter import SpeechSplitter
from core.text.tags import META_TAGS, PAUSE_RE, is_emoji_only
from core.text.tokenizer import Token, TokenKind, StreamTokenizer

logger = logging.getLogger(__name__)


class StreamEventType(StrEnum):
    MOOD = "mood"
    INNER_VOICE = "inner_voice"
    SENTENCE = "sentence"
    TEXT_DELTA = "text_delta"
    TTFT = "ttft"


# 从枚举自动派生 {value: type} 映射——META 注册表的 emit 字段是 value 字符串，
# 这里映射到事件类型。未来加新事件类型（枚举加值）即自动覆盖，协调器零改动。
_EMIT_BY_NAME: dict[str, StreamEventType] = {v.value: v for v in StreamEventType}


@dataclass(frozen=True)
class StreamEvent:
    type: StreamEventType
    text: str = ""


class StreamProcessor:
    """流式解析协调器。纯状态机，零 I/O。

    Args:
        min_chunk_length: 短句最小长度（传给 SpeechSplitter，min_chunk 暂留）。
        max_buffer_length: 缓冲区最大长度（传给 SpeechSplitter，长度保护强制切分）。
    """

    def __init__(
        self,
        *,
        min_chunk_length: int = 5,
        max_buffer_length: int = 2000,
    ) -> None:
        """初始化协调器。参数说明见类文档。"""
        self._tokenizer = StreamTokenizer()
        self._splitter = SpeechSplitter(
            min_chunk_length=min_chunk_length,
            max_buffer_length=max_buffer_length,
        )
        self._blocks: list[BlockRef] = []
        self._all_tokens: list[Token] = []
        self._ttft_sent = False
        self._cr_pending = False

    # ---- 公共接口 ----------------------------------------------------------

    def feed(self, delta: str) -> list[StreamEvent]:
        """输入增量文本，返回零到多个事件。

        换行归一（``\\r\\n`` / 裸 ``\\r`` → ``\\n``）在本方法完成 —— 这里是
        **管道唯一摄入点**（LLM 增量只经此进入 tokenizer/splitter/块/public_text，
        下游 tts_adapter 与显示路径都消费本方法的产物）。不变式见
        ``_normalize_newlines``。
        """
        if not delta:
            return []
        delta = self._normalize_newlines(delta)
        if not delta:
            return []
        events: list[StreamEvent] = []
        for token in self._tokenizer.feed(delta):
            self._all_tokens.append(token)
            self._route(token, events)
        return events

    def flush(self) -> list[StreamEvent]:
        """流结束：处理残留（未闭合 inner_voice 广播、尾句、emoji 合并）。

        Returns:
            残留的 StreamEvent 列表。flush() 后 get_blocks() 返回本轮全部块。
        """
        events: list[StreamEvent] = []
        # 跨 chunk 扣下的 \r：流已结束 → 定论为行终止符 \n（等价于 LF 输入），
        # 否则一个"末尾恰好是 \r"的流会把这一行粘进上一行（且 tokenizer 永远
        # 收不到换行，行首状态无法重建）。
        tail = self._take_pending_cr()
        if tail:
            for token in self._tokenizer.feed(tail):
                self._all_tokens.append(token)
                self._route(token, events)
        # tokenizer 残留（未闭合 inner_voice 产出 META → 广播；收尾围栏 EOF 确认
        # 闭合产出 CODE → 记块；半截标签/prefix 回落 SPEECH）
        for token in self._tokenizer.flush():
            self._all_tokens.append(token)
            self._route(token, events)
        self._flush_splitter_tail(events)
        return events

    def reset(self) -> None:
        """重置全部内部状态（每轮对话开始时调用）。"""
        self._tokenizer.reset()
        self._splitter.reset()
        self._blocks = []
        self._all_tokens = []
        self._ttft_sent = False
        self._cr_pending = False

    def get_blocks(self) -> list[BlockRef]:
        """返回本轮全部代码块：(kind, content, lang, after_sentence_idx)。"""
        return list(self._blocks)

    def rebuild_public_text(self) -> str:
        """按 token 流重建 public_text（去 META，保留 PAUSE/STAGE_CN/块内容）。"""
        return rebuild_public_text(self._all_tokens)

    # ---- 换行归一（管道唯一摄入点）-------------------------------------------

    def _normalize_newlines(self, delta: str) -> str:
        """换行归一：``\\r\\n`` / 裸 ``\\r`` → ``\\n``（跨 chunk 安全）。

        为什么在摄入点而不是各处空白集合：``\\r`` 会渗进 tokenizer 的
        ``_WS``/行首判定、splitter 的 ``_HARD_DELIMITERS``、块内容、``public_text``、
        以及 tts_adapter/显示路径消费的事件文本 —— 逐点补 ``\\r`` 属于
        "两条路径不对称"缺陷家族（补了一处漏另一处），且块内容里的 ``\\r`` 无论如何
        都该归一。归一一次 = 下游所有消费者自动一致。

        不变式（CRLF 输入的产物与 LF **逐项相同**）：

        - 归一后管道内部不再出现 ``\\r``；
        - chunk 末尾的 ``\\r`` **扣下**到下一个 chunk 再定论，因为 ``"...\\r"`` +
          ``"\\n..."`` 是跨 chunk 的 CRLF：下一个 chunk 以 ``\\n`` 开头则吞掉该
          ``\\n`` 并补一个 ``\\n``（净效果恰好一个换行）；否则直接补 ``\\n``
          （裸 CR = 行终止符）。朴素的每 chunk ``replace("\\r\\n", "\\n")``
          会把这对跨 chunk 的 CRLF 变成**两个**换行。
        - ``flush()`` 时仍扣着（流截断在半个 CRLF）→ 按行终止符补发 ``\\n``；
        - ``reset()`` 清空持有态，不把上一轮的 ``\\r`` 带进下一轮；
        - 空 chunk（``feed("")``）不是"新数据"——``feed`` 在调用本函数前就返回，
          持有态保持不变。
        """
        if self._cr_pending:
            self._cr_pending = False
            if delta.startswith("\n"):
                delta = delta[1:]
            delta = "\n" + delta
        if delta.endswith("\r"):
            self._cr_pending = True
            delta = delta[:-1]
        return delta.replace("\r\n", "\n").replace("\r", "\n")

    def _take_pending_cr(self) -> str:
        """取走跨 chunk 持有态：流已结束，``\\r`` 定论为行终止符，返回 ``"\\n"``。"""
        if not self._cr_pending:
            return ""
        self._cr_pending = False
        return "\n"

    # ---- 路由 --------------------------------------------------------------

    def _route(self, token: Token, events: list[StreamEvent]) -> None:
        kind = token.kind
        if kind is TokenKind.SPEECH:
            # 可见文本：TTFT + 即时 TEXT_DELTA + 分句（SENTENCE）
            self._emit_ttft(events)
            events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=token.text))
            self._emit_sentences(self._splitter.feed(token), events)
        elif kind is TokenKind.PAUSE:
            # PAUSE 是受保护 span，无边界、显示剥离 → 只喂 splitter
            self._splitter.feed(token)
        elif kind is TokenKind.STAGE_CN:
            # 中文舞台指示：显示保留（TEXT_DELTA）、无边界 → 喂 splitter + TEXT_DELTA
            self._emit_ttft(events)
            events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text=token.text))
            self._splitter.feed(token)
        elif kind is TokenKind.STAGE_EN:
            # 英文语气词：显示剥离（无 TEXT_DELTA）、TTS 保留 → 只喂 splitter（同 PAUSE）
            self._splitter.feed(token)
        elif kind is TokenKind.ANGLE_EN:
            # 尖括号语气词（F3 违规格式）：显示 + TTS 全丢弃（不进 splitter / 事件）
            pass
        elif kind in (TokenKind.CODE, TokenKind.MARKDOWN):
            # 块：不产生流式事件；闭合时记 after_sentence_idx（已完成句子数）
            self._blocks.append(
                (kind.value, token.text, token.lang, self._splitter.completed_count())
            )
        elif kind is TokenKind.META:
            # META：闭合时按注册表 emit 发对应事件（空内容不发）。
            # 注册表驱动：meta_kind → META_TAGS 的 emit 字段 → StreamEventType。
            # 未知 emit（注册表有但枚举无对应值，或 meta_kind 未注册）→ 降级丢弃 + warning。
            text = token.text.strip()
            if text:
                emit_name = META_TAGS.get(token.meta_kind, ("", "", ""))[2]
                emit_type = _EMIT_BY_NAME.get(emit_name)
                if emit_type is not None:
                    events.append(StreamEvent(type=emit_type, text=text))
                else:
                    logger.warning("未知 META 类型 %r，事件丢弃", token.meta_kind)

    # ---- 输出辅助 ----------------------------------------------------------

    def _emit_ttft(self, events: list[StreamEvent]) -> None:
        """首个非 META 可见文本到达时发一次 TTFT。"""
        if not self._ttft_sent:
            self._ttft_sent = True
            events.append(StreamEvent(type=StreamEventType.TTFT))

    def _emit_sentences(self, sentences: list[str], events: list[StreamEvent]) -> None:
        """分句完成 → SENTENCE 事件（供 TTS）。"""
        for sentence in sentences:
            events.append(StreamEvent(type=StreamEventType.SENTENCE, text=sentence))

    def _flush_splitter_tail(self, events: list[StreamEvent]) -> None:
        """splitter 尾句 → SENTENCE 事件。

        历史（2026-09-16，S3-G7）：这里曾把 emoji-only 尾句**并入上一条 SENTENCE**
        （注释写"避免独占一行"）。那个合并只在"句子与 emoji 尾停在同一个待落盘缓冲里"
        时才可能发生——正是无空格/无换行回复整轮滞留到 `flush()` 的旧行为。句末标点
        改为即时落盘后，句子的 SENTENCE 在标点到达时就已经发出去了，**没有可并入的
        对象**（调用方已把它派发给 TTS/段帧，无法回改），该分支成为死代码。
        留着它只会让读者以为这个特性还在，故删除，并把新语义钉进用例：
        emoji-only 尾句是**独立句子**。附带影响为零——TTS 侧本来就有 emoji-only
        门控（见 `core/text/tests/test_emoji_consumers.py::TestTTSEmojiGate`），
        不会为它合成音频；显示侧由 TEXT_DELTA 承载，与本事件无关。
        """
        for tail in self._splitter.flush():
            events.append(StreamEvent(type=StreamEventType.SENTENCE, text=tail))
