"""CharacterEngine 接口层契约测试（ARCH-INC-P1-02 / 引擎可替换性）。

## 这个文件在守什么

`core/personality/protocol.py` 用 `typing.Protocol` 把引擎契约与实现解耦，让
`CharacterEngine` 可以**单独迭代或整体替换**。结构化契约的代价是：**没有实例化期
强制**——实现少一个方法、改一个参数名，都不会像 ABC 那样在构造时炸。

本文件补上那道强制：

1. `CharacterEngine` / `CharacterReflector` 必须满足各自协议的**每一个成员**（含
   `state` 这类数据成员——`isinstance` 才查得到，类级 `hasattr` 查不到）；
2. 方法**签名**必须与协议一致（参数名与种类逐个比对；参数名漂移会让关键字调用
   在运行时才炸，是最隐蔽的一类破坏）；
3. `CharacterEngine` **不得继承**协议——若继承，协议里 `...` 的空方法体会变成静默
   可用的 no-op，漏实现反而不会报错，与建立契约的目的相反。

静态侧由 `make pyright` 在「具体实现 → 接口类型」的赋值点做同一件事（本文件建立
时它当场抓出 `buffer_message` 的 `reflector` 参数比契约更窄）。
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from core.personality.engine import CharacterEngine
from core.personality.protocol import (
    CharacterEngineProtocol,
    CharacterReflectorProtocol,
)
from core.personality.reflector import CharacterReflector

pytestmark = pytest.mark.unit


def _protocol_members(proto: type) -> dict[str, Any]:
    """协议自身声明的成员（滤掉 Protocol 机制自带的 dunder）。"""
    members: dict[str, Any] = {}
    for name in dir(proto):
        if name.startswith("_"):
            continue
        members[name] = getattr(proto, name)
    return members


def _make_engine() -> CharacterEngine:
    """最小可构造的 CharacterEngine（不触网、不读数据）。"""
    from unittest.mock import AsyncMock, MagicMock

    from core.adapter.llm_adapter import ThoughtResult
    from core.config import CharacterConfig
    from core.schema.character import CharacterState
    from core.schema.types import SessionId

    llm = MagicMock()
    llm.think = AsyncMock(return_value=ThoughtResult(text=""))
    return CharacterEngine(
        llm=llm,
        state=CharacterState(session_id=SessionId("protocol-conformance")),
        character_name="Elysia",
        user_name="妖木",
        base_personality="你是 Elysia。",
        character_config=CharacterConfig(name="Elysia"),
        output=MagicMock(),
    )


# ── 成员齐备 ──────────────────────────────────────────────────────────────────

def test_character_engine_satisfies_protocol() -> None:
    """引擎实例满足协议全部成员（含数据成员 `state`）。"""
    engine = _make_engine()
    missing = [n for n in _protocol_members(CharacterEngineProtocol)
               if not hasattr(engine, n)]
    assert missing == [], f"CharacterEngine 缺少契约成员：{missing}"
    assert isinstance(engine, CharacterEngineProtocol), "协议成员齐备但 isinstance 不通过"


def test_character_reflector_satisfies_protocol() -> None:
    """反思器实例满足其窄面协议。"""
    reflector = _make_engine().reflector
    assert reflector is not None
    missing = [n for n in _protocol_members(CharacterReflectorProtocol)
               if not hasattr(reflector, n)]
    assert missing == [], f"CharacterReflector 缺少契约成员：{missing}"
    assert isinstance(reflector, CharacterReflectorProtocol)


# ── 签名一致 ──────────────────────────────────────────────────────────────────

def _comparable(proto: type) -> dict[str, Any]:
    """协议里可做签名比对的成员（方法 + property 的 getter）。

    `state` 这类**数据成员**不在其中——它只有注解没有取值，实例侧由
    `isinstance` 检查（`test_character_engine_satisfies_protocol`）。
    """
    out: dict[str, Any] = {}
    for name in dir(proto):
        if name.startswith("_"):
            continue
        member = getattr(proto, name)
        if isinstance(member, property):
            member = member.fget
        if callable(member):
            out[name] = member
    return out


def _signature_diff(proto: type, impl: type) -> list[str]:
    """逐方法比对参数名与种类（不比对注解——注解由 pyright 门禁负责）。"""
    diffs: list[str] = []
    for name, member in _comparable(proto).items():
        impl_member = getattr(impl, name, None)
        if isinstance(impl_member, property):
            impl_member = impl_member.fget
        if impl_member is None:
            diffs.append(f"{name}: 实现中不存在")
            continue
        proto_params = [(p.name, p.kind) for p in inspect.signature(member).parameters.values()]
        impl_params = [
            (p.name, p.kind) for p in inspect.signature(impl_member).parameters.values()
        ]
        if proto_params != impl_params:
            diffs.append(f"{name}: 契约 {proto_params} ≠ 实现 {impl_params}")
    return diffs


def test_character_engine_signatures_match_protocol() -> None:
    """方法签名（参数名 + 种类）与契约一致——关键字调用不会在运行时才炸。"""
    assert _signature_diff(CharacterEngineProtocol, CharacterEngine) == []


def test_character_reflector_signatures_match_protocol() -> None:
    assert _signature_diff(CharacterReflectorProtocol, CharacterReflector) == []


# ── 设计意图：不继承 ──────────────────────────────────────────────────────────

def test_engine_does_not_inherit_protocol() -> None:
    """引擎**不得**继承协议：继承会让空方法体变成静默 no-op，漏实现不再报错。"""
    assert CharacterEngineProtocol not in CharacterEngine.__mro__, (
        "CharacterEngine 继承了 CharacterEngineProtocol——协议里的 `...` 方法体会变成"
        "可用的 no-op 实现，漏实现一个方法将不再报错。保持结构化（不继承），"
        "由本文件与 make pyright 共同把关。"
    )


# ── 守卫自身的可靠性 ──────────────────────────────────────────────────────────

def test_guard_is_not_vacuous() -> None:
    """守卫必须真的在比对：协议成员非空，且空实现对每一项都报差异。"""
    assert len(_protocol_members(CharacterEngineProtocol)) >= 10
    assert len(_protocol_members(CharacterReflectorProtocol)) >= 10

    class _Empty:
        """什么都不实现的空壳——所有契约方法都必须被报为缺失。"""

    diffs = _signature_diff(CharacterEngineProtocol, _Empty)
    assert len(diffs) == len(_comparable(CharacterEngineProtocol)), (
        "空实现应逐项报差异；报不全说明比对逻辑漏项"
        f"（可比对成员 {sorted(_comparable(CharacterEngineProtocol))}，实报 {diffs}）"
    )
