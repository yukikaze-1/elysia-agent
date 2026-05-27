"""SenseVoice 本地 STT 后端 — HTTP API 调用。

POST /predict/sentence，multipart 上传音频 bytes。
响应格式: {"result": {"text": "...", "raw_text": "<|zh|><|HAPPY|><|Speech|><|withitn|>...", "clean_text": "..."}}

raw_text 格式: <|语种|><|情绪|><|事件|><|文本规范|>识别文本
"""

import logging
import re
from typing import AsyncIterator

import httpx

from .protocol import STTBackend

# SenseVoice 情绪标签
_EMOTION_TAGS: dict[str, str] = {
    "HAPPY": "😊", "SAD": "😔", "ANGRY": "😡", "NEUTRAL": "",
    "FEARFUL": "😰", "DISGUSTED": "🤢", "SURPRISED": "😮",
    "EMO_UNKNOWN": "",
}
_EMOTION_RE = re.compile(r"<\|(HAPPY|SAD|ANGRY|NEUTRAL|FEARFUL|DISGUSTED|SURPRISED|EMO_UNKNOWN)\|>")

# SenseVoice 音频事件标签
_EVENT_TAGS: dict[str, str] = {
    "Speech": "", "BGM": "🎼", "Applause": "👏", "Laughter": "😀",
    "Cry": "😭", "Cough": "😷", "Sneeze": "🤧", "Breath": "",
    "Sing": "", "Speech_Noise": "", "GBG": "", "Event_UNK": "",
}
_EVENT_RE = re.compile(
    r"<\|(Speech|BGM|Applause|Laughter|Cry|Cough|Sneeze|Breath|Sing|Speech_Noise|GBG|Event_UNK)\|>"
)


def _parse_emotion(raw_text: str) -> str:
    """从 raw_text 提取情绪标签，返回 NEUTRAL 或具体情绪名。"""
    m = _EMOTION_RE.search(raw_text)
    if not m:
        return "NEUTRAL"
    tag = m.group(1)
    return "NEUTRAL" if tag in ("NEUTRAL", "EMO_UNKNOWN") else tag


def _parse_events(raw_text: str) -> list[str]:
    """从 raw_text 提取所有音频事件标签（不包含 Speech）。"""
    events = _EVENT_RE.findall(raw_text)
    return [e for e in events if e != "Speech"]


class SenseVoiceBackend(STTBackend):
    """SenseVoice 语音识别后端。

    transcribe_stream 当前 yield 完整结果（SenseVoice 是整句识别，非真正流式）。
    每次 transcribe 调用后，last_emotion / last_events 记录最近一次识别的情绪和事件。
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
        self.last_emotion: str = "NEUTRAL"
        self.last_events: list[str] = []

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

        if data.get("result"):
            result = data["result"]
            # 解析 raw_text 中的情绪和事件标签
            raw = result.get("raw_text", "")
            if raw:
                self.last_emotion = _parse_emotion(raw)
                self.last_events = _parse_events(raw)
            if result.get("text"):
                # <|nospeech|> 表示 SenseVoice 判定为非语音，丢弃（防止噪音误识别）
                if "<|nospeech|>" in raw:
                    self._logger.debug("SenseVoice returned nospeech, discarding text=%r", result["text"])
                    return
                yield result["text"]
            else:
                self._logger.warning(
                    "SenseVoice returned result without text: %s",
                    str(data)[:200],
                )
        else:
            self._logger.warning(
                "SenseVoice returned unexpected response shape: %s",
                str(data)[:200],
            )

    async def close(self) -> None:
        await self._client.aclose()
