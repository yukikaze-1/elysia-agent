from unittest.mock import AsyncMock

import pytest

from core.character.session_binding import (
    CharacterSessionBindingService,
    SnapshotUnavailableCause,
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


@pytest.mark.asyncio
async def test_resolve_for_create_fail_closed_when_identity_unavailable():
    """P0-3：权威身份不可用时不静默回退内置默认角色——fail-closed（路由映射 503）。"""
    from core.storage.base import StorageUnavailableError
    from core.storage.user_identity_repo import UserIdentityRepository

    class _DownRepo(UserIdentityRepository):
        async def connect(self) -> bool:
            return False

    identity = UserIdentityStore(repo=_DownRepo())
    await identity.connect(user_name="用户")
    assert identity.get() is None

    characters = AsyncMock()
    characters.get.return_value = _detail(character_id="chr_builtin_cyrene")
    service = CharacterSessionBindingService(
        character_service=characters,
        instance_repo=CharacterInstanceRepository(db=FakeDocumentDBBackend()),
        user_identity_store=identity,
    )
    with pytest.raises(StorageUnavailableError):
        await service.resolve_for_create(None)
    characters.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# 读出期语义区分（问题单 §5.2 ★★☆）
# ---------------------------------------------------------------------------

class TestSnapshotUnavailableCause:
    """`snapshot_with_cause` 必须把「合法无绑定」与两类「数据缺口」分开。

    `character_profiles=true` 时前端要求每条会话都带角色快照，缺失即整页 fail-closed；
    此前三种成因一律返回 None、无法区分，运维无从定位。本区分只用于**服务端告警语义**，
    不参与 wire（响应仍省略 `character` 键，契约零变更）。
    """

    @pytest.mark.asyncio
    async def test_absent_binding_is_legacy_not_gap(self):
        """None / {} / 全空值对象 → LEGACY_ABSENT（合法，不告警）。"""
        service, _chars, _inst, _ident = _service(_detail(character_id="chr_x"))
        for value in (None, {}, {"character_id": "", "instance_id": ""},
                      {"characterId": None, "instanceId": None}):
            snapshot, cause = await service.snapshot_with_cause(value)
            assert snapshot is None
            assert cause is SnapshotUnavailableCause.LEGACY_ABSENT, repr(value)

    @pytest.mark.asyncio
    async def test_incomplete_binding_is_gap(self):
        """字段带非空值却归一失败（残缺）→ INCOMPLETE_BINDING（缺口，需告警）。"""
        service, _chars, _inst, _ident = _service(_detail(character_id="chr_x"))
        for value in ({"character_id": "chr_x"},          # 缺 instance_id
                      {"instance_id": "chi_x"},           # 缺 character_id
                      {"character_id": "chr_x", "instance_id": "   "}):  # 空白值
            snapshot, cause = await service.snapshot_with_cause(value)
            assert snapshot is None
            assert cause is SnapshotUnavailableCause.INCOMPLETE_BINDING, repr(value)

    @pytest.mark.asyncio
    async def test_dangling_reference_is_gap(self):
        """绑定合法但角色解析不到（软删/悬空）→ DANGLING_REFERENCE（缺口，需告警）。"""
        service, _chars, _inst, _ident = _service(None)   # characters.get → None
        snapshot, cause = await service.snapshot_with_cause(
            {"character_id": "chr_gone", "instance_id": "chi_gone"})
        assert snapshot is None
        assert cause is SnapshotUnavailableCause.DANGLING_REFERENCE

    @pytest.mark.asyncio
    async def test_ok_returns_snapshot_without_cause(self):
        service, _chars, _inst, _ident = _service(_detail(character_id="chr_ok"))
        snapshot, cause = await service.snapshot_with_cause(
            {"character_id": "chr_ok", "instance_id": "chi_ok"})
        assert cause is None
        assert snapshot is not None and snapshot.characterId == "chr_ok"

    @pytest.mark.asyncio
    async def test_snapshot_wrapper_is_backward_compatible(self):
        """原 snapshot() 语义不变（既有调用方零影响）。"""
        service, _chars, _inst, _ident = _service(_detail(character_id="chr_ok"))
        ok = await service.snapshot({"character_id": "chr_ok", "instance_id": "chi_ok"})
        assert ok is not None and ok.characterId == "chr_ok"
        assert await service.snapshot(None) is None
        assert await service.snapshot({}) is None
