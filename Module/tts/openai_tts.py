"""OpenAI 兼容 TTS 后端 — 用于 custom provider（OpenAI TTS API 格式）。

POST /v1/audio/speech → 返回音频 bytes。
"""

import logging
from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

from .protocol import TTSBackend


class OpenAICompatTTSBackend(TTSBackend):
    """OpenAI 兼容 TTS 后端 (tts-1 / tts-1-hd 等)。

    character 参数映射为 OpenAI TTS API 的 voice 字段。
    emotion 参数被忽略（OpenAI TTS 不支持情绪控制）。
    """

    def __init__(
        self,
        model: str = "tts-1",
        *,
        api_key: str = "",
        base_url: str = "",
        logger: logging.Logger | None = None,
    ):
        self._model = model
        self._logger = logger or logging.getLogger(__name__)
        self._client = AsyncOpenAI(
            api_key=api_key or "tts",
            base_url=base_url,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )

    async def synthesize(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> bytes:
        chunks: list[bytes] = []
        async for chunk in self.synthesize_stream(text, character=character, emotion=emotion):
            chunks.append(chunk)
        return b"".join(chunks)

    async def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> AsyncIterator[bytes]:
        """流式合成 — OpenAI TTS 非真正流式，一次返回完整音频。"""
        async with self._client.audio.speech.with_streaming_response.create(
            model=self._model,
            voice=character,
            input=text,
            response_format="wav",
        ) as response:
            async for chunk in response.iter_bytes(4096):
                yield chunk

    async def close(self) -> None:
        await self._client.close()

    async def test(self) -> dict:
        """测试连通性：发最小合成请求。5 秒超时。"""
        import asyncio
        import time
        t0 = time.perf_counter()
        try:
            async with self._client.audio.speech.with_streaming_response.create(
                model=self._model,
                voice="alloy",
                input="test",
                response_format="wav",
            ) as response:
                # 只读第一个 chunk 验证 API 可达
                await asyncio.wait_for(
                    response.iter_bytes(4096).__anext__(),
                    timeout=5.0,
                )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": True, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}
