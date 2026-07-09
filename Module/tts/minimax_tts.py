"""MiniMax TTS 后端 — HTTP 非流式合成 + 分块流式。

共享 httpx.AsyncClient（Bearer Token 认证），hex 编码音频解码。
synthesize_stream() 内部使用非流式 wav（MiniMax 流式仅 mp3，前端期望 wav），
获取完整音频后分块 yield。
"""

import logging
from pathlib import Path
from typing import AsyncIterator

import httpx
import yaml

from .protocol import TTSBackend
from .text_preprocess import strip_for_minimax
from Module._common.test_helpers import make_test_method

_TTS_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


def _load_voice_map(voices_config: str = "") -> dict[str, str]:
    """加载 MiniMax 音色 YAML，返回 {character_name: voice_id}。"""
    if voices_config:
        path = Path(voices_config)
    else:
        path = Path(__file__).parent / "minimax_voices.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {k: v["voice_id"] for k, v in raw.items()}


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
        voices_config: str = "",
        voice_id: str = "elysia_test_001",
        voice_map: dict[str, str] | None = None,
        speed: int = 1,           # MiniMax API 仅接受整数值
        vol: int = 1,
        pitch: int = 0,
        sample_rate: int = 32000,
        audio_format: str = "wav",
        logger: logging.Logger | None = None,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._voice_id = voice_id
        # 加载 voice_map：YAML 文件 → 显式 dict 覆盖
        self._voice_map = _load_voice_map(voices_config)
        if voice_map:
            self._voice_map.update(voice_map)
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

        async def _health_check() -> bool:
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
        try:
            resp = await self._client.post("/v1/t2a_v2", json=payload)
            resp.raise_for_status()
            data = resp.json()
            self._check_base_resp(data)
            audio_hex = (data.get("data") or {}).get("audio", "")
            return bytes.fromhex(audio_hex)
        except httpx.RequestError as e:
            self._logger.debug(
                "MiniMax 连接失败 (base_url=%s): %s", self._base_url, e
            )
            raise RuntimeError(
                f"无法连接 MiniMax 服务 ({self._base_url}): {e}"
            ) from e

    async def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> AsyncIterator[bytes]:
        """流式合成 — 委托给 synthesize()，下载完整 wav 后分块 yield。

        MiniMax 流式 API 仅输出 mp3，前端期望 wav，故先下载完整音频再分块。
        注意：并非真正的流式——首个 chunk 延迟与 synthesize() 相同。
        """
        # 复用 synthesize() 的非流式 wav 逻辑
        audio = await self.synthesize(text, character=character, emotion=emotion)
        # 按 4096 字节分块 yield，与其他 backend 行为一致
        chunk_size = 4096
        for offset in range(0, len(audio), chunk_size):
            yield audio[offset:offset + chunk_size]

    @property
    def provider_name(self) -> str:
        return "minimax"

    @property
    def stream_format(self) -> str:
        return "wav"

    def get_preprocess(self):
        return self._preprocess

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
        if not voice_id:
            raise ValueError(
                "MiniMax TTS 需要 voice_id。请通过 voice_map 或 voice_id 参数指定音色。"
            )
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
                "bitrate": 128000,
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
        base_resp = data.get("base_resp")
        if base_resp is None:
            raise RuntimeError("MiniMax API 响应缺少 base_resp 字段")
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(
                f"MiniMax API 错误: status_code={base_resp.get('status_code')}, "
                f"status_msg={base_resp.get('status_msg', 'unknown')}"
            )
