"""STT 后端抽象接口。所有语音识别后端必须实现此协议。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator


class STTBackend(ABC):
    """语音识别后端抽象基类。

    audio 为原始音频 bytes（WAV 等格式，由后端决定支持哪些）。
    kwargs 透传 language / task 等参数。
    """

    @abstractmethod
    async def transcribe(self, audio: bytes, **kwargs) -> str:
        """非流式识别 — 返回完整转写文本。"""
        ...

    @abstractmethod
    async def transcribe_stream(self, audio: bytes, **kwargs) -> AsyncGenerator[str, None]:
        """流式识别 — 逐句/逐词产出识别结果。

        后端不支持流式时可 yield 完整结果作为单个 chunk。"""
        ...

    @abstractmethod
    async def test(self) -> dict:
        """测试连通性。返回 {"ok": bool, "latency_ms": int, "error": str}"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭 HTTP client。"""
        ...
