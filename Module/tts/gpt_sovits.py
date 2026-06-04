"""GPT-SoVits TTS 后端 — HTTP API 调用。

每次 TTS 请求创建独立的 httpx.AsyncClient，用完即关。
避免连接池复用导致的服务端空闲断开问题。
流式通过 asyncio.Queue + create_task 桥接。
"""

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

import httpx
import yaml

from .protocol import TTSBackend
from .text_preprocess import strip_all_markup

# 32000 Hz * 1 channel * 2 bytes/sample
BYTES_PER_SECOND = 64000

_TTS_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


class GPTSoVitsBackend(TTSBackend):
    """GPT-SoVits 语音合成后端。

    emotion 参数当前被忽略（GPT-SoVits 不支持情绪控制），
    保留用于接口一致和后续后端（如 IndexTTS2）。
    """

    def __init__(
        self,
        base_url: str = "http://localhost:9880",
        voices_config: str = "",
        *,
        model: str = "default",
        logger: logging.Logger | None = None,
    ):
        self._logger = logger or logging.getLogger(__name__)
        self._base_url = base_url
        self._model = model
        if voices_config:
            path = Path(voices_config)
        else:
            path = Path(__file__).parent / "gpt_sovits_voices.yaml"
        self._voices = yaml.safe_load(path.read_text(encoding="utf-8"))
        self._preprocess = strip_all_markup

    async def synthesize(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> bytes:
        chunks: list[bytes] = []
        async for chunk in self._call_api(text, character, emotion, streaming=False):
            chunks.append(chunk)
        return b"".join(chunks)

    async def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> AsyncIterator[bytes]:
        async for chunk in self._call_api(text, character, emotion, streaming=True):
            yield chunk

    async def _call_api(
        self, text: str, character: str, emotion: str | None, streaming: bool,
    ) -> AsyncIterator[bytes]:
        """统一的 HTTP 调用入口。

        每次请求创建独立的 httpx.AsyncClient，用完即关。
        避免连接池复用时服务端空闲断开导致后续请求失败。
        """
        try:
            voice_cfg = self._voices[character]
        except KeyError:
            raise KeyError(
                f"未知发音人 '{character}'，可用: {list(self._voices.keys())}"
            ) from None
        text = self._preprocess(text)

        payload: dict = {
            "text": text,
            "text_lang": "zh",
            "ref_audio_path": voice_cfg["ref_audio"],
            "prompt_lang": "zh",
            "prompt_text": voice_cfg["prompt_text"],
            "top_k": 5,
            "top_p": 1.0,
            "temperature": 1.0,
            "text_split_method": "cut5",
            "batch_size": 1,
            "batch_threshold": 0.75,
            "speed_factor": 1.0,
            "split_bucket": True,
            "fragment_interval": 0.3,
            "seed": -1,
            "media_type": "wav",
            "streaming_mode": streaming,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
        }

        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def _pull() -> None:
            """httpx 流式消费者 — 运行在独立 task 中。"""
            client = httpx.AsyncClient(base_url=self._base_url, timeout=_TTS_TIMEOUT)
            try:
                async with client.stream("POST", "/tts", json=payload) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(4096):
                        await queue.put(chunk)
            except httpx.RequestError as e:
                self._logger.error(
                    "GPT-SoVits connection failed (base_url=%s): %s",
                    self._base_url, e,
                )
                raise RuntimeError(
                    f"无法连接 GPT-SoVits 服务 ({self._base_url}): {e}"
                ) from e
            finally:
                await client.aclose()
                await queue.put(None)  # 哨兵：流结束

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
                logging.getLogger("Module.tts.gpt_sovits").warning(
                    "TTS _pull task failed: %s", e, exc_info=True
                )

    async def test(self) -> dict:
        """测试连通性：GET 根路径检查服务可用。5 秒超时。"""
        import asyncio as _asyncio
        import time as _time
        t0 = _time.perf_counter()
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=httpx.Timeout(5.0)) as client:
                resp = await _asyncio.wait_for(client.get("/"), timeout=5.0)
                latency_ms = int((_time.perf_counter() - t0) * 1000)
                return {"ok": resp.status_code < 400, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((_time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}

    async def close(self) -> None:
        pass  # 无共享 client，无需清理
