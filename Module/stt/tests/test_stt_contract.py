"""STT 后端契约测试 —— 「生产实现真的满足 `STTBackend` 吗」的机器判据。

与 `Module/tts/tests/test_tts_contract.py` 同因同形，细节说明见那边；这里只记
STT 侧的差异：

- `STTBackend` 的协议面只有 **6** 个成员（5 抽象 + `model`）——`list_voices` /
  `stream_format` / `default_voice` / `get_preprocess` 都是 TTS 专有；
- 两个真实实现：`SenseVoiceBackend`（本地服务，`sensevoice`）与
  `OpenAICompatSTTBackend`（`custom` 走 OpenAI 兼容协议；`openai` / `deepgram`
  是 providers.yaml 里声明但**本仓未实现**的占位 provider，由
  `STTFactory._UNIMPLEMENTED_PROVIDERS` 在 `create()` 里拒绝，故不在契约面内）；
- `last_emotion` / `last_events` 是协议 docstring 声明的**可选属性**，不在 ABC
  成员表里（本文件不把它们当契约成员断言），标准替身会主动维护它们。
"""

from __future__ import annotations

import inspect
import socket
from collections.abc import Callable

import pytest

from Module._common.protocol_contract import public_members, violations
from Module.stt.openai_stt import OpenAICompatSTTBackend
from Module.stt.protocol import STTBackend
from Module.stt.registry import STTFactory
from Module.stt.sensevoice import SenseVoiceBackend
from Module.stt.testing import FakeSTTBackend

pytestmark = pytest.mark.service

#: 不可达的本地端口（契约测试不连它；见构造不触网的判据）。
_UNREACHABLE = "http://127.0.0.1:1"

#: `STTBackend` 声明的全部公开成员（5 抽象 + 1 带默认实现）。
_PROTOCOL_SURFACE = frozenset({
    "transcribe", "transcribe_stream", "test", "provider_name", "close",  # 抽象
    "model",                                                              # 具体默认
})


def _sensevoice() -> STTBackend:
    return SenseVoiceBackend(model="sense-voice", base_url=_UNREACHABLE)


def _openai_compat() -> STTBackend:
    return OpenAICompatSTTBackend(
        model="whisper-1", api_key="contract-test-key", base_url=_UNREACHABLE,
    )


_BACKENDS: list[tuple[str, Callable[[], STTBackend], str]] = [
    ("sensevoice", _sensevoice, "sensevoice/sense-voice"),
    ("custom", _openai_compat, "custom/whisper-1"),
]

_PARAMS = [pytest.param(build, name, model_id, id=name)
           for name, build, model_id in _BACKENDS]


# ── 协议面 ────────────────────────────────────────────────────────────────────

def test_protocol_surface_matches_this_file() -> None:
    """协议面的成员集合与本文档钉住的清单一致（加成员必须同步这里）。"""
    assert public_members(STTBackend) == _PROTOCOL_SURFACE, (
        "`STTBackend` 的公开成员变了——三个消费方要一起看：生产后端、标准替身"
        "（Module/stt/testing.py::FakeSTTBackend 会自动继承到）、duck-typed 替身。"
    )


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_backend_satisfies_protocol(build, provider, model_id) -> None:
    """生产后端满足 `STTBackend` 的**每一个**公开成员（含形态）。"""
    assert violations(build(), STTBackend) == []


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_model_is_provider_qualified(build, provider, model_id) -> None:
    """`model` 是 ``provider/model`` 的完整标识——它会原样下发到前端。"""
    model = build().model
    assert isinstance(model, str) and model, f"{provider} 的 model 应为非空字符串，实为 {model!r}"
    assert model == model_id


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_provider_name_matches(build, provider, model_id) -> None:
    assert build().provider_name == provider


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_async_members_have_async_shape(build, provider, model_id) -> None:
    """异步成员是协程函数；`transcribe_stream` 是**真的**异步生成器。"""
    backend = build()
    for name in ("transcribe", "test", "close"):
        assert inspect.iscoroutinefunction(getattr(backend, name)), f"{name} 应为协程函数"
    assert inspect.isasyncgenfunction(backend.transcribe_stream), (
        "transcribe_stream 应为异步生成器（调用方 `async for` 它）"
    )


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_construction_does_not_touch_the_network(build, provider, model_id, monkeypatch) -> None:
    """构造后端不发起任何连接——契约测试不能悄悄变成 integration 测试。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"{provider} 后端在构造期发起了网络连接")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    build()


# ── 工厂路径（Agent 真正走的那条） ────────────────────────────────────────────

@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_factory_output_satisfies_protocol(build, provider, model_id, monkeypatch) -> None:
    """`STTFactory.create` 的返回值同样满足协议——它是 Agent 实际拿到的东西。"""
    monkeypatch.setenv("SENSEVOICE_URL", _UNREACHABLE)
    monkeypatch.setenv("CUSTOM_STT_API_KEY", "contract-test-key")

    backend = STTFactory.create(model_id, base_url=_UNREACHABLE)
    assert violations(backend, STTBackend) == []
    assert backend.model == model_id


def test_factory_refuses_unimplemented_providers() -> None:
    """占位 provider 在 `create()` 就被拒——它们**不**属于契约面。

    与 `STTFactory._UNIMPLEMENTED_PROVIDERS` 同源：这里钉住的是"没实现的东西
    不会被当成实现放出去"，避免契约测试将来误把占位 provider 也算进来。
    """
    for model_id in ("openai/whisper-1", "deepgram/nova-2"):
        assert STTFactory.is_implemented(model_id) is False
        with pytest.raises(NotImplementedError):
            STTFactory.create(model_id)


# ── 测试替身（§7：注入生产路径的对象也必须满足协议） ──────────────────────────

def test_canonical_double_satisfies_protocol() -> None:
    """标准替身 `FakeSTTBackend` 满足全部协议成员——它是"合法替身"的定义。"""
    assert violations(FakeSTTBackend(), STTBackend) == []


def test_canonical_double_model_semantics() -> None:
    """替身的 `model` 由 provider_name + model 拼成（两个参数都可配）。"""
    backend = FakeSTTBackend(provider_name="sensevoice", model="sense-voice")
    assert backend.model == "sensevoice/sense-voice"


def test_canonical_double_maintains_optional_attributes() -> None:
    """协议 docstring 声明的 `last_emotion` / `last_events` 由标准替身维护。"""
    backend = FakeSTTBackend(emotion="HAPPY", events=["Laughter"])
    assert (backend.last_emotion, backend.last_events) == ("HAPPY", ["Laughter"])


def test_duck_typed_fake_without_model_is_rejected() -> None:
    """duck-typed 替身只写 transcribe 时，契约检查必须判它不合规。"""

    class _BadSttBackend:
        """故意违反 Module/stt/protocol.py。"""

        async def transcribe(self, audio: bytes, **kwargs: object) -> str:
            return ""

    problems = violations(_BadSttBackend(), STTBackend)
    assert problems, "缺成员的对象必须被契约检查判为不合规"
    assert any(p.startswith("model:") for p in problems), problems
    assert any(p.startswith("close:") for p in problems), problems


def test_guard_is_not_vacuous() -> None:
    """空对象必须**逐项**报缺失——报不全说明比对逻辑漏项。"""

    class _Empty:
        """什么都不实现的空壳。"""

    problems = violations(_Empty(), STTBackend)
    assert len(problems) == len(_PROTOCOL_SURFACE), (
        f"空实现应逐项报差异（协议面 {sorted(_PROTOCOL_SURFACE)}），实报 {problems}"
    )
