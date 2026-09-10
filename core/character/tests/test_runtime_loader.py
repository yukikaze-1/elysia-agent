"""Storage-backed character runtime source loading tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog
from core.character.definition import CharacterDefinition
from core.character.draft import CharacterDraft
from core.character.instance import CharacterInstance
from core.character.revision import CharacterRevision
from core.character.runtime_loader import (
    CharacterRuntimeSourceError,
    CharacterRuntimeSourceLoader,
)
from core.character.service import (
    CharacterInstanceNotFoundError,
    CharacterNotFoundError,
    CharacterService,
    CharacterStorageUnavailableError,
)
from core.profile.profile_store import UserIdentityStore
from core.storage.base import StorageUnavailableError
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

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


# ---------------------------------------------------------------------------
# CharacterService.resolve_runtime_source —— 每轮运行时的活路径。
#
# 上面的用例里 `characters` 是 AsyncMock，等于没执行真实解析逻辑。这里改用
# FakeDocumentDBBackend 装配真实 repo，直接打真实实现的三条分支：
#   1) revision 缺省 → 用 definition.currentRevision；
#   2) revision 指定且 ≠ currentRevision → 返回该历史 revision（绝不被当前指针顶替）；
#   3) 请求的 revision 不存在 → fail-closed 抛 CharacterStorageUnavailableError；
#   4) 角色不存在 → CharacterNotFoundError；
#   5) 内置角色 revision 不符 → CharacterNotFoundError（不打存储）。
# ---------------------------------------------------------------------------

_BODY = "你是用于 runtime source 解析回归的测试角色。"


def _draft(**overrides: object) -> CharacterDraft:
    """最小合法 draft（字段满足 PromptProfile 的非空最小边界）。"""
    values: dict[str, object] = {
        "displayName": "解析角色",
        "promptProfile": {
            "corePersonality": _BODY,
            "relationshipStyle": _BODY,
            "speakingStyle": _BODY,
        },
    }
    values.update(overrides)
    return CharacterDraft(**values)


def _make_service() -> CharacterService:
    """真实 repo + 内置 catalog 的 CharacterService（内存假后端）。"""
    return CharacterService(
        repo=CharacterRepository(db=FakeDocumentDBBackend()),
        rev_repo=CharacterRevisionRepository(db=FakeDocumentDBBackend()),
        catalog=CharacterCatalog(source=BuiltinCharacterSource()),
        user_identity_store=UserIdentityStore(),
    )


@pytest.mark.asyncio
async def test_resolve_runtime_source_uses_current_revision_when_unspecified() -> None:
    """revision=None → 用 definition.currentRevision（首版为 1）。"""
    service = _make_service()
    detail = await service.create(_draft(displayName="A"), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId

    definition, revision = await service.resolve_runtime_source(character_id)

    assert definition.currentRevision == 1
    assert revision.revision == 1
    assert revision.displayName == "A"


@pytest.mark.asyncio
async def test_resolve_runtime_source_pins_requested_historical_revision() -> None:
    """revision=1 而 currentRevision=2 → 返回历史 revision 1，不被当前指针顶替。

    这是 resolve_runtime_source docstring 明令禁止的回归（"A requested historical
    revision is never replaced with the current pointer"）。
    """
    service = _make_service()
    detail = await service.create(_draft(displayName="A"), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId
    await service.update(character_id, _draft(displayName="B"), expected_revision=1)

    definition, revision = await service.resolve_runtime_source(character_id, revision=1)

    assert definition.currentRevision == 2  # 指针确实已前进
    assert revision.revision == 1           # 但请求的历史版本原样返回
    assert revision.displayName == "A"


@pytest.mark.asyncio
async def test_resolve_runtime_source_fails_closed_on_missing_revision() -> None:
    """请求的 revision 不存在 → CharacterStorageUnavailableError（不得静默顶替）。"""
    service = _make_service()
    detail = await service.create(_draft(displayName="A"), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId

    with pytest.raises(CharacterStorageUnavailableError):
        await service.resolve_runtime_source(character_id, revision=999)


@pytest.mark.asyncio
async def test_resolve_runtime_source_raises_when_character_missing() -> None:
    """角色不存在（既非内置也不在 custom 存储）→ CharacterNotFoundError。"""
    service = _make_service()

    with pytest.raises(CharacterNotFoundError):
        await service.resolve_runtime_source("chr_custom_missing")


@pytest.mark.asyncio
async def test_resolve_runtime_source_builtin_revision_mismatch_is_not_found() -> None:
    """内置角色请求非当前 revision → CharacterNotFoundError（不打存储）。"""
    service = _make_service()

    definition, revision = await service.resolve_runtime_source("chr_builtin_elysia")
    assert revision.revision == definition.currentRevision

    with pytest.raises(CharacterNotFoundError):
        await service.resolve_runtime_source("chr_builtin_elysia", revision=999)
