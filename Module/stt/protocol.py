"""STT 后端抽象接口。所有语音识别后端必须实现此协议。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any


class STTBackend(ABC):
    """语音识别后端抽象基类。

    audio 为原始音频 bytes（WAV 等格式，由后端决定支持哪些）。
    kwargs 透传 language / task 等参数。

    可选属性（由 transcribe() 调用后设置）：
        last_emotion: str — 最近一次转录检测到的情绪标签。
            SenseVoice 提供真实值（HAPPY/SAD/ANGRY/NEUTRAL 等），
            OpenAI 兼容后端设为 "NEUTRAL"。
        last_events: list[str] — 最近一次转录检测到的音频事件。
            SenseVoice 提供真实值（Applause/Laughter/Cough 等），
            OpenAI 兼容后端设为空列表。
    """

    @abstractmethod
    async def transcribe(self, audio: bytes, **kwargs: Any) -> str:
        """非流式识别 — 返回完整转写文本。

        Args:
            audio: 原始音频 bytes（WAV 等格式，由后端决定支持哪些）。
            **kwargs: 透传 language / task 等参数。

        Returns:
            完整转写文本。
        """
        ...

    @abstractmethod
    async def transcribe_stream(self, audio: bytes, **kwargs: Any) -> AsyncGenerator[str, None]:
        """流式识别 — 逐句/逐词产出识别结果。

        后端不支持流式时可 yield 完整结果作为单个 chunk。

        Args:
            audio: 原始音频 bytes（WAV 等格式，由后端决定支持哪些）。
            **kwargs: 透传 language / task 等参数。

        Yields:
            逐句/逐词的识别结果字符串。
        """
        ...
        yield ""  # type: ignore[misc]  # pragma: no cover — 让抽象方法成为生成器

    @abstractmethod
    async def test(self) -> dict:
        """测试连通性。

        Returns:
            {"ok": bool, "latency_ms": int, "error": str} 格式的连通性检测结果。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭 HTTP client。"""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """返回 provider 标识：'sensevoice' / 'custom'。"""
        ...

    @property
    def model(self) -> str:
        """返回完整模型标识 ``provider/model``（如 ``sensevoice/sense-voice``）。

        各后端把模型名存在 ``self._model``（如 ``sense-voice``），
        这里拼上 ``provider_name`` 得到前端下拉选项一致的完整 id。
        用于 capabilities 下发当前 STT 模型。
        """
        name = getattr(self, "_model", "")
        if not name:
            return ""
        return f"{self.provider_name}/{name}"
