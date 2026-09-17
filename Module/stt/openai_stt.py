"""OpenAI 兼容 STT 后端 — 用于 custom provider（Whisper API 格式）。"""

import logging
from io import BytesIO
from typing import Any, AsyncIterator, Callable

import httpx
from openai import AsyncOpenAI

from Module.stt.protocol import STTBackend
from Module._common.test_helpers import make_test_method


class OpenAICompatSTTBackend(STTBackend):
    """OpenAI 兼容语音识别后端 (Whisper API 格式)。"""

    def __init__(
        self,
        model: str = "whisper-1",
        *,
        api_key: str = "",
        base_url: str = "",
        logger: logging.Logger | None = None,
        http_client: httpx.AsyncClient | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        """初始化 OpenAI 兼容 STT 后端。

        Args:
            model: 模型名称。
            api_key: API 密钥，不传则使用默认值 "stt"。
            base_url: 服务地址，为空则使用 OpenAI 默认地址。
            logger: 日志记录器，不传则使用模块默认 logger。
            http_client: 出站守卫挂载点（批次 5）——注入的 httpx.AsyncClient 实例。
            http_client_factory: 出站守卫工厂（批次 5 review 修正）——零参可调用，
                每次调用返回全新守卫 client（本后端无 reconnect，构造时调用一次）。
        """
        self._model = model
        self._logger = logger or logging.getLogger(__name__)
        self._http_client_factory = http_client_factory
        self._injected_client: httpx.AsyncClient | None = http_client
        if http_client_factory is not None and http_client is None:
            self._injected_client = http_client_factory()
        client_kwargs: dict = {
            "api_key": api_key or "stt",
            "base_url": base_url,
            "timeout": httpx.Timeout(30.0, connect=5.0),
        }
        if self._injected_client is not None:
            client_kwargs["http_client"] = self._injected_client
        self._client = AsyncOpenAI(**client_kwargs)
        self.last_emotion: str = "NEUTRAL"
        self.last_events: list[str] = []

        async def _health_check() -> bool:
            import asyncio
            silence_wav = (
                b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
                b"\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00"
                b"\x02\x00\x10\x00data\x00\x00\x00\x00"
            )
            await asyncio.wait_for(
                self._client.audio.transcriptions.create(
                    model=self._model,
                    file=("silence.wav", silence_wav, "audio/wav"),
                ),
                timeout=5.0,
            )
            return True

        self._test_fn = make_test_method(_health_check)

    @property
    def provider_name(self) -> str:
        """provider 标识。"""
        return "custom"

    async def test(self) -> dict:
        """测试连通性：发最小转录请求（静音 WAV）。5 秒超时。"""
        return await self._test_fn(self)

    async def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """非流式识别 — 返回完整转写文本。

        Args:
            audio: 原始音频 bytes。
            **kwargs: 透传 language 等参数。

        Returns:
            完整转写文本。调用后 last_emotion 设为 "NEUTRAL"，last_events 设为空列表。
        """
        result = await self._client.audio.transcriptions.create(
            model=self._model,
            file=("audio.wav", BytesIO(audio), "audio/wav"),
            **kwargs,
        )
        self.last_emotion = "NEUTRAL"
        self.last_events = []
        return result.text

    async def transcribe_stream(self, audio: bytes, **kwargs: Any) -> AsyncIterator[str]:
        """流式识别 — OpenAI 兼容后端不支持真正流式，yield 完整结果作为单个 chunk。

        Args:
            audio: 原始音频 bytes。
            **kwargs: 透传 language 等参数。

        Yields:
            完整转写文本。
        """
        text = await self.transcribe(audio, **kwargs)
        self.last_emotion = "NEUTRAL"
        self.last_events = []
        yield text

    async def close(self) -> None:
        await self._client.close()

