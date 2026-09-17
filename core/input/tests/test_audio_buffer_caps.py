# 测试文件：core/input/tests/test_audio_buffer_caps.py
"""S2-E2：v1 VAD 段必须有**上界**。

段结束的唯一条件是"≥`silence_duration` 静音"，`AudioBuffer` 对累积 PCM 只有**下界**
（`MIN_SEGMENT_SECONDS`）。环境持续人声/音乐/外放回声/采集卡死时 `_chunks` 线性增长，
最终被 `b"".join()` 成**一个**巨型 WAV 一次性送 STT，而 `_process_segment` 只给 30s
超时、超时**整段丢弃**——十几分钟对话一次性静默消失，只留一行 warning。

同仓 RECORDING 模式有 `MAX_RECORDING_SECONDS` / `MAX_RECORDING_BYTES` 两道闸门，
VAD 路径此前一个都没有：**同一风险在两条语音路径上不对称**。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.service

_FRAME_SAMPLES = 320          # 20ms @ 16kHz
_FRAME_BYTES = _FRAME_SAMPLES * 2


def _buffer(*, speech: bool = True, sample_rate: int = 16000,
            silence_duration: float = 1.5):
    from core.input.audio_buffer import AudioBuffer

    transcribed: list[bytes] = []

    async def _stt(wav: bytes) -> str:
        transcribed.append(wav)
        return "文本"

    buf = AudioBuffer(silence_duration=silence_duration, sample_rate=sample_rate,
                      stt_transcribe=_stt, on_transcription=AsyncMock())
    buf._is_speech = lambda chunk: speech          # noqa: SLF001 全程语音
    return buf, transcribed


@pytest.mark.asyncio
async def test_continuous_speech_cuts_segment_at_cap():
    """持续语音 → 达到上限即**主动切段**（而非一直缓冲到 STT 超时丢整段）。"""
    buf, transcribed = _buffer()
    cap_bytes = buf._max_segment_bytes()                    # noqa: SLF001

    # 喂到明显超过上限（不触发静音，模拟持续人声）
    chunk = b"\x00" * _FRAME_BYTES
    for _ in range(int(cap_bytes / _FRAME_BYTES) + 50):
        await buf.feed(chunk)

    assert transcribed, (
        "达到段上限后必须已经切段送 STT —— 否则会一路缓冲成巨型 WAV")
    assert buf._buffered_bytes() < cap_bytes, (             # noqa: SLF001
        f"切段后缓冲未回落：{buf._buffered_bytes()} bytes")  # noqa: SLF001


@pytest.mark.asyncio
async def test_buffer_never_exceeds_cap_by_much():
    """缓冲上界：任何时刻都不应远超上限（防无界增长）。"""
    buf, _ = _buffer()
    cap_bytes = buf._max_segment_bytes()                    # noqa: SLF001
    chunk = b"\x00" * _FRAME_BYTES

    peak = 0
    for _ in range(int(cap_bytes / _FRAME_BYTES) * 3):
        await buf.feed(chunk)
        peak = max(peak, buf._buffered_bytes())             # noqa: SLF001

    assert peak <= cap_bytes + _FRAME_BYTES, (
        f"峰值缓冲 {peak} 超过上限 {cap_bytes} + 一帧——缓冲无界")


@pytest.mark.asyncio
async def test_short_speech_is_unchanged():
    """回归网：短语音（正常一句）不受上限影响，仍按静音切段。"""
    # 静音阈值设 0：喂入循环在微秒内跑完，`time.monotonic()` 几乎不前进，
    # 真实阈值下"静音 1.5s"永远等不到（这是测试时钟问题，不是被测逻辑）。
    buf, transcribed = _buffer(silence_duration=0.0)
    chunk = b"\x00" * _FRAME_BYTES

    # 0.4s 语音——必须超过 `MIN_SEGMENT_SECONDS=0.3`，否则会被"太短，大概率是噪声"
    # 正确丢弃（这也是被测代码的既有语义，不是本用例要改的东西）
    for _ in range(20):
        await buf.feed(chunk)
    buf._is_speech = lambda chunk: False                    # noqa: SLF001 转静音
    for _ in range(3):
        await buf.feed(chunk)

    assert transcribed, "正常静音切段仍然要工作"


@pytest.mark.asyncio
async def test_release_frees_buffer():
    """`release()` 释放缓冲——此前 `_chunks` 没有显式释放点。"""
    buf, _ = _buffer()
    chunk = b"\x00" * _FRAME_BYTES
    for _ in range(10):
        await buf.feed(chunk)
    assert buf._buffered_bytes() > 0                        # noqa: SLF001

    buf.release()
    assert buf._buffered_bytes() == 0                       # noqa: SLF001


def test_cap_is_far_below_stt_timeout_scale():
    """上限必须小到"切出来的段一定能在 STT 超时内转写完"的量级。"""
    from core.input.audio_buffer import AudioBuffer
    from core.input.voice_sessions import STT_TIMEOUT

    assert AudioBuffer.MAX_SEGMENT_SECONDS <= 120.0
    assert AudioBuffer.MAX_SEGMENT_SECONDS < STT_TIMEOUT * 4, (
        "段上限相对 30s 的 STT 超时过大，切出来仍会超时丢段")
