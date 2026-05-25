"""SenseVoice 本地 STT 后端 — HTTP API 调用。

POST /predict/sentence，multipart 上传音频 bytes。
响应格式: {"result": {"text": "识别文本"}}
"""

import logging
from typing import AsyncIterator

import httpx

from .protocol import STTBackend


class SenseVoiceBackend(STTBackend):
    """SenseVoice 语音识别后端。

    transcribe_stream 当前 yield 完整结果（SenseVoice 是整句识别，非真正流式）。
    """

    def __init__(
        self,
        model: str = "sense-voice",
        *,
        api_key: str = "",
        base_url: str = "http://localhost:20042",
        logger: logging.Logger | None = None,
    ):
        self._model = model
        self._logger = logger or logging.getLogger(__name__)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )

    async def transcribe(self, audio: bytes, **kwargs) -> str:
        chunks: list[str] = []
        async for chunk in self._call_api(audio, **kwargs):
            chunks.append(chunk)
        return "".join(chunks)

    async def transcribe_stream(self, audio: bytes, **kwargs) -> AsyncIterator[str]:
        async for chunk in self._call_api(audio, **kwargs):
            yield chunk

    async def _call_api(self, audio: bytes, **kwargs) -> AsyncIterator[str]:
        try:
            response = await self._client.post(
                "/predict/sentence",
                files={"file": audio},
            )
        except httpx.RequestError as e:
            self._logger.error(
                "SenseVoice connection failed (base_url=%s): %s",
                self._client.base_url, e,
            )
            raise RuntimeError(
                f"无法连接 SenseVoice 服务 ({self._client.base_url}): {e}"
            ) from e
        response.raise_for_status()
        data = response.json()

        if data.get("result") and data["result"].get("text"):
            yield data["result"]["text"]

    async def close(self) -> None:
        await self._client.aclose()
