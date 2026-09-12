# core/character/tests/test_runtime_lease.py
"""BE-STAGEH-01：CharacterRuntime 的 generation / 破坏性变更 lease 语义。

真实联调复现：generation 在途时 reset-evolution 未被拒绝，两处并发写同一人格状态。
本文件锁定 lease 的原子语义——判定与登记必须同处一把同步锁内完成。
"""

from types import SimpleNamespace

import pytest

from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.character.revision import CharacterRevision
from core.character.runtime import CharacterRuntime
from core.profile.user_identity import UserIdentity

pytestmark = pytest.mark.unit


def _rt() -> CharacterRuntime:
    return CharacterRuntime(
        instance=CharacterInstance(instanceId="chi_lease", characterId="chr_lease"),
        definition=CharacterDefinition(characterId="chr_lease"),
        revision=CharacterRevision(
            characterId="chr_lease", revision=1, displayName="Lease"),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )


class _FakeEngine:
    """`CharacterEngineProtocol.evolution_in_flight()` 的最小替身。

    2026-09-12 前这里造的是 `SimpleNamespace(_reflector=..., _compression=...)`
    + 自造 `_FakeLock`——替身照抄了引擎的私有结构，把「引擎内部怎么组合锁」
    固化进了 core/character 的测试。现在替身只实现协议里被消费的那一项。
    """

    def __init__(self) -> None:
        self.busy = False

    def evolution_in_flight(self) -> bool:
        return self.busy


def _engine_with_locks() -> _FakeEngine:
    return _FakeEngine()


def test_generation_lease_counts_nested_turns():
    rt = _rt()
    assert rt.is_generation_busy is False
    assert rt.try_begin_generation() is True
    assert rt.try_begin_generation() is True
    assert rt.is_generation_busy is True
    rt.end_generation()
    assert rt.is_generation_busy is True
    rt.end_generation()
    assert rt.is_generation_busy is False


def test_end_generation_without_begin_is_noop():
    """多释放一次不得把计数压成负数（否则 lease 永久失效）。"""
    rt = _rt()
    rt.end_generation()
    rt.end_generation()
    assert rt.is_generation_busy is False
    assert rt.try_begin_generation() is True
    assert rt.is_generation_busy is True
    rt.end_generation()


def test_destructive_mutation_rejected_while_generation_busy():
    rt = _rt()
    assert rt.try_begin_generation() is True
    try:
        assert rt.try_begin_destructive_mutation() is False
    finally:
        rt.end_generation()
    assert rt.try_begin_destructive_mutation() is True
    rt.end_destructive_mutation()


def test_lease_maintains_busy_flag_for_eviction_guard():
    """_busy 由 lease 维护——registry._evict_if_needed 读它做驱逐保护。"""
    rt = _rt()
    assert rt._busy is False
    assert rt.try_begin_generation() is True
    assert rt._busy is True
    rt.end_generation()
    assert rt._busy is False
    assert rt.try_begin_destructive_mutation() is True
    assert rt._busy is True
    rt.end_destructive_mutation()
    assert rt._busy is False


def test_destructive_mutation_rejected_while_evolution_lock_held():
    """反思/压缩持锁时 reset 也必须被拒——这是 busy 的第二个来源。"""
    rt = _rt()
    engine = _engine_with_locks()
    rt.bind_engine(engine)  # type: ignore[arg-type]
    engine.busy = True
    try:
        assert rt.try_begin_destructive_mutation() is False
    finally:
        engine.busy = False
    assert rt.try_begin_destructive_mutation() is True
    rt.end_destructive_mutation()


def test_destructive_mutation_releases_when_cancelled():
    """异常/取消路径由调用方 finally 释放——这里锁定释放后的可重入性。"""
    rt = _rt()
    assert rt.try_begin_destructive_mutation() is True
    rt.end_destructive_mutation()
    assert rt.is_destructive_mutation_active is False
    assert rt.try_begin_destructive_mutation() is True
    rt.end_destructive_mutation()


def test_generation_rejected_while_destructive_mutation_active():
    """对称互斥：reset 在途时 generation 不得开始（不能只靠 route 的 _process_lock）。"""
    rt = _rt()
    assert rt.try_begin_destructive_mutation() is True
    try:
        assert rt.try_begin_generation() is False
        assert rt.is_generation_busy is False
    finally:
        rt.end_destructive_mutation()
    assert rt.try_begin_generation() is True
    rt.end_generation()


def test_tail_lease_keeps_busy_after_main_turn_released():
    """P0-1：主回合 lease 释放后，tail lease 仍须令 reset 被拒。"""
    rt = _rt()
    assert rt.try_begin_generation() is True
    rt.begin_generation_tail()
    rt.end_generation()                      # 主回合结束
    assert rt.is_generation_busy is True     # tail 仍在
    assert rt.try_begin_destructive_mutation() is False
    rt.end_generation()                      # tail task 结束
    assert rt.is_generation_busy is False
    assert rt.try_begin_destructive_mutation() is True
    rt.end_destructive_mutation()


def test_lease_is_per_instance():
    """不同 instance 各自演化——lease 不引入全局领域锁。"""
    a, b = _rt(), _rt()
    assert a.try_begin_generation() is True
    assert b.is_generation_busy is False
    assert b.try_begin_destructive_mutation() is True
    assert a.is_destructive_mutation_active is False
    b.end_destructive_mutation()
    a.end_generation()
