"""MiniMax TTS 后端 — HTTP + SSE 流式合成。

共享 httpx.AsyncClient（Bearer Token 认证），hex 编码音频解码。
SSE 流式通过 asyncio.Queue + create_task 桥接。
"""

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from .protocol import TTSBackend
from .text_preprocess import strip_for_minimax
from Module._common.test_helpers import make_test_method

_TTS_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


class MiniMaxTTSBackend(TTSBackend):
    """MiniMax 语音合成后端。

    character 通过 voice_map 映射为 voice_setting.voice_id，
    未命中时 fallback 到构造参数的 voice_id。
    emotion 直接映射为 voice_setting.emotion。
    """

    def __init__(
        self,
        model: str = "speech-2.8-hd",
        *,
        api_key: str,
        base_url: str = "https://api.minimaxi.com",
        voice_id: str = "",
        voice_map: dict[str, str] | None = None,
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: float = 0.0,
        sample_rate: int = 32000,
        audio_format: str = "wav",
        logger: logging.Logger | None = None,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._voice_id = voice_id
        self._voice_map = voice_map or {}
        self._speed = speed
        self._vol = vol
        self._pitch = pitch
        self._sample_rate = sample_rate
        self._audio_format = audio_format
        self._logger = logger or logging.getLogger(__name__)
        self._preprocess = strip_for_minimax

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_TTS_TIMEOUT,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

        async def _health_check(self) -> bool:
            resp = await self._client.post(
                "/v1/get_voice",
                json={"voice_type": "system"},
                timeout=httpx.Timeout(5.0),
            )
            if resp.status_code >= 400:
                return False
            return resp.json().get("base_resp", {}).get("status_code") == 0

        self._test_fn = make_test_method(_health_check)

    # ---- TTSBackend 协议 ----

    async def test(self) -> dict:
        return await self._test_fn(self)

    async def synthesize(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> bytes:
        text = self._preprocess(text)
        voice_id = self._resolve_voice(character)
        payload = self._build_payload(text, voice_id, emotion, stream=False)
        resp = await self._client.post("/v1/t2a_v2", json=payload)
        resp.raise_for_status()
        data = resp.json()
        self._check_base_resp(data)
        audio_hex = data.get("data", {}).get("audio", "")
        return bytes.fromhex(audio_hex)

    async def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> AsyncIterator[bytes]:
        text = self._preprocess(text)
        voice_id = self._resolve_voice(character)
        payload = self._build_payload(text, voice_id, emotion, stream=True)

        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def _pull() -> None:
            try:
                async with self._client.stream(
                    "POST", "/v1/t2a_v2", json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        self._logger.warning(
                            "MiniMax 返回 %d: %s (text=%r)",
                            resp.status_code,
                            body.decode("utf-8", errors="replace")[:500],
                            text,
                        )
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            raw = line[6:]
                            try:
                                event = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            self._check_base_resp(event)
                            audio_hex = event.get("data", {}).get("audio", "")
                            if audio_hex:
                                await queue.put(bytes.fromhex(audio_hex))
            except httpx.RequestError as e:
                self._logger.debug(
                    "MiniMax 连接失败 (base_url=%s): %s", self._base_url, e
                )
                raise RuntimeError(
                    f"无法连接 MiniMax 服务 ({self._base_url}): {e}"
                ) from e
            finally:
                await queue.put(None)

        task = asyncio.create_task(_pull())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.getLogger("Module.tts.minimax").debug(
                    "TTS _pull task failed: %s", e
                )

    async def close(self) -> None:
        await self._client.aclose()

    # ---- 内部方法 ----

    def _resolve_voice(self, character: str) -> str:
        """按 voice_map 查找 voice_id，未命中 fallback 到默认值。"""
        return self._voice_map.get(character, self._voice_id)

    def _build_payload(
        self, text: str, voice_id: str, emotion: str | None, stream: bool
    ) -> dict:
        """构建 MiniMax /v1/t2a_v2 请求体。"""
        payload: dict = {
            "model": self._model,
            "text": text,
            "stream": stream,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": self._speed,
                "vol": self._vol,
                "pitch": self._pitch,
            },
            "audio_setting": {
                "sample_rate": self._sample_rate,
                "format": "mp3" if stream else self._audio_format,
                "channel": 1,
            },
        }
        if emotion:
            payload["voice_setting"]["emotion"] = emotion
        return payload

    @staticmethod
    def _check_base_resp(data: dict) -> None:
        """检查 base_resp.status_code，非 0 抛 RuntimeError。"""
        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(
                f"MiniMax API 错误: status_code={base_resp.get('status_code')}, "
                f"status_msg={base_resp.get('status_msg', 'unknown')}"
            )
