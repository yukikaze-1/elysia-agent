"""Storage-backed character runtime source loading tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.character.revision import CharacterRevision
from core.character.runtime_loader import (
    CharacterRuntimeSourceError,
    CharacterRuntimeSourceLoader,
)
from core.character.service import CharacterInstanceNotFoundError
from core.storage.base import StorageUnavailableError

pytestmark = pytest.mark.service


def _loader(*, instances: AsyncMock, characters: AsyncMock) -> CharacterRuntimeSourceLoader:
    builtins = MagicMock()
    return CharacterRuntimeSourceLoader(
        characters=characters,
        instances=instances,
        builtins=builtins,
        owner_user_id="usr_local_default",
    )


@pytest.mark.asyncio
async def test_custom_runtime_loads_its_pinned_revision() -> None:
    instance = CharacterInstance(
        instanceId="chi_custom_a",
        characterId="chr_custom_a",
        revision=3,
        revisionPolicy="pinned",
        ownerUserId="usr_local_default",
    )
    definition = CharacterDefinition(
        characterId="chr_custom_a", source="custom", mutable=True,
        currentRevision=4,
    )
    revision = CharacterRevision(
        characterId="chr_custom_a", revision=3, displayName="A"
    )
    instances = AsyncMock()
    instances.get.return_value = instance
    characters = AsyncMock()
    characters.resolve_runtime_source.return_value = (definition, revision)

    loaded = await _loader(instances=instances, characters=characters).load(
        "chi_custom_a"
    )

    assert loaded == (definition, revision, instance)
    characters.resolve_runtime_source.assert_awaited_once_with(
        "chr_custom_a", revision=3
    )


@pytest.mark.asyncio
async def test_custom_runtime_does_not_fallback_when_instance_is_missing() -> None:
    instances = AsyncMock()
    instances.get.return_value = None
    characters = AsyncMock()

    with pytest.raises(CharacterInstanceNotFoundError):
        await _loader(instances=instances, characters=characters).load("chi_missing")

    characters.resolve_runtime_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_runtime_hides_foreign_owner() -> None:
    instances = AsyncMock()
    instances.get.return_value = CharacterInstance(
        instanceId="chi_foreign", characterId="chr_custom_a", ownerUserId="usr_other"
    )
    characters = AsyncMock()

    with pytest.raises(CharacterInstanceNotFoundError):
        await _loader(instances=instances, characters=characters).load("chi_foreign")


@pytest.mark.asyncio
async def test_builtin_survives_instance_storage_outage() -> None:
    instance = CharacterInstance(
        instanceId="chi_builtin_cyrene", characterId="chr_builtin_cyrene"
    )
    definition = CharacterDefinition(
        characterId="chr_builtin_cyrene", source="builtin"
    )
    revision = CharacterRevision(
        characterId="chr_builtin_cyrene", displayName="Cyrene"
    )
    instances = AsyncMock()
    instances.get.side_effect = StorageUnavailableError("offline")
    characters = AsyncMock()
    loader = _loader(instances=instances, characters=characters)
    loader._builtins.load.return_value = (definition, revision, instance)

    assert await loader.load("chi_builtin_cyrene") == (
        definition, revision, instance
    )


@pytest.mark.asyncio
async def test_non_builtin_instance_cannot_reference_builtin_definition() -> None:
    instance = CharacterInstance(
        instanceId="chi_spoof", characterId="chr_builtin_elysia",
        revisionPolicy="pinned",
    )
    definition = CharacterDefinition(
        characterId="chr_builtin_elysia", source="builtin"
    )
    revision = CharacterRevision(
        characterId="chr_builtin_elysia", displayName="Elysia"
    )
    instances = AsyncMock()
    instances.get.return_value = instance
    characters = AsyncMock()
    characters.resolve_runtime_source.return_value = (definition, revision)

    with pytest.raises(CharacterRuntimeSourceError):
        await _loader(instances=instances, characters=characters).load("chi_spoof")
