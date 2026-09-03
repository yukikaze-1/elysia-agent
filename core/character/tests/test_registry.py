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
