"""TTS 流式播放管理器 — 句级流式合成 + emoji 排队同步。

从 cognitive_loop.py 提取。管理 TTS 后台任务生命周期、emoji 与 TTS
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
    """句子去掉 emoji 后是否只剩空白 — 用于跳过 TTS 合成。"""
    return not _EMOJI_RE.sub("", text).strip()


# MiniMax 等 TTS 引擎的内部控制标签，应该送到 TTS 但不显示在前端
_PAUSE_TAG_RE = re.compile(r"<#\d+\.?\d*#>")


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
        self._tts = tts
        self._output = output
        self._char_name = character_name
        self._preprocess = lambda text: text  # 默认：不预处理
        if tts is not None:
            fn = tts.get_preprocess()
            if fn is not None:
                self._preprocess = fn
        self._tasks: list[asyncio.Task] = []
        self._total: int = 0       # 全局计数器，跨轮次持续递增
        self._seq: int = 0          # 本轮计数器，reset_for_turn() 时归零
        self._pending_emojis: dict[int, list[str]] = {}

    # ---- 公开 API ----

    def reset_for_turn(self) -> None:
        """每轮 process() 开始时调用。"""
        self._seq = 0
        self._pending_emojis.clear()

    def stream_sentence(self, sentence: str) -> None:
        """启动一句的 TTS 后台任务。

        自动判断：纯 emoji 句排队到上一句之后，其余送 TTS 合成。
        TTS 不可用时是 no-op。
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
        logger.info("TTS 句 %d (总#%d) 开始 (%d 字符)", seq, sid, len(sentence))
        logger.debug("TTS 句内容: %s", _clean[:60])

        task = asyncio.create_task(
            self._stream_one(sid, seq, sentence),
            name=f"tts-{sid}",
        )
        self._tasks.append(task)
        # Clean up completed tasks
        self._tasks = [t for t in self._tasks if not t.done()]

    async def flush_emojis(self) -> None:
        """发送所有还未排出的 emoji（最后一句 TTS 之后的）。"""
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

    def cancel_all(self) -> None:
        """取消所有进行中的 TTS 任务。"""
        for t in self._tasks:
            if not t.done():
                t.cancel()
        self._tasks.clear()

    def set_backend(self, tts: TTSBackend | None) -> None:
        """热切换 TTS 后端。"""
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

    async def _stream_one(self, sid: int, seq: int, sentence: str) -> None:
        """流式合成一句 TTS，绑定发送文字+音频。"""
        if not self._tts:
            return
        try:
            first_chunk = True
            t_tts = time.perf_counter()
            # 去掉 TTS 内部控制标签（停顿），保留语气标签（对人也有意义）
            display_text = _PAUSE_TAG_RE.sub("", sentence).strip()
            async for chunk in self._tts.synthesize_stream(
                sentence, character=self._char_name,
            ):
                if first_chunk:
                    first_chunk = False
                    logger.info(
                        "TTS 句 %d (总#%d) 首块音频到达，发送文字+音频", seq, sid,
                    )
                    # 先发排在本句之前的 emoji
                    for e in self._pending_emojis.pop(sid - 1, []):
                        await self._output._broadcast(
                            "on_text_delta", e,
                            capability=ChannelCapability.TEXT_DELTA,
                            stream_id=sid,
                        )
                        await self._output.send("")
                    # 发本句文字（去掉 TTS 内部控制标签）
                    await self._output._broadcast(
                        "on_text_delta", display_text,
                        capability=ChannelCapability.TEXT_DELTA,
                        stream_id=sid,
                    )
                    await self._output.send("")
                await self._output.audio_chunk(chunk, stream_id=sid)
            await self._output.audio_end(sid)
            t_tts_elapsed = (time.perf_counter() - t_tts) * 1000
            logger.info(
                "TTS 句 %d (总#%d) 播放完成 (%d ms)",
                seq, sid, int(t_tts_elapsed),
            )
            # 发排在本句之后的 emoji
            for e in self._pending_emojis.pop(sid, []):
                await self._output._broadcast(
                    "on_text_delta", e,
                    capability=ChannelCapability.TEXT_DELTA,
                    stream_id=sid,
                )
                await self._output.send("")
        except asyncio.CancelledError:
            pass  # INTERRUPT 打断
        except Exception:
            # TTS 不可用时静默降级到纯文本——不打印堆栈以免误导
            logger.debug("TTS stream failed — fallback 发送文字", exc_info=False)
            # TTS 失败兜底: 至少把文字发出去
            if first_chunk:
                try:
                    await self._output._broadcast(
                        "on_text_delta", display_text,
                        capability=ChannelCapability.TEXT_DELTA,
                        stream_id=sid,
                    )
                    await self._output.send("")
                except Exception:
                    pass

    async def synthesize_legacy(self, sentence: str) -> None:
        """Legacy 非流式 TTS — 保留向后兼容（供测试用）。

        流式路径请用 stream_sentence() + wait_all()。
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
