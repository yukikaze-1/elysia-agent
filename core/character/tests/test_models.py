import pytest
pytestmark = pytest.mark.unit   # 纯模型，无 I/O
from pydantic import ValidationError
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.instance import CharacterInstance


def test_definition_defaults():
    d = CharacterDefinition(characterId="chr_builtin_elysia")
    assert d.source == "builtin"
    assert d.mutable is False
    assert d.status == "active"
    assert d.currentRevision == 1


def test_revision_defaults():
    r = CharacterRevision(characterId="chr_builtin_elysia", displayName="Elysia")
    assert r.revision == 1
    assert r.schemaVersion == 1
    assert r.promptProfile == {}           # 本批 promptProfile 只承载有源字段，默认空
    assert r.createdBy == "system"


def test_revision_revision_validation():
    with pytest.raises(ValidationError):
        CharacterRevision(characterId="chr", displayName="X", revision=0)


def test_instance_defaults():
    i = CharacterInstance(instanceId="chi_builtin_elysia", characterId="chr_builtin_elysia")
    assert i.revision == 1
    assert i.stateVersion == 1
    assert i.ownerUserId == "usr_local_default"
