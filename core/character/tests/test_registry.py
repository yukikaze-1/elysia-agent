# core/character/tests/test_registry.py
import asyncio

import pytest
pytestmark = pytest.mark.service   # 内存 LRU
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity


def _factory(instance_id):
    def _make():
        instance = CharacterInstance(instanceId=instance_id, characterId="chr_test")
        definition = CharacterDefinition(characterId="chr_test")
        revision = CharacterRevision(characterId="chr_test", displayName="Test")
        ui = UserIdentity()
        return CharacterRuntime(instance, definition, revision, ui)
    return _make


def test_registry_get_caches():
    reg = CharacterRuntimeRegistry()
    rt = reg.get("chi_1", _factory("chi_1"))
    assert rt.instance.instanceId == "chi_1"
    rt2 = reg.get("chi_1", _factory("chi_1"))
    assert rt2 is rt   # 缓存命中


@pytest.mark.asyncio
async def test_registry_async_load_is_single_flight():
    reg = CharacterRuntimeRegistry()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return _factory("chi_async")()

    first, second = await asyncio.gather(
        reg.get_or_load("chi_async", load),
        reg.get_or_load("chi_async", load),
    )

    assert first is second
    assert calls == 1


@pytest.mark.asyncio
async def test_registry_rejects_mismatched_async_loader_result():
    reg = CharacterRuntimeRegistry()

    async def load():
        return _factory("chi_wrong")()

    with pytest.raises(ValueError, match="mismatched instance"):
        await reg.get_or_load("chi_expected", load)


def test_registry_invalidate():
    reg = CharacterRuntimeRegistry()
    rt = reg.get("chi_1", _factory("chi_1"))
    reg.invalidate("chi_1", "test")
    rt2 = reg.get("chi_1", _factory("chi_1"))
    assert rt2 is not rt


def test_registry_release_evicts():
    reg = CharacterRuntimeRegistry()
    reg.get("chi_1", _factory("chi_1"))
    reg.release("chi_1", "session_evicted")
    assert reg.get("chi_1", _factory("chi_1")) is not None  # 释放后可重建，但缓存清掉了


"""CharacterRuntimeRegistry 激活测试（M3：_busy lease + LRU 驱逐/flush）。"""
import pytest
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity

def _factory(iid="chi_x"):
    def f():
        return CharacterRuntime(
            instance=CharacterInstance(instanceId=iid, characterId="chr_x"),
            definition=CharacterDefinition(characterId="chr_x"),
            revision=CharacterRevision(characterId="chr_x", revision=1, displayName="X"),
            user_identity=UserIdentity(userId="usr_local_default"),
        )
    return f

def test_get_per_instance_lazy():
    reg = CharacterRuntimeRegistry()
    rt = reg.get("chi_x", _factory())
    assert rt is not None
    assert reg.get("chi_x", _factory()) is rt

def test_mark_busy_prevents_evict():
    reg = CharacterRuntimeRegistry(max_size=1)
    reg.get("chi_a", _factory("chi_a"))
    rt_b = reg.get("chi_b", _factory("chi_b"))
    reg.mark_busy("chi_b", True)
    reg.get("chi_c", _factory("chi_c"))
    assert reg.get("chi_b", _factory("chi_b")) is rt_b


def test_evicts_oldest_when_not_busy():
    """P2-3：非 busy 的 runtime 按 LRU 正常驱逐（不再有死掉的绑定索引保护）。"""
    reg = CharacterRuntimeRegistry(max_size=1)
    rt_a = reg.get("chi_a", _factory("chi_a"))
    rt_b = reg.get("chi_b", _factory("chi_b"))
    # max_size=1 → 取 chi_b 时 chi_a 被驱逐
    assert reg.get_existing("chi_a") is None
    assert reg.get_existing("chi_b") is rt_b
    assert rt_a is not rt_b


@pytest.mark.asyncio
async def test_evict_flushes_state_before_drop():
    """P2-3：被驱逐的 runtime 先 flush 落库再丢弃（架构稿 §7.1「驱逐前必须 flush」）。"""
    reg = CharacterRuntimeRegistry(max_size=1)
    persisted = []

    async def _persist(state):
        persisted.append(state)

    rt_a = reg.get("chi_a", _factory("chi_a"))
    rt_a.set_persist_fn(_persist)

    reg.get("chi_b", _factory("chi_b"))   # 超出 max_size → 驱逐 chi_a
    assert reg.get_existing("chi_a") is None
    await asyncio.sleep(0)                # 让 create_task 调度的 flush 执行
    await asyncio.sleep(0)
    assert persisted, "被驱逐的 runtime 未 flush（状态会随对象丢弃）"
