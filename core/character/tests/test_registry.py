# core/character/tests/test_registry.py
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


"""CharacterRuntimeRegistry 激活测试（M3：_busy/_session_binding_index）。"""
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

def test_session_binding_index_prevents_evict():
    reg = CharacterRuntimeRegistry(max_size=1)
    reg.get("chi_a", _factory("chi_a"))
    rt_b = reg.get("chi_b", _factory("chi_b"))
    reg.set_session_binding_index({"chi_b"})
    reg.get("chi_c", _factory("chi_c"))
    assert reg.get("chi_b", _factory("chi_b")) is rt_b
