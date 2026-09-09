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


class _FakeLock:
    """只实现 `locked()` 的锁替身——evolution_locks_busy 只读这一个方法。"""

    def __init__(self) -> None:
        self.held = False

    def locked(self) -> bool:
        return self.held


def _engine_with_locks() -> SimpleNamespace:
    return SimpleNamespace(
        _reflector=SimpleNamespace(_reflect_lock=_FakeLock()),
        _compression=SimpleNamespace(_compression_lock=_FakeLock()),
    )


def test_generation_lease_counts_nested_turns():
    rt = _rt()
    assert rt.is_generation_busy is False
    rt.begin_generation()
    rt.begin_generation()
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
    rt.begin_generation()
    assert rt.is_generation_busy is True
    rt.end_generation()


def test_destructive_mutation_rejected_while_generation_busy():
    rt = _rt()
    rt.begin_generation()
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
    rt.begin_generation()
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
    engine._reflector._reflect_lock.held = True
    try:
        assert rt.try_begin_destructive_mutation() is False
    finally:
        engine._reflector._reflect_lock.held = False
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
