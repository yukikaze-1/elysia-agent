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
import struct
import time
from typing import TYPE_CHECKING

from Module.output import ChannelCapability, OutputManager
from Module.tts import TTSBackend

# 共享标签定义（core.text.tags 收口，替代本模块本地正则）
#   STAGE_CN_RE        — 中文括号舞台指示（如 (清了清嗓子)、（眨了眨眼））：显示但 TTS 不合成
#   is_emoji_only      — 纯 emoji 句判定（跳过 TTS 合成）
#   strip_display_hidden — 显示剥离：PAUSE <#x#> + 英文语气词 (sighs)（送到 TTS 但不显示在前端）
from core.text.tags import STAGE_CN_RE, is_emoji_only, strip_display_hidden

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


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

def _wav_info(audio: bytes) -> tuple[int | None, int | None]:
    """解析 WAV header → (sample_rate, duration_ms)；非 WAV/异常返回 (None, None)。

    遍历 RIFF chunk 表定位 fmt/data（不假设标准 44 字节头）——含额外 chunk
    （LIST/fact/cue）的 WAV 同样正确解析（spec §3.11）。
    """
    try:
        if len(audio) < 12 or audio[:4] != b"RIFF":
            return None, None
        sample_rate: int | None = None
        byte_rate: int | None = None
        offset = 12                      # 跳过 RIFF 头（12 字节），进入 chunk 表
        while offset + 8 <= len(audio):
            chunk_id = audio[offset:offset + 4]
            size = struct.unpack("<I", audio[offset + 4:offset + 8])[0]
            if chunk_id == b"fmt " and offset + 20 <= len(audio):
                sample_rate = struct.unpack("<I", audio[offset + 12:offset + 16])[0]
                byte_rate = struct.unpack("<I", audio[offset + 16:offset + 20])[0]
            elif chunk_id == b"data":
                duration_ms = int(size * 1000 / byte_rate) if byte_rate else None
                return sample_rate, duration_ms
            offset += 8 + size + (size & 1)   # chunk 数据 + 奇偶对齐
        return sample_rate, None
    except Exception:
        return None, None


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
        self._enabled = True  # TTS 启用开关，仅由 set_enabled 控制（热插拔门控）
        self._preprocess = lambda text: text  # 默认：不预处理
        if tts is not None:
            fn = tts.get_preprocess()
            if fn is not None:
                self._preprocess = fn
        self._emotion: str | None = None
        self._voice: str | None = None  # 音色覆盖（每 turn 注入；None 走后端默认）
        self._tasks: list[asyncio.Task] = []
        self._task_by_sid: dict[int, asyncio.Task] = {}  # sid → 任务（BE-063 cancel）
        self._audio_forwarding: set[int] = set()         # 已开始转发的 sid（cancel 补 audio_cancelled）
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
        if not self._enabled:
            return
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

        # 显示文本：enqueue 时算一次（任务局部自持，不共享注册表），
        # 禁用取消时由 CancelledError 分支按此自刷，排队未播句不丢
        display_text = strip_display_hidden(sentence).strip()
        action_text = ""
        m = STAGE_CN_RE.match(display_text)
        if m:
            action_text = m.group()
            display_text = display_text[m.end():].strip()

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
            self._download_and_forward(sid, seq, sentence, display_text, action_text),
            name=f"tts-{sid}",
        )
        self._tasks.append(task)
        self._task_by_sid[sid] = task
        # 完成清理
        self._tasks = [t for t in self._tasks if not t.done()]
        self._task_by_sid = {s: t for s, t in self._task_by_sid.items() if not t.done()}

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
        # 同步剪除 _task_by_sid——否则窗口期 audio.cancel 对刚被取消的 sid 返回 True
        # 而任务不再补 audio_cancelled（语义失真，review N3）
        self._task_by_sid = {s: t for s, t in self._task_by_sid.items() if not t.done()}

    def cancel_stream(self, stream_id: int) -> bool:
        """取消指定播放实例（BE-063）；正在转发（audio_start 后 audio_end 前）返回
        True（任务补 audio_cancelled），已 audio_end / 不存在返回 False（PLAYBACK_NOT_FOUND，
        spec §8.2）。判定与 CancelledError 分支是否补 audio_cancelled 对齐——用
        _audio_forwarding 而非 task 存活，否则 audio_end 后的 emoji/finally 窗口
        返回 True 却无事件（spec §3.9）。"""
        task = self._task_by_sid.get(stream_id)
        if task is None or task.done():
            return False
        if stream_id not in self._audio_forwarding:
            return False           # 已 audio_end（末尾 emoji/finally 窗口）——不再补事件
        task.cancel()
        return True

    def set_emotion(self, emotion: str | None) -> None:
        """设置本轮 TTS 情绪。自由文本会自动映射为 TTS 参数 (happy/sad/neutral)。

        Args:
            emotion: 自由文本情绪描述，为 None 时清除情绪设置。
        """
        self._emotion = _mood_text_to_tts_emotion(emotion) if emotion else None

    def set_voice(self, voice: str | None) -> None:
        """设置 TTS 音色 ID 覆盖（每 turn 注入；None 清除走后端默认）。

        声音是 per-turn 可变状态，由 CognitiveLoop 每轮解析发起会话的
        ``service_state.tts_voice`` 后注入——不随 reset_for_turn() 重置，
        set_voice 幂等，重复注入同值无副作用。

        Args:
            voice: 音色 ID（须在激活后端 list_voices 结果内），None 走后端默认。
        """
        self._voice = voice

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
        """TTS 后端是否可用（active 语义保持"后端可用"，不随 enabled 变化）。"""
        return self._tts is not None

    @property
    def enabled(self) -> bool:
        """TTS 是否启用（由 set_enabled 控制，默认 True）。"""
        return self._enabled

    async def set_enabled(self, enabled: bool) -> None:
        """设置 TTS 启用状态（热插拔门控的唯一入口）。

        禁用时：置位 → 捕获 pending（cancel_all 会清 _tasks，须先捕获）→
        对每个任务置 per-task flush_on_cancel（让被取消任务自刷排队未播文本）→
        cancel_all() → drain（wait_for 对齐 _lifecycle_services 超时模式）→
        flush_emojis()（纯 emoji 句无任务，自刷覆盖不到）。TimeoutError 内吞：
        flush_on_cancel 已置、cancel_all 已发，禁用本身已生效，异常冒泡到
        set_service 会跳过 session.save() + capabilities 重播。
        """
        self._enabled = enabled
        if not enabled:
            # 先捕获 pending——cancel_all() 会 clear _tasks，之后 gather 拿到空列表
            pending = [t for t in self._tasks if not t.done()]
            for t in pending:
                t.flush_on_cancel = True  # 仅禁用路径置位，保证只有禁用触发文本兜底
            self.cancel_all()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=3.0)
                # drain 完成 → 被取消任务已自刷文本 → 才 flush 纯 emoji 句
                await self.flush_emojis()
            except asyncio.TimeoutError:
                # 超时分支：不清除 _pending_emojis——被僵尸客户端卡住的任务稍后
                # 完成时仍会广播其 display_text，此时 flush emoji 会让 emoji 提前
                # 发出、依附到上一句（顺序破坏）。跳过 flush 保序，残留 emoji 随
                # reset_for_turn 丢弃。
                logger.warning("TTS disable drain 超时，跳过 emoji flush")

    # ---- 内部 ----

    async def _download_and_forward(
        self,
        sid: int,
        seq: int,
        sentence: str,
        display_text: str,
        action_text: str,
    ) -> None:
        """下载 TTS 音频后按序转发到前端。

        Phase 1（并行）: 发送文本到云端，下载完整音频。
        Phase 2（串行）: 按 seq 顺序分块转发音频到前端。

        Args:
            sid: 全局流 ID（跨轮次递增）。
            seq: 本轮序号（用于门控排序）。
            sentence: 待合成的句子文本（用于 TTS 文本与字符统计）。
            display_text: enqueue 时算好的显示文本（任务局部自持，不共享注册表）。
            action_text: 句首中文舞台指示（单独一行显示）。
        """
        if not self._enabled:
            # 禁用后尚未开始的任务：flush_on_cancel 置位时自刷排队未播文本。
            # 此分支在首个 await 前同步 return，进不到下方 try 的 CancelledError
            # 路径——不在此兜底会丢句子文本（违反"TTS 关闭不丢句"不变式）。
            if getattr(asyncio.current_task(), "flush_on_cancel", False):
                try:
                    await self._output._broadcast(
                        "on_text_delta", display_text,
                        capability=ChannelCapability.TEXT_DELTA,
                        stream_id=sid,
                    )
                    await self._output.send("")
                except Exception:
                    pass
            return
        if not self._tts:
            return

        # TTS 文本：保留停顿标签（MiniMax 原生支持 <#x#> 产生语音停顿），
        # 去掉中文舞台指示（英文语气标签保留，MiniMax 原生支持）
        tts_text = STAGE_CN_RE.sub("", sentence).strip()

        # 文本是否已广播：False → 禁用取消时自刷兜底；True → 音频流中取消不重复广播
        text_broadcast = False

        try:
            # Phase 1: 下载音频（并行，与其他句同时进行）
            t_download = time.perf_counter()
            audio = await self._tts.synthesize(
                tts_text, character=self._char_name, emotion=self._emotion,
                voice_id=self._voice,
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
            text_broadcast = True  # 文本已广播，禁用取消时不再自刷（防双播）
            await self._output._broadcast(
                "on_text_delta", display_text,
                capability=ChannelCapability.TEXT_DELTA,
                stream_id=sid,
            )
            await self._output.send("")
            # BE-063：句开始播放（stream_id = playback ID）+ WAV 元数据
            fmt = self._tts.stream_format if self._tts else "wav"
            sr, dur = _wav_info(audio)
            self._audio_forwarding.add(sid)
            await self._output.audio_start(sid, format=fmt, sample_rate=sr, duration_ms=dur)
            chunk_size = 4096
            for offset in range(0, len(audio), chunk_size):
                await self._output.audio_chunk(
                    audio[offset:offset + chunk_size], stream_id=sid,
                )
            await self._output.audio_end(sid, format=fmt)
            self._audio_forwarding.discard(sid)
            # 发排在本句之后的 emoji
            for e in self._pending_emojis.pop(sid, []):
                await self._output._broadcast(
                    "on_text_delta", e,
                    capability=ChannelCapability.TEXT_DELTA,
                    stream_id=sid,
                )
                await self._output.send("")
        except asyncio.CancelledError:
            # 禁用兜底：per-task flush_on_cancel 时自刷排队未播文本（须读
            # current_task——self 是 TTSStreamManager 不是任务，读 self 会复活
            # 全局标志竞态；打断/异常取消的任务无该属性，getattr 兜底 False）
            if (
                getattr(asyncio.current_task(), "flush_on_cancel", False)
                and not text_broadcast
            ):
                await self._output._broadcast(
                    "on_text_delta", display_text,
                    capability=ChannelCapability.TEXT_DELTA,
                    stream_id=sid,
                )
                await self._output.send("")
            # BE-063：已开始转发的句被取消 → 补 audio_cancelled（前端清理孤儿 chunk）
            if sid in self._audio_forwarding:
                await self._output.audio_cancelled(sid)
                self._audio_forwarding.discard(sid)
            raise  # 上抛给任务
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
                sentence, character=self._char_name, voice_id=self._voice,
            )
            await self._output.audio_chunk(audio)
        except Exception:
            logger.warning("TTS synthesis failed", exc_info=True)
