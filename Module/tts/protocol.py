"""TTS 后端抽象接口。所有语音合成后端必须实现此协议。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator


class TTSBackend(ABC):
    """语音合成后端抽象基类。

    character 必传，对应音色配置。emotion 可选，部分后端不支持（传了会被忽略）。
    """

    @abstractmethod
    async def synthesize(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> bytes:
        """非流式合成 — 返回完整 WAV bytes。

        采样率因后端而异：GPT-SoVits 为 32000Hz/mono/int16，OpenAI TTS 为 24000Hz。
        """
        ...

    @abstractmethod
    def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        """流式合成 — 边生成边产出音频 chunk。首 chunk 包含 WAV header 及初始音频数据。"""
        ...

    @abstractmethod
    async def test(self) -> dict:
        """测试连通性。返回 {"ok": bool, "latency_ms": int, "error": str}"""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """返回 provider 标识：'gpt-sovits' / 'minimax' / 'custom'。"""
        ...

    def get_preprocess(self):
        """返回本后端使用的预处理函数，默认 None（不预处理）。"""
        return None

    @abstractmethod
    async def close(self) -> None:
        """关闭 HTTP client。"""
        ...
