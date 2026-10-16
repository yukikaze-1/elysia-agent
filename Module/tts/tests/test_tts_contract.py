"""TTS 后端契约测试 —— 「生产实现真的满足 `TTSBackend` 吗」的机器判据。

## 这一层补的是什么

`make pyright` 在生产代码里只抓得住一件事：**继承者漏实现抽象成员**
（`reportAbstractUsage`，实例化即红）。实测它**不**检查覆写形态——把 `async def`
覆写成同步 `def`、把 `model` 覆写成返回 `None`，pyright 在 `basic` 模式下均报
0 error；而 `**/tests/**` 整片被排除，测试替身完全在静态视野之外。

所以本文件负责静态层看不见的那部分：

| 判据 | 防的是什么 |
|------|-----------|
| 协议面齐备（`violations`） | 少成员、成员形态错（同步化了、property 变成可调用对象） |
| `model` 语义 | 返回非 `str` / 拼错 provider 前缀——`services.tts.model` 会直接下发到前端 |
| `synthesize_stream` 是**真的**异步生成器 | 声明成协程但没 `yield` ⇒ 调用方 `async for` 当场炸 |
| 构造不触网 | 契约测试自己变成 integration 测试（"测契约"顺手连了云端） |
| 守卫非空转 | 比对逻辑本身写错，于是永远报"合规" |

判据 `violations()` 的成员清单**从协议类推导**（`dir(TTSBackend)` 的公开成员），
不是手抄一份——往协议里加成员，这里立刻要求所有实现跟上。
"""

from __future__ import annotations

import inspect
import socket
from collections.abc import Callable

import pytest

from Module._common.protocol_contract import public_members, violations
from Module.tts.gpt_sovits import GPTSoVitsBackend
from Module.tts.minimax_tts import MiniMaxTTSBackend
from Module.tts.openai_tts import OpenAICompatTTSBackend
from Module.tts.protocol import TTSBackend
from Module.tts.registry import TTSFactory
from Module.tts.testing import FakeTTSBackend

pytestmark = pytest.mark.service

#: 不可达的本地端口：契约测试**不**连它，但后端若在构造期真的发起连接，
#: `test_construction_does_not_touch_the_network` 的 socket 守卫会先一步炸掉。
_UNREACHABLE = "http://127.0.0.1:1"

#: `TTSBackend` 声明的全部公开成员（5 抽象 + 5 带默认实现）。
#: 钉死它是为了让"协议面被改动"这件事**显式可见**——只加成员不加实现会被
#: `violations()` 抓；这里再钉一份，是为了让"协议面变了"本身也能被 review 到。
_PROTOCOL_SURFACE = frozenset({
    "synthesize", "synthesize_stream", "test", "provider_name", "close",   # 抽象
    "model", "list_voices", "stream_format", "default_voice", "get_preprocess",  # 具体默认
})


def _gpt_sovits() -> TTSBackend:
    return GPTSoVitsBackend(model="default", base_url=_UNREACHABLE)


def _minimax() -> TTSBackend:
    return MiniMaxTTSBackend(
        model="speech-2.8-hd", api_key="contract-test-key", base_url=_UNREACHABLE,
    )


def _openai_compat() -> TTSBackend:
    return OpenAICompatTTSBackend(
        model="tts-1", api_key="contract-test-key", base_url=_UNREACHABLE,
    )


#: `(provider_name, 工厂 callable, provider/model 标识)` —— callable 的返回注解是
#: `TTSBackend`，于是"实现是否可赋给协议类型"这件事连 pyright 也一起验了。
_BACKENDS: list[tuple[str, Callable[[], TTSBackend], str]] = [
    ("gpt-sovits", _gpt_sovits, "gpt-sovits/default"),
    ("minimax", _minimax, "minimax/speech-2.8-hd"),
    ("custom", _openai_compat, "custom/tts-1"),
]

_PARAMS = [pytest.param(build, name, model_id, id=name)
           for name, build, model_id in _BACKENDS]


# ── 协议面 ────────────────────────────────────────────────────────────────────

def test_protocol_surface_matches_this_file() -> None:
    """协议面的成员集合与本文档钉住的清单一致（加成员必须同步这里）。"""
    assert public_members(TTSBackend) == _PROTOCOL_SURFACE, (
        "`TTSBackend` 的公开成员变了。这不是错误本身，而是提醒："
        "① 三个生产后端的契约断言会自动跟上；② 测试替身需要补新成员——"
        "用 Module/tts/testing.py::FakeTTSBackend 的地方会自动继承到，"
        "duck-typed 替身则需要逐个补。"
    )


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_backend_satisfies_protocol(build, provider, model_id) -> None:
    """生产后端满足 `TTSBackend` 的**每一个**公开成员（含形态）。"""
    backend = build()
    assert violations(backend, TTSBackend) == []


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_model_is_provider_qualified(build, provider, model_id) -> None:
    """`model` 是 ``provider/model`` 的完整标识——它会被原样下发到前端下拉。

    `model` 在 ABC 上**不是**抽象成员（有默认实现），所以"没写"不会报错；
    这条断言负责它的**语义**：非空字符串、前缀正确。
    """
    backend = build()
    model = backend.model
    assert isinstance(model, str) and model, f"{provider} 的 model 应为非空字符串，实为 {model!r}"
    assert model == model_id


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_provider_name_matches(build, provider, model_id) -> None:
    assert build().provider_name == provider


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_async_members_have_async_shape(build, provider, model_id) -> None:
    """异步成员必须是协程函数；`synthesize_stream` 必须是**真的**异步生成器。

    `violations()` 只验证"声明 async 的实现也是 async"；这里额外钉住流式方法
    真的能 `async for`——声明成协程却不 `yield` 的实现会在这里红，而不是等到
    调用方 `async for` 时才发现。
    """
    backend = build()
    for name in ("synthesize", "test", "close"):
        assert inspect.iscoroutinefunction(getattr(backend, name)), f"{name} 应为协程函数"
    assert inspect.isasyncgenfunction(backend.synthesize_stream), (
        "synthesize_stream 应为异步生成器（调用方 `async for` 它）"
    )


@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_construction_does_not_touch_the_network(build, provider, model_id, monkeypatch) -> None:
    """构造后端不发起任何连接——契约测试不能悄悄变成 integration 测试。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"{provider} 后端在构造期发起了网络连接")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    build()  # 不抛即通过


# ── 工厂路径（Agent 真正走的那条） ────────────────────────────────────────────

@pytest.mark.parametrize("build, provider, model_id", _PARAMS)
def test_factory_output_satisfies_protocol(build, provider, model_id, monkeypatch) -> None:
    """`TTSFactory.create` 的返回值同样满足协议——它是 Agent 实际拿到的东西。"""
    monkeypatch.setenv("GPT_SOVITS_URL", _UNREACHABLE)
    monkeypatch.setenv("MINIMAX_TTS_URL", _UNREACHABLE)
    monkeypatch.setenv("MINIMAX_API_KEY", "contract-test-key")
    monkeypatch.setenv("CUSTOM_TTS_API_KEY", "contract-test-key")

    backend = TTSFactory.create(model_id, base_url=_UNREACHABLE)
    assert violations(backend, TTSBackend) == []
    assert backend.model == model_id


# ── 测试替身（§7：注入生产路径的对象也必须满足协议） ──────────────────────────

def test_canonical_double_satisfies_protocol() -> None:
    """标准替身 `FakeTTSBackend` 满足全部协议成员——它是"合法替身"的定义。"""
    assert violations(FakeTTSBackend(), TTSBackend) == []


def test_canonical_double_model_semantics() -> None:
    """替身的 `model` 由 provider_name + model 拼成（两个参数都可配）。"""
    backend = FakeTTSBackend(provider_name="minimax", model="speech-2.8-hd")
    assert backend.model == "minimax/speech-2.8-hd"


def test_duck_typed_fake_without_model_is_rejected() -> None:
    """**2026-09-27 那个坏 fake 的形状**：duck-typed、只有 list_voices/close。

    它当时让 v2 的 `server_info` 整帧消失。这条判据保证同样的对象在**契约层**
    立刻失败（而不是等某个测试碰巧读到 `.model`）。
    """

    class _BadTtsBackend:
        """故意违反 Module/tts/protocol.py。"""

        async def list_voices(self):
            return []

        async def close(self):
            return None

    problems = violations(_BadTtsBackend(), TTSBackend)
    assert problems, "缺成员的对象必须被契约检查判为不合规"
    assert any(p.startswith("model:") for p in problems), problems
    # 缺失是逐项报的，不是"第一个就返回"
    assert any(p.startswith("synthesize:") for p in problems), problems


def test_guard_is_not_vacuous() -> None:
    """空对象必须**逐项**报缺失——报不全说明比对逻辑漏项。"""

    class _Empty:
        """什么都不实现的空壳。"""

    problems = violations(_Empty(), TTSBackend)
    assert len(problems) == len(_PROTOCOL_SURFACE), (
        f"空实现应逐项报差异（协议面 {sorted(_PROTOCOL_SURFACE)}），实报 {problems}"
    )
