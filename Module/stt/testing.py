"""标准合法 STT 测试替身 —— 让「假后端漏实现协议成员」在结构上难以发生。

与 `Module/tts/testing.py::FakeTTSBackend` 同因同形：`STTBackend` 的 6 个公开成员
（5 抽象 + 1 带默认实现的 `model`）在 duck-typed 替身里可以安静地少实现，而
`make pyright` 排除 `**/tests/**` 且不检查覆写形态——见
`Module/_common/protocol_contract.py` 的模块 docstring。

除协议成员外，本类还**主动**维护 `STTBackend` docstring 里声明的两个可选属性
`last_emotion` / `last_events`（SenseVoice 给真实值，OpenAI 兼容后端给
`"NEUTRAL"` / `[]`）。它们不在 ABC 的成员表里（协议未把它们升格为抽象成员），
但消费方（`core/input`）会在 transcribe 之后直接读——替身构造时就初始化，
调用方不必先猜"有没有被赋过值"。

命名不用 `Test*` 前缀的原因见 `Module/tts/testing.py` 的同名说明。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from Module.stt.protocol import STTBackend

__all__ = ["FakeSTTBackend"]


class FakeSTTBackend(STTBackend):
    """全协议面、零 I/O 的 STT 替身（默认返回固定文本，不触网）。

    Args:
        provider_name: 协议成员 ``provider_name`` 的返回值。
        model: 模型名——协议成员 ``model`` 由 ``provider_name`` 与它拼成
            ``provider/model``（继承 `STTBackend.model` 的默认实现）。
        text: ``transcribe()`` 返回的文本。
        stream_chunks: ``transcribe_stream()`` 依次 yield 的文本；None 表示
            yield 单个 ``text``。
        emotion: 转录后写入 ``last_emotion`` 的值。
        events: 转录后写入 ``last_events`` 的值。
        test_result: ``test()`` 返回的字典；None 表示 ``{"ok": True, ...}``。

    Attributes:
        calls: 每次 ``transcribe``/``transcribe_stream`` 收到的 ``(audio, kwargs)``。
            异步生成器的体在**首次迭代**时才执行，故 ``transcribe_stream`` 的记录
            要求调用方真的消费它。
        close_calls: ``close()`` 被调用的次数。
        last_emotion: 最近一次转录的情绪标签（构造时即存在，避免消费方
            在首次 transcribe 前读到 `AttributeError`）。
        last_events: 最近一次转录的音频事件列表。
    """

    def __init__(
        self,
        *,
        provider_name: str = "fake",
        model: str = "fake-stt",
        text: str = "测试转写",
        stream_chunks: Sequence[str] | None = None,
        emotion: str = "NEUTRAL",
        events: Sequence[str] | None = None,
        test_result: dict | None = None,
    ) -> None:
        self._provider_name = provider_name
        self._model = model
        self._text = text
        self._stream_chunks = [text] if stream_chunks is None else list(stream_chunks)
        self._test_result = test_result or {"ok": True, "latency_ms": 0, "error": ""}
        self.calls: list[tuple[bytes, dict]] = []
        self.close_calls = 0
        self.last_emotion: str = emotion
        self.last_events: list[str] = list(events or [])

    # ── 协议：provider_name ───────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        """provider 标识。"""
        return self._provider_name

    # ── 协议：transcribe / transcribe_stream ──────────────────────────────────

    async def transcribe(self, audio: bytes, **kwargs: object) -> str:
        """记录调用、写入可选属性，返回固定文本（无 I/O）。"""
        self.calls.append((audio, dict(kwargs)))
        return self._text

    async def transcribe_stream(self, audio: bytes, **kwargs: object) -> AsyncIterator[str]:
        """记录调用、写入可选属性，依次 yield 构造时给定的 chunk（无 I/O）。"""
        self.calls.append((audio, dict(kwargs)))
        for chunk in self._stream_chunks:
            yield chunk

    # ── 协议：test / close ────────────────────────────────────────────────────

    async def test(self) -> dict:
        """返回构造时给定的连通性结果（拷贝）。"""
        return dict(self._test_result)

    async def close(self) -> None:
        """标记已关闭（计数，供断言）。"""
        self.close_calls += 1
