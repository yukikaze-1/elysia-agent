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

        Args:
            text: 要合成的文本。
            character: 发音人/音色名称，对应后端配置中的角色标识。
            emotion: 可选的情绪参数，部分后端不支持（传了会被忽略）。

        Returns:
            完整的 WAV 格式音频字节数据。
        """
        ...

    @property
    def stream_format(self) -> str:
        """流式输出的音频格式（MIME 子类型），默认 'wav'。"""
        return "wav"

    @abstractmethod
    def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        """流式合成 — 边生成边产出音频 chunk。首 chunk 包含 WAV header 及初始音频数据。

        Args:
            text: 要合成的文本。
            character: 发音人/音色名称，对应后端配置中的角色标识。
            emotion: 可选的情绪参数，部分后端不支持（传了会被忽略）。

        Yields:
            音频字节数据 chunk。
        """
        ...

    @abstractmethod
    async def test(self) -> dict:
        """测试连通性。

        Returns:
            {"ok": bool, "latency_ms": int, "error": str} 格式的连通性检测结果。
        """
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """返回 provider 标识：'gpt-sovits' / 'minimax' / 'custom'。"""
        ...

    @property
    def model(self) -> str:
        """返回完整模型标识 ``provider/model``（如 ``minimax/speech-2.8-hd``）。

        各后端把 model name 存在 ``self._model``（如 ``speech-2.8-hd``），
        这里拼上 ``provider_name`` 得到前端下拉选项一致的完整 id。
        用于 capabilities 下发当前 TTS 模型。
        """
        name = getattr(self, "_model", "")
        if not name:
            return ""
        return f"{self.provider_name}/{name}"

    def get_preprocess(self):
        """返回本后端使用的预处理函数，默认 None（不预处理）。

        Returns:
            预处理函数 callable，或 None。"""
        return None

    @abstractmethod
    async def close(self) -> None:
        """关闭 HTTP client。"""
        ...
