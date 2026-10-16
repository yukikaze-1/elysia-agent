"""标准合法 TTS 测试替身 —— 让「假后端漏实现协议成员」在结构上难以发生。

## 为什么要有这个类

`Module/tts/protocol.py` 的 `TTSBackend` 有 10 个公开成员（5 抽象 + 5 带默认实现），
但测试替身**大多不继承它**（duck-typed 普通类）。替身因此可以安静地少实现几个，
而唯一的发现途径是"生产路径恰好在某条测试里访问到了那个成员"——2026-09-27 的
`server_info` 整帧消失就是这么来的（假替身没写 `model`，而 `build_server_info` 裸读它）。

2026-09-28 全仓审计（45 个具名替身 + 23 处动态替身）的实际代价：

- **33 个**至少缺一个**抽象**成员——若它们继承 ABC，会在实例化时直接 `TypeError`；
- **16 个**缺 `model`；
- 后果常常被**吞掉**而不是报错：`core/input/tests/test_v2_ws.py::_FakeTtsBackend`
  没有 `close()`，而 `Agent.stop()` 会 `await self._tts.close()`——失败被 shutdown
  步骤的 `except BaseException` 收走，只在日志里留一行。

`FakeTTSBackend` 继承 `TTSBackend` 并实现**全部**成员：子类只 override 自己关心的
那几个，其余拿本类的确定性默认值。于是"漏字段"不再是"少写一行"，而是要么继承到
默认值、要么在实例化时 `TypeError`。

## 命名为什么不是 `TestTTSBackend`

`pytest.ini` 没有覆盖 `python_classes`（默认 `Test*`）。被 import 进测试模块的
`Test*` 类会被 pytest 当成测试类去收集，报 `PytestCollectionWarning`（还带
`__init__` 构造器告警）。`FakeTTSBackend` 与本仓既有的 `FakeLLMBackend`
（`core/tests/fake_llm.py`）/ `FakeDocumentDBBackend` 同名法一致。

## 它不做的事

不模拟真实后端的**语义**（音频格式、限速、错误分类）——那是各后端自己的测试与
`Module/tts/tests/test_tts_contract.py`（针对**生产**后端）的职责。这里只保证
"协议面齐备 + 零 I/O + 可断言"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence

from Module.tts.protocol import TTSBackend

__all__ = ["FakeTTSBackend"]


class FakeTTSBackend(TTSBackend):
    """全协议面、零 I/O 的 TTS 替身（默认返回固定音频，不触网）。

    Args:
        provider_name: 协议成员 ``provider_name`` 的返回值。
        model: 模型名——协议成员 ``model`` 由 ``provider_name`` 与它拼成
            ``provider/model``（继承 `TTSBackend.model` 的默认实现，子类不必重写）。
        voices: ``list_voices()`` 返回的条目（会被浅拷贝后返回）。
        audio: ``synthesize()`` 返回的字节。
        stream_chunks: ``synthesize_stream()`` 依次 yield 的字节；None 表示
            yield 单个 ``audio``。
        stream_format: 协议成员 ``stream_format`` 的返回值。
        default_voice_id: ``default_voice()`` 的固定返回；None 表示沿用协议默认
            语义（返回传入的 ``character``）。
        preprocess: ``get_preprocess()`` 的返回值；None 表示不预处理。
        test_result: ``test()`` 返回的字典；None 表示 ``{"ok": True, ...}``。

    Attributes:
        calls: 每次 ``synthesize``/``synthesize_stream`` 的
            ``(text, character, emotion, voice_id)``。注意异步生成器的体在**首次
            迭代**时才执行，故 ``synthesize_stream`` 的记录要求调用方真的消费它。
        close_calls: ``close()`` 被调用的次数（供"生命周期是否收尾"断言）。
    """

    def __init__(
        self,
        *,
        provider_name: str = "fake",
        model: str = "fake-tts",
        voices: Sequence[dict] | None = None,
        audio: bytes = b"",
        stream_chunks: Sequence[bytes] | None = None,
        stream_format: str = "wav",
        default_voice_id: str | None = None,
        preprocess: Callable | None = None,
        test_result: dict | None = None,
    ) -> None:
        self._provider_name = provider_name
        self._model = model
        self._voices = list(voices or [])
        self._audio = audio
        self._stream_chunks = [audio] if stream_chunks is None else list(stream_chunks)
        self._stream_format = stream_format
        self._default_voice_id = default_voice_id
        self._preprocess = preprocess
        self._test_result = test_result or {"ok": True, "latency_ms": 0, "error": ""}
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.close_calls = 0

    # ── 协议：provider_name / stream_format / default_voice / get_preprocess ──

    @property
    def provider_name(self) -> str:
        """provider 标识。"""
        return self._provider_name

    @property
    def stream_format(self) -> str:
        """流式输出格式。"""
        return self._stream_format

    def default_voice(self, character: str = "") -> str:
        """默认音色：显式配置优先，否则沿用协议默认语义（character 即音色键）。"""
        return self._default_voice_id if self._default_voice_id is not None else character

    def get_preprocess(self):
        """预处理函数，默认 None（不预处理）。"""
        return self._preprocess

    # ── 协议：list_voices ─────────────────────────────────────────────────────

    async def list_voices(self) -> list[dict]:
        """返回构造时给定的声音列表（拷贝，避免调用方改到内部状态）。"""
        return [dict(v) for v in self._voices]

    # ── 协议：synthesize / synthesize_stream ──────────────────────────────────

    async def synthesize(
        self, text: str, *, character: str, emotion: str | None = None,
        voice_id: str | None = None,
    ) -> bytes:
        """记录调用并返回固定音频（无 I/O）。"""
        self.calls.append((text, character, emotion, voice_id))
        return self._audio

    async def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None,
        voice_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """记录调用并依次 yield 构造时给定的 chunk（无 I/O）。"""
        self.calls.append((text, character, emotion, voice_id))
        for chunk in self._stream_chunks:
            yield chunk

    # ── 协议：test / close ────────────────────────────────────────────────────

    async def test(self) -> dict:
        """返回构造时给定的连通性结果（拷贝）。"""
        return dict(self._test_result)

    async def close(self) -> None:
        """标记已关闭（计数，供断言）。"""
        self.close_calls += 1
