"""OpenAI 兼容 TTS 后端 — 用于 custom provider（OpenAI TTS API 格式）。

POST /v1/audio/speech → 返回音频 bytes。
"""

import logging
from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

from Module.tts.protocol import TTSBackend
from Module.tts.text_preprocess import strip_all_markup
from Module._common.test_helpers import make_test_method

# OpenAI TTS 官方预置 voice（tts-1 / tts-1-hd 共用）——静态声音列表源。
_OPENAI_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]


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
        http_client: httpx.AsyncClient | None = None,
    ):
        """初始化 OpenAI 兼容 TTS 后端。

        Args:
            model: 模型名称（如 tts-1, tts-1-hd）。
            api_key: API 密钥，不传则使用默认值 "tts"。
            base_url: 服务地址，为空则使用 OpenAI 默认地址。
            logger: 日志记录器，不传则使用模块默认 logger。
            http_client: 出站守卫挂载点（批次 5）——注入的 httpx.AsyncClient。
                非 None 时 AsyncOpenAI 复用该 client，且 health check 直连路径
                也复用注入 client（供 Task 6/7 传 build_guarded_http_client 守卫）。
        """
        self._model = model
        self._base_url = base_url
        self._logger = logger or logging.getLogger(__name__)
        self._preprocess = strip_all_markup
        self._injected_client: httpx.AsyncClient | None = http_client
        client_kwargs: dict = {
            "api_key": api_key or "tts",
            "base_url": base_url,
            "timeout": httpx.Timeout(60.0, connect=5.0),
        }
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        self._client = AsyncOpenAI(**client_kwargs)

        async def _health_check() -> bool:
            # 注入守卫 client 时复用（直连路径也走守卫）；否则自建临时 client
            if self._injected_client is not None:
                resp = await self._injected_client.get(
                    f"{self._base_url.rstrip('/')}/health"
                )
                return resp.status_code < 400
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=httpx.Timeout(5.0)
            ) as client:
                resp = await client.get("/health")
                return resp.status_code < 400

        self._test_fn = make_test_method(_health_check)

    async def test(self) -> dict:
        """测试连通性：GET /health。5 秒超时。"""
        return await self._test_fn(self)

    async def list_voices(self) -> list[dict]:
        """返回静态声音列表：OpenAI 官方预置 voice 集。

        Returns:
            ``[{id, name}]`` — alloy/echo/fable/onyx/nova/shimmer（id 唯一）。
        """
        return [{"id": v, "name": v} for v in _OPENAI_VOICES]

    async def synthesize(
        self, text: str, *, character: str, emotion: str | None = None,
        voice_id: str | None = None,
    ) -> bytes:
        """非流式合成 — 返回完整 WAV bytes。

        Args:
            text: 要合成的文本。
            character: 发音人名称，映射为 OpenAI TTS API 的 voice 字段。
            emotion: 可选的情绪参数（OpenAI TTS 不支持，传了会被忽略）。
            voice_id: 显式音色 ID（批次 5 BE-051）。非空时流进 synthesize_stream
                的 voice 参数（voice=voice_id or character）；None 走 character。

        Returns:
            完整的 WAV 格式音频字节数据。采样率 24000Hz。
        """
        text = self._preprocess(text)
        chunks: list[bytes] = []
        async for chunk in self.synthesize_stream(
            text, character=character, emotion=emotion, voice_id=voice_id,
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    async def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """流式合成 — OpenAI TTS 非真正流式，一次返回完整音频。

        Args:
            text: 要合成的文本。
            character: 发音人名称，映射为 OpenAI TTS API 的 voice 字段。
            emotion: 可选的情绪参数（OpenAI TTS 不支持，传了会被忽略）。
            voice_id: 显式音色 ID（批次 5 BE-051）。非空 → voice=voice_id；
                None → voice=character（现状）。

        Yields:
            音频字节数据 chunk（4096 字节每块）。
        """
        text = self._preprocess(text)
        async with self._client.audio.speech.with_streaming_response.create(
            model=self._model,
            voice=voice_id or character,
            input=text,
            response_format="wav",
        ) as response:
            async for chunk in response.iter_bytes(4096):
                yield chunk

    @property
    def provider_name(self) -> str:
        return "custom"

    def get_preprocess(self):
        """返回本后端使用的预处理函数。

        Returns:
            strip_all_markup 预处理函数。
        """
        return self._preprocess

    async def close(self) -> None:
        await self._client.close()

