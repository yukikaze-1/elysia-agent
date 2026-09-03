# core/character/tests/test_runtime.py
import pytest
pytestmark = pytest.mark.unit   # 纯容器
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity


def _make_runtime():
    instance = CharacterInstance(instanceId="chi_test", characterId="chr_test")
    definition = CharacterDefinition(characterId="chr_test")
    revision = CharacterRevision(characterId="chr_test", displayName="Test")
    ui = UserIdentity()
    return CharacterRuntime(instance, definition, revision, ui)


def test_runtime_holds_snapshot():
    rt = _make_runtime()
    assert rt.instance.instanceId == "chi_test"
    assert rt.definition.characterId == "chr_test"
    assert rt.revision.displayName == "Test"
    assert rt.user_identity.pronouns == "她"


def test_runtime_lazy_engine():
    rt = _make_runtime()
    assert rt.engine is None   # 懒建
