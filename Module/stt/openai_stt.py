"""OpenAI 兼容 STT 后端 — 用于 custom provider（Whisper API 格式）。"""

import logging
import struct
from io import BytesIO
from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

from .protocol import STTBackend


def _make_silence_wav(duration_sec: float = 1.0, sample_rate: int = 16000) -> bytes:
    """生成静音 WAV 字节，用于连通性测试。"""
    num_samples = int(sample_rate * duration_sec)
    data = b"\x00\x00" * num_samples
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data),
        b"WAVE", b"fmt ", 16,
        1, 1, sample_rate,
        sample_rate * 2, 2, 16,
        b"data", len(data),
    )
    return header + data


class OpenAICompatSTTBackend(STTBackend):
    """OpenAI 兼容语音识别后端 (Whisper API 格式)。"""

    def __init__(
        self,
        model: str = "whisper-1",
        *,
        api_key: str = "",
        base_url: str = "",
        logger: logging.Logger | None = None,
    ):
        self._model = model
        self._logger = logger or logging.getLogger(__name__)
        self._client = AsyncOpenAI(
            api_key=api_key or "stt",
            base_url=base_url,
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
        self.last_emotion: str = "NEUTRAL"
        self.last_events: list[str] = []

    async def transcribe(self, audio: bytes, **kwargs) -> str:
        result = await self._client.audio.transcriptions.create(
            model=self._model,
            file=("audio.wav", BytesIO(audio), "audio/wav"),
            **kwargs,
        )
        self.last_emotion = "NEUTRAL"
        self.last_events = []
        return result.text

    async def transcribe_stream(self, audio: bytes, **kwargs) -> AsyncIterator[str]:
        text = await self.transcribe(audio, **kwargs)
        self.last_emotion = "NEUTRAL"
        self.last_events = []
        yield text

    async def close(self) -> None:
        await self._client.close()

    async def test(self) -> dict:
        """测试连通性：发最小转录请求（静音 WAV）。5 秒超时。"""
        import asyncio
        import time
        t0 = time.perf_counter()
        try:
            silence = _make_silence_wav()
            await asyncio.wait_for(
                self._client.audio.transcriptions.create(
                    model=self._model,
                    file=("silence.wav", BytesIO(silence), "audio/wav"),
                ),
                timeout=5.0,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": True, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}
