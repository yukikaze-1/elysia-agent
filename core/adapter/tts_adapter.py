"""TTS 适配器 — 句级流式合成 + emoji 排队同步。

封装 Module/tts TTSBackend，提供流式播放管理、emoji 与 TTS
句子的绑定排队、热切换 TTS 后端。

设计原则:
- 文字和音频绑定发送：前端收到即可渲染+播放，无需同步逻辑
- emoji 排队依附在 TTS 句前后——emoji-only 句先排队，下一句 TTS
  开始时自动排在本句文字之前/之后发送
- TTS workers=1 FIFO 保证句间顺序
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

from Module.output import ChannelCapability, OutputManager
from Module.tts import TTSBackend

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---- Emoji 检测 -----------------------------------------------------------

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF"
    r"\U0001F600-\U0001F64F"
    r"\U00002600-\U000027BF"
    r"\U00002300-\U000023FF]+",
    flags=re.UNICODE,
)


def is_emoji_only(text: str) -> bool:
    """句子去掉 emoji 后是否只剩空白 — 用于跳过 TTS 合成。

    Args:
        text: 待检测的句子。

    Returns:
        bool: 纯 emoji 句时为 True。
    """
    return not _EMOJI_RE.sub("", text).strip()


# MiniMax 等 TTS 引擎的内部控制标签，送到 TTS 但不显示在前端
_PAUSE_TAG_RE = re.compile(r"<#\d+\.?\d*#>")
# MiniMax 英文语气标签：送到 TTS 合成语气
_TONE_TAG_RE = re.compile(
    r"\((?:laughs|sighs|crying|gasps|breath|humming|sniffs|sneezes|"
    r"coughs|groans|pant|chuckle|clears?\s?throat|applause|inhale|exhale|"
    r"snorts|burps|lip.?smacking|hissing|emm|whistles|sneezes)\)",
    re.IGNORECASE,
)
# 中文括号舞台指示（如 (清了清嗓子)、（眨了眨眼））：显示但 TTS 不合成
_CN_ACTION_RE = re.compile(r"[\(\（][^\)\）]*[一-鿿][^\)\）]*[\)\）]")


def _mood_text_to_tts_emotion(mood_text: str) -> str:
    """自由文本 mood → TTS 情绪参数 (happy/sad/neutral)。

    Args:
        mood_text: 自由文本情绪描述。

    Returns:
        str: TTS 情绪参数，值为 "happy"、"sad" 或 "neutral"。
    """
    sad_words = {"难过", "悲伤", "伤心", "低落", "沮丧", "消沉", "失落"}
    happy_words = {"开心", "高兴", "兴奋", "快乐", "喜悦", "期待", "惊喜"}
    # happy 优先 — "虽然难过但还是很开心" → happy
    if any(w in mood_text for w in happy_words):
        return "happy"
    if any(w in mood_text for w in sad_words):
        return "sad"
    return "neutral"


# ---- TTSStreamManager -----------------------------------------------------

class TTSStreamManager:
    """管理 TTS 流式播放、emoji 排队、句间同步。

    每轮对话开始时调用 reset_for_turn()，
    每个句子调用 stream_sentence()（自动区分 TTS 句和 emoji 句），
    所有句子处理完后调用 flush_emojis() 和 wait_all()。
    """

    def __init__(
        self,
        tts: TTSBackend | None,
        output: OutputManager,
        character_name: str,
    ) -> None:
        """初始化 TTS 流管理器。

        Args:
            tts: TTS 后端实例，为 None 时 TTS 不可用。
            output: 输出管理器，用于广播音频和文本到前端。
            character_name: 角色名称，传递给 TTS 后端用于音色选择。
        """
        self._tts = tts
        self._output = output
        self._char_name = character_name
        self._preprocess = lambda text: text  # 默认：不预处理
        if tts is not None:
            fn = tts.get_preprocess()
            if fn is not None:
                self._preprocess = fn
        self._emotion: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._total: int = 0       # 全局计数器，跨轮次持续递增
        self._seq: int = 0          # 本轮计数器，reset_for_turn() 时归零
        self._turn_chars: int = 0   # 本轮累计字符数
        self._turn_bytes: int = 0   # 本轮累计音频字节数
        self._pending_emojis: dict[int, list[str]] = {}
        self._release_events: dict[int, asyncio.Event] = {}  # seq → event 门控

    # ---- 公开 API ----

    def reset_for_turn(self, emotion: str | None = None) -> None:
        """每轮 process() 开始时调用。

        Args:
            emotion: 本轮 TTS 情绪参数（自由文本，自动映射为 happy/sad/neutral）。
        """
        self._emotion = emotion
        self._seq = 0
        self._turn_chars = 0
        self._turn_bytes = 0
        self._pending_emojis.clear()
        self._release_events.clear()
        # 第一个句子无需等待
        self._release_events[1] = asyncio.Event()
        self._release_events[1].set()

    def stream_sentence(self, sentence: str) -> None:
        """启动一句的 TTS 后台任务。

        自动判断：纯 emoji 句排队到上一句之后，其余送 TTS 合成。
        TTS 不可用时是 no-op。

        Args:
            sentence: 待合成的句子文本。
        """
        if not self._tts:
            return
        if is_emoji_only(sentence):
            # Emoji 句：排队，依附到最近一句 TTS 后面
            self._pending_emojis.setdefault(self._total, []).append(sentence)
            return

        # 跳过空句 — 预处理后为空会导致 TTS API 返回 400
        _clean = self._preprocess(sentence)
        if not _clean:
            return

        self._total += 1
        self._seq += 1
        sid = self._total
        seq = self._seq
        # 提前创建门控 event，保证 seq 不存在时也能 wait
        if seq not in self._release_events:
            self._release_events[seq] = asyncio.Event()
        logger.debug("TTS 句 %d (总#%d) 开始 (%d 字符)", seq, sid, len(sentence))
        logger.debug("TTS 句内容: %s", _clean[:60])

        task = asyncio.create_task(
            self._download_and_forward(sid, seq, sentence),
            name=f"tts-{sid}",
        )
        self._tasks.append(task)
        # Clean up completed tasks
        self._tasks = [t for t in self._tasks if not t.done()]

    async def flush_emojis(self) -> None:
        """发送所有还未排出的 emoji（最后一句 TTS 之后的）。

        调用方须保证在 wait_all() 之后调用——否则最后一句的 pop(sid-1)
        尚未执行，残留 emoji 会以无 stream_id 方式提前广播，破坏"句子😊"依附顺序。
        """
        for seq in sorted(self._pending_emojis):
            for e in self._pending_emojis[seq]:
                await self._output._broadcast(
                    "on_text_delta", e,
                    capability=ChannelCapability.TEXT_DELTA,
                )
                await self._output.send("")
        self._pending_emojis.clear()

    async def wait_all(self) -> None:
        """等待所有 TTS 任务完成。"""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            logger.debug("等待 %d 个 TTS 任务完成", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
            self._tasks.clear()
        # 汇总一行 INFO：本轮 TTS 合成/播放统计
        if self._seq > 0:
            kb = self._turn_bytes / 1000
            logger.info(
                "TTS: %d 句 %d 字符 → %.0fKB 已播放",
                self._seq, self._turn_chars, kb,
            )

    def cancel_all(self) -> None:
        """取消所有进行中的 TTS 任务。"""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            logger.debug("TTS: cancelled %d tasks", len(pending))
        for t in pending:
            t.cancel()
        self._tasks.clear()

    def set_emotion(self, emotion: str | None) -> None:
        """设置本轮 TTS 情绪。自由文本会自动映射为 TTS 参数 (happy/sad/neutral)。

        Args:
            emotion: 自由文本情绪描述，为 None 时清除情绪设置。
        """
        self._emotion = _mood_text_to_tts_emotion(emotion) if emotion else None

    def set_backend(self, tts: TTSBackend | None) -> None:
        """热切换 TTS 后端。

        Args:
            tts: 新的 TTS 后端实例，为 None 时禁用 TTS。
        """
        self._tts = tts
        if tts is not None:
            fn = tts.get_preprocess()
            self._preprocess = fn if fn is not None else lambda text: text
        else:
            self._preprocess = lambda text: text

    @property
    def active(self) -> bool:
        """TTS 后端是否可用。"""
        return self._tts is not None

    # ---- 内部 ----

    async def _download_and_forward(self, sid: int, seq: int, sentence: str) -> None:
        """下载 TTS 音频后按序转发到前端。

        Phase 1（并行）: 发送文本到云端，下载完整音频。
        Phase 2（串行）: 按 seq 顺序分块转发音频到前端。

        Args:
            sid: 全局流 ID（跨轮次递增）。
            seq: 本轮序号（用于门控排序）。
            sentence: 待合成的句子文本。
        """
        if not self._tts:
            return

        # 显示文本：去掉停顿标签（前端不显示 <#x#>）
        display_text = _PAUSE_TAG_RE.sub("", sentence).strip()
        # 提取句首中文舞台指示，单独一行显示
        action_text = ""
        m = _CN_ACTION_RE.match(display_text)
        if m:
            action_text = m.group()
            display_text = display_text[m.end():].strip()
        # TTS 文本：保留停顿标签（MiniMax 原生支持 <#x#> 产生语音停顿），
        # 去掉中文舞台指示（英文语气标签保留，MiniMax 原生支持）
        tts_text = _CN_ACTION_RE.sub("", sentence).strip()

        try:
            # Phase 1: 下载音频（并行，与其他句同时进行）
            t_download = time.perf_counter()
            audio = await self._tts.synthesize(
                tts_text, character=self._char_name, emotion=self._emotion,
            )
            t_download_elapsed = (time.perf_counter() - t_download) * 1000
            logger.debug(
                "TTS 句 %d (总#%d) 下载完成 (%d ms, %d bytes)",
                seq, sid, int(t_download_elapsed), len(audio),
            )

            # Phase 2: 按序转发（串行，门控保证播放顺序）
            await self._release_events[seq].wait()
            self._turn_chars += len(sentence)
            self._turn_bytes += len(audio)
            logger.debug(
                "TTS 句 %d (总#%d) 开始播放 (%d 字符, %d bytes)",
                seq, sid, len(sentence), len(audio),
            )
            # 先发排在本句之前的 emoji
            for e in self._pending_emojis.pop(sid - 1, []):
                await self._output._broadcast(
                    "on_text_delta", e,
                    capability=ChannelCapability.TEXT_DELTA,
                    stream_id=sid,
                )
                await self._output.send("")
            # 句首舞台指示单独一行
            if action_text:
                await self._output._broadcast(
                    "on_text_delta", action_text,
                    capability=ChannelCapability.TEXT_DELTA,
                    stream_id=sid,
                )
                await self._output.send("")
            # 发本句文字
            await self._output._broadcast(
                "on_text_delta", display_text,
                capability=ChannelCapability.TEXT_DELTA,
                stream_id=sid,
            )
            await self._output.send("")
            # 分块转发音频
            chunk_size = 4096
            for offset in range(0, len(audio), chunk_size):
                await self._output.audio_chunk(
                    audio[offset:offset + chunk_size], stream_id=sid,
                )
            fmt = self._tts.stream_format if self._tts else "wav"
            await self._output.audio_end(sid, format=fmt)
            # 发排在本句之后的 emoji
            for e in self._pending_emojis.pop(sid, []):
                await self._output._broadcast(
                    "on_text_delta", e,
                    capability=ChannelCapability.TEXT_DELTA,
                    stream_id=sid,
                )
                await self._output.send("")
        except asyncio.CancelledError:
            raise  # propagate to task
        except Exception:
            logger.warning(
                "TTS 句 %d (总#%d) 下载/播放失败", seq, sid, exc_info=True,
            )
            # TTS 失败兜底: 至少把文字发出去
            try:
                await self._output._broadcast(
                    "on_text_delta", display_text,
                    capability=ChannelCapability.TEXT_DELTA,
                    stream_id=sid,
                )
                await self._output.send("")
            except Exception:
                pass
        finally:
            # 无论成功/失败/取消，都释放下一句的门控（避免后续句子永久卡住）
            # 若下一句尚未创建 event，则先创建再 set，防止死锁：
            #   句 N 完成时句 N+1 的 stream_sentence() 尚未被调用 → event 未创建 → set 跳过
            #   → 之后句 N+1 创建 event 永远等不到 set
            if seq + 1 not in self._release_events:
                self._release_events[seq + 1] = asyncio.Event()
            self._release_events[seq + 1].set()

    async def synthesize_legacy(self, sentence: str) -> None:
        """Legacy 非流式 TTS — 保留向后兼容（供测试用）。

        流式路径请用 stream_sentence() + wait_all()。

        Args:
            sentence: 待合成的句子文本。
        """
        if not self._tts:
            return
        try:
            audio = await self._tts.synthesize(
                sentence, character=self._char_name,
            )
            await self._output.audio_chunk(audio)
        except Exception:
            logger.debug("TTS synthesis failed", exc_info=True)
