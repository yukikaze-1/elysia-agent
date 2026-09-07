from unittest.mock import AsyncMock

import pytest

from core.character.session_binding import (
    CharacterSessionBindingService,
    canonical_instance_id,
)
from core.character.service import CharacterArchivedError, CharacterNotFoundError
from core.profile.profile_store import UserIdentityStore
from core.storage.character_instance_repo import CharacterInstanceRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from frontend_contract.rest import (
    CharacterDetail,
    CharacterPresentation,
    CharacterPromptProfile,
    CharacterRevisionContent,
    CharacterRuntimeDefaults,
    CharacterRuntimeEmotion,
    CharacterRuntimeProactive,
    CharacterRuntimeVoice,
    CharacterSummary,
)

pytestmark = pytest.mark.unit


def _detail(*, character_id: str, source: str = "builtin", status: str = "active"):
    presentation = CharacterPresentation(
        avatarAssetId=None, live2dModelId=None, themeColor="#88aaff",
    )
    return CharacterDetail(
        summary=CharacterSummary(
            characterId=character_id,
            source=source,
            mutable=source == "custom",
            status=status,
            currentRevision=3,
            displayName="Cyrene",
            description="",
            aliases=[],
            presentation=presentation,
            updatedAt="2026-09-07T00:00:00Z",
        ),
        current=CharacterRevisionContent(
            characterId=character_id,
            revision=3,
            schemaVersion=1,
            displayName="Cyrene",
            description="",
            aliases=[],
            promptProfile=CharacterPromptProfile(
                corePersonality="core",
                relationshipStyle="kind",
                emotionStyle="",
                speakingStyle="soft",
                conversationInitiative="",
                examples=[],
            ),
            runtimeDefaults=CharacterRuntimeDefaults(
                voice=CharacterRuntimeVoice(),
                emotion=CharacterRuntimeEmotion(idleExpressions=[]),
                wakeWords=[],
                proactive=CharacterRuntimeProactive(
                    enabled=False, intervalSeconds=1800, triggers=[],
                ),
            ),
            presentation=presentation,
            openingMessage=None,
            checksum="sum",
            compilerVersion=1,
            createdBy="system",
            createdAt="2026-09-07T00:00:00Z",
        ),
    )


def _service(detail):
    characters = AsyncMock()
    characters.get.return_value = detail
    identity = UserIdentityStore()
    identity.bootstrap("用户")
    instances = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    return CharacterSessionBindingService(
        character_service=characters,
        instance_repo=instances,
        user_identity_store=identity,
    ), characters, instances, identity


def test_canonical_instance_id_is_stable_and_builtin_readable():
    assert canonical_instance_id("u", "chr_builtin_cyrene") == "chi_builtin_cyrene"
    assert canonical_instance_id("u", "chr_custom_a") == canonical_instance_id(
        "u", "chr_custom_a",
    )
    assert canonical_instance_id("u", "chr_custom_a") != canonical_instance_id(
        "u", "chr_custom_b",
    )


@pytest.mark.asyncio
async def test_builtin_resolution_uses_user_default_without_instance_storage():
    service, characters, instances, identity = _service(
        _detail(character_id="chr_builtin_cyrene"),
    )
    profile = identity.get()
    assert profile is not None
    profile.defaultCharacterId = "chr_builtin_cyrene"
    resolved = await service.resolve_for_create(None)
    assert resolved.binding == {
        "character_id": "chr_builtin_cyrene",
        "instance_id": "chi_builtin_cyrene",
    }
    assert resolved.snapshot.displayName == "Cyrene"
    characters.get.assert_awaited_once_with("chr_builtin_cyrene")
    assert await instances.get("chi_builtin_cyrene") is None


@pytest.mark.asyncio
async def test_custom_resolution_creates_and_reuses_canonical_instance():
    service, _, instances, _ = _service(
        _detail(character_id="chr_custom_a", source="custom"),
    )
    first = await service.resolve_for_create("chr_custom_a")
    second = await service.resolve_for_create("chr_custom_a")
    assert first.binding == second.binding
    stored = await instances.get(first.binding["instance_id"])
    assert stored is not None
    assert stored.revision == 3
    assert stored.canonical is True


@pytest.mark.asyncio
async def test_resolution_rejects_missing_or_archived_character():
    service, characters, _, _ = _service(None)
    with pytest.raises(CharacterNotFoundError):
        await service.resolve_for_create("chr_missing")
    characters.get.return_value = _detail(
        character_id="chr_custom_archived", source="custom", status="archived",
    )
    with pytest.raises(CharacterArchivedError):
        await service.resolve_for_create("chr_custom_archived")
