"""面向 WebSocket 音频流的 WebRTC VAD 音频缓冲。

接收浏览器麦克风的原始 Int16 PCM 块，通过 WebRTC VAD（基于高斯混合模型的
语音活动检测）识别语音片段，并将完整 WAV 片段送入 STT。
"""

import asyncio
import logging
import struct
import time
from typing import Callable, Coroutine

import enum

import webrtcvad

from core.input.wakeword import WakeWordDetector

logger = logging.getLogger(__name__)


class WakeState(enum.Enum):
    AWAITING = "awaiting"    # 等待唤醒词，STT 结果丢弃
    WOKEN = "woken"          # 已唤醒，等待用户开始说话 (5s 超时)
    LISTENING = "listening"  # 用户正在说话，VAD 累积中

Callback = Callable[[], Coroutine[object, object, None]]
TranscriptionCallback = Callable[[str], Coroutine[object, object, None]]

# WebRTC VAD 帧尺寸（16kHz 单声道 16-bit）。
# 20ms = 320 样本 × 2 字节 = 640 字节。
_FRAME_MS = 20
_FRAME_SAMPLES = int(16000 * _FRAME_MS / 1000)   # 320
_FRAME_BYTES = _FRAME_SAMPLES * 2                  # 640


def pcm_to_wav(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    bits: int = 16,
) -> bytes:
    """给原始 PCM 数据加上 WAV 头。纯 Python 实现，无外部依赖。

    Args:
        pcm_data: 原始 PCM 音频数据（Int16 样本）。
        sample_rate: 音频采样率（Hz）。
        channels: 音频通道数（1 = 单声道）。
        bits: 每个样本的位深。

    Returns:
        bytes: 含 RIFF 头的 WAV 文件数据。
    """
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm_data)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ",
        16,             # 子块大小 (PCM)
        1,              # 音频格式 (PCM=1)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )
    return header + pcm_data


class AudioBuffer:
    """缓冲原始 PCM 音频块，并通过 WebRTC VAD 检测语音片段。

    使用高斯混合模型区分语音与噪声（键盘、风扇、背景音乐），
    不同于仅看能量水平的 RMS。

    语音片段结束（静音超时）时，累积 PCM 数据并包装为 WAV 发给 STT。
    结果经回调交付。
    """

    # 最小片段时长（秒），更短的片段大概率是噪声
    MIN_SEGMENT_SECONDS = 0.3
    # WOKEN 超时 —— 唤醒词后用户须在该秒数内开始说话
    WOKEN_TIMEOUT = 5.0
    # 触发打断前所需的连续语音块数（避免
    # 背景噪声导致的单帧误报）。
    MIN_SPEECH_CHUNKS_FOR_INTERRUPT = 3

    def __init__(
        self,
        *,
        aggressiveness: int = 3,
        silence_duration: float = 1.5,
        sample_rate: int = 16000,
        on_interrupt: Callback | None = None,
        on_transcription: TranscriptionCallback | None = None,
        stt_transcribe: "Callable[[bytes], Coroutine[object, object, str]] | None" = None,
        wake_words: list[str] | None = None,
        on_wake_notify: Callback | None = None,
    ):
        """用 VAD 与 STT 管线初始化音频缓冲。

        Args:
            aggressiveness: WebRTC VAD 灵敏度（0=最不灵敏，3=最灵敏）。
            silence_duration: 结束语音片段的静音时长（秒）。
            sample_rate: 音频采样率（Hz）。
            on_interrupt: 检测到语音打断时的回调。
            on_transcription: 收到转写文本时调用的回调。
            stt_transcribe: 语音转文本的异步 callable(bytes) -> str。
            wake_words: 用于语音激活的唤醒词列表。
            on_wake_notify: 检测到唤醒词时的回调。
        """
        self._aggressiveness = aggressiveness
        self._silence_dur = silence_duration       # 结束片段的静音秒数
        self._sample_rate = sample_rate
        self._on_interrupt = on_interrupt
        self._on_transcription = on_transcription
        self._stt_transcribe = stt_transcribe      # STT 可调用对象
        self._on_wake_notify = on_wake_notify

        self._chunks: list[bytes] = []
        self._speaking: bool = False
        self._speech_chunk_count: int = 0           # 连续语音块数
        self._interrupt_fired: bool = False         # 本片段已发送打断
        self._silence_start: float | None = None
        self._deafen_until: float = 0.0            # 忽略音频直到该时间戳
        self._last_interrupt: float = 0.0          # 上次打断时间（冷却用）
        self._interrupt_cooldown: float = 0.5      # 打断之间的最小秒数

        # 唤醒词状态
        if wake_words:
            self._wake_detector = WakeWordDetector(
                wake_words=wake_words,
                on_wake=self._on_wake,
            )
            self._wake_state = WakeState.AWAITING if self._wake_detector.enabled else None
            if not self._wake_detector.enabled:
                logger.warning(
                    "Wake words configured but openwakeword unavailable. "
                    "STT will pass through unfiltered."
                )
        else:
            self._wake_detector = None
            self._wake_state = None
        self._woken_at: float = 0.0

    def deafen(self, duration: float = 1.0) -> None:
        """忽略音频 `duration` 秒。TTS 开始播放时调用。"""
        self._deafen_until = time.monotonic() + duration
        self._chunks.clear()
        self._speaking = False
        self._speech_chunk_count = 0
        self._interrupt_fired = False
        self._silence_start = None
        if self._wake_detector:
            self._wake_detector.reset()

    def _on_wake(self) -> None:
        """WakeWordDetector 检测到唤醒词时调用。"""
        if self._wake_state == WakeState.LISTENING:
            return  # 已在监听，继续累积

        self._wake_state = WakeState.WOKEN
        self._woken_at = time.monotonic()
        self._chunks.clear()
        self._speaking = False
        self._speech_chunk_count = 0
        self._interrupt_fired = False
        self._silence_start = None

        logger.debug("Wake word detected → WOKEN")

    def _is_speech(self, chunk: bytes) -> bool:
        """用 WebRTC VAD 检查 PCM 块是否包含语音。

        将块切成 20ms 帧并逐帧检查。
        每次调用创建新的 VAD 实例以避免状态漂移 ——
        WebRTC VAD 会累积噪声估计，处理大量块后可能导致误判
        为静音。

        Args:
            chunk: 原始 Int16 PCM 音频块。

        Returns:
            bool: 块中检测到语音时返回 True。
        """
        if len(chunk) < _FRAME_BYTES:
            return False

        # 每次调用使用新的 VAD —— 避免噪声估计漂移
        vad = webrtcvad.Vad(self._aggressiveness)
        speech_frames = 0
        total_frames = 0
        for offset in range(0, len(chunk) - _FRAME_BYTES + 1, _FRAME_BYTES):
            frame = chunk[offset:offset + _FRAME_BYTES]
            try:
                if vad.is_speech(frame, self._sample_rate):
                    speech_frames += 1
            except Exception:
                logger.warning("VAD 帧处理失败", exc_info=True)
            total_frames += 1

        if total_frames == 0:
            return False

        return speech_frames > total_frames * 0.5

    async def feed(self, chunk: bytes) -> None:
        """喂入原始 Int16 PCM 块。片段完成时触发 STT。"""
        # 静音期间忽略音频（TTS 播放 / 回声抑制）
        if time.monotonic() < self._deafen_until:
            return

        # 唤醒词流式检测（与 VAD 并行）
        if self._wake_detector and self._wake_detector.enabled:
            prev_state = self._wake_state
            self._wake_detector.feed(chunk)
            # 刚检测到唤醒词时通知（须在异步上下文中）
            if prev_state != self._wake_state and self._wake_state == WakeState.WOKEN:
                if self._on_wake_notify:
                    await self._on_wake_notify()

        # WOKEN 超时检查
        if self._wake_state == WakeState.WOKEN:
            if time.monotonic() - self._woken_at > self.WOKEN_TIMEOUT:
                self._wake_state = WakeState.AWAITING
                logger.debug("WOKEN timeout → AWAITING")

        is_speech = self._is_speech(chunk)

        if is_speech:
            self._silence_start = None
            if not self._speaking:
                self._speaking = True
                self._speech_chunk_count = 0
                self._interrupt_fired = False
            self._speech_chunk_count += 1
            self._chunks.append(chunk)

            # 首次语音时 WOKEN → LISTENING
            if self._wake_state == WakeState.WOKEN:
                self._wake_state = WakeState.LISTENING

            # 连续语音块数足够时才触发打断
            if not self._interrupt_fired and self._speech_chunk_count >= self.MIN_SPEECH_CHUNKS_FOR_INTERRUPT:
                now = time.monotonic()
                if now - self._last_interrupt >= self._interrupt_cooldown:
                    self._last_interrupt = now
                    self._interrupt_fired = True
                    if self._on_interrupt:
                        await self._on_interrupt()
        elif self._speaking:
            now = time.monotonic()
            if self._silence_start is None:
                self._silence_start = now
                self._chunks.append(chunk)  # 连同尾部静音一并交 STT
            elif now - self._silence_start >= self._silence_dur:
                await self._process_segment()

    async def flush(self) -> None:
        """强制处理任何剩余的缓冲音频。"""
        if self._chunks:
            await self._process_segment()

    async def _process_segment(self) -> None:
        audio = b"".join(self._chunks)
        self._chunks.clear()
        self._speaking = False
        self._speech_chunk_count = 0
        self._interrupt_fired = False
        self._silence_start = None

        min_bytes = int(self._sample_rate * self.MIN_SEGMENT_SECONDS * 2)
        if len(audio) < min_bytes:
            return  # 太短，大概率是噪声

        # 门控：启用唤醒词时只在 LISTENING 状态处理
        if self._wake_state is not None and self._wake_state != WakeState.LISTENING:
            return

        if self._stt_transcribe is None:
            logger.debug("No STT configured, dropping %d bytes of audio", len(audio))
            return

        # S3（review 复验）：段边界不再整帧截断——VAD 检测（_is_speech）只扫整帧
        # 640B/20ms，但整段音频（含非整帧尾部）原样送 STT。喂入 chunk 不强制 640
        # 倍数（feed 不校验），join 后全段交 STT，尾部残帧不丢。
        wav = pcm_to_wav(audio, sample_rate=self._sample_rate)
        try:
            text = await asyncio.wait_for(
                self._stt_transcribe(wav),
                timeout=30.0,
            )
            if text and text.strip() and self._on_transcription:
                stripped = text.strip()
                # SenseVoice nospeech 已在 STT backend 层过滤 (raw_text 检测)
                await self._on_transcription(stripped)
        except asyncio.TimeoutError:
            logger.warning("STT timed out (segment length=%d bytes)", len(audio))
        except Exception:
            logger.warning("STT transcription failed", exc_info=True)
        finally:
            # STT 结束后回到 AWAITING（无论成功失败）
            if self._wake_state == WakeState.LISTENING:
                self._wake_state = WakeState.AWAITING

    def set_wake_enabled(self, enabled: bool) -> None:
        """运行时启用或禁用唤醒词检测。"""
        if enabled and self._wake_detector and self._wake_detector.enabled:
            self._wake_state = WakeState.AWAITING
            logger.debug("Wake word detection enabled → AWAITING")
        else:
            self._wake_state = None  # 直通模式
            logger.debug("Wake word detection disabled → passthrough")

    def reset(self) -> None:
        """清空所有缓冲状态（如被打断时）。"""
        self._chunks.clear()
        self._speaking = False
        self._speech_chunk_count = 0
        self._interrupt_fired = False
        self._silence_start = None
        if self._wake_detector:
            self._wake_detector.reset()
