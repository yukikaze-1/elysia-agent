# core/character/tests/test_archive_consistency.py
"""P1：默认角色归档的持久化一致性。

修复前顺序是「先归档角色 → 再改 defaultCharacterId」——profile CAS 冲突或存储失败
会留下「角色已归档但默认角色仍指向它」的悬空引用，响应却按半成功返回。

本文件锁定：profile 重定向先于归档；失败则整体失败且角色保持 active（可恢复）；
并发 CAS 冲突有限重试；重启后仍指向可用角色。
"""

import pytest

from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog
from core.character.draft import CharacterDraft
from core.character.service import (
    BUILTIN_FALLBACK_CHARACTER_ID,
    CharacterService,
    RevisionConflictError,
)
from core.profile.profile_store import UserIdentityStore
from core.storage.base import StorageUnavailableError
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.user_identity_repo import UserIdentityRepository

pytestmark = pytest.mark.service

BODY = "你是测试角色。"


def _draft() -> CharacterDraft:
    return CharacterDraft(
        displayName="测试角色",
        promptProfile={
            "corePersonality": BODY, "relationshipStyle": BODY, "speakingStyle": BODY,
        },
    )


def _service(store: UserIdentityStore) -> CharacterService:
    return CharacterService(
        repo=CharacterRepository(db=FakeDocumentDBBackend()),
        rev_repo=CharacterRevisionRepository(db=FakeDocumentDBBackend()),
        catalog=CharacterCatalog(source=BuiltinCharacterSource()),
        user_identity_store=store,
    )


async def _make_default(svc: CharacterService, store: UserIdentityStore) -> str:
    """建一个 custom 角色并把它设为默认角色，返回 characterId。

    直接调用基类实现，绕过子类为被测路径准备的 override（冲突/不可用替身）。
    """
    await svc.repo.connect()
    await svc.rev_repo.connect()
    detail = await svc.create(_draft(), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId
    updated = await UserIdentityStore.update_if_revision(
        store, 1, {"defaultCharacterId": character_id, "initialized": True})
    assert updated is not None
    assert updated.defaultCharacterId == character_id
    return character_id


class _RecordingStore(UserIdentityStore):
    """记录重定向调用顺序。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    async def update_if_revision(self, expected_revision, updates):  # noqa: ANN001
        self.events.append(f"retarget:{expected_revision}")
        return await super().update_if_revision(expected_revision, updates)


class _ConflictOnceStore(UserIdentityStore):
    """首次 CAS 模拟并发写冲突（外部先改了一次档案），随后成功。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def update_if_revision(self, expected_revision, updates):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            await super().update_if_revision(expected_revision, {"displayName": "并发改名"})
            return None
        return await super().update_if_revision(expected_revision, updates)


class _AlwaysConflictStore(UserIdentityStore):
    """持续 CAS 冲突——重试耗尽。"""

    async def update_if_revision(self, expected_revision, updates):  # noqa: ANN001
        return None


class _DownStore(UserIdentityStore):
    """存储不可用（fail-closed）。"""

    async def update_if_revision(self, expected_revision, updates):  # noqa: ANN001
        raise StorageUnavailableError("identity store down")


@pytest.mark.asyncio
async def test_archive_retargets_default_before_archiving():
    """顺序证据：先改 defaultCharacterId，再归档角色。"""
    store = _RecordingStore()
    store.bootstrap("妖梦")
    svc = _service(store)
    character_id = await _make_default(svc, store)

    original_archive = svc.repo.archive

    async def _spy(archived_id: str) -> None:
        store.events.append("archive")
        await original_archive(archived_id)

    svc.repo.archive = _spy  # type: ignore[method-assign]

    result = await svc.archive(character_id)

    assert store.events == ["retarget:2", "archive"]
    assert result.userProfile is not None
    assert result.userProfile.defaultCharacterId == BUILTIN_FALLBACK_CHARACTER_ID
    assert result.character.status.value == "archived"


@pytest.mark.asyncio
async def test_archive_fails_closed_when_profile_storage_unavailable():
    """profile 存储失败 → 整体失败：角色保持 active，不留悬空默认角色。"""
    store = _DownStore()
    store.bootstrap("妖梦")
    svc = _service(store)
    character_id = await _make_default(svc, store)

    with pytest.raises(StorageUnavailableError):
        await svc.archive(character_id)

    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.status == "active"
    profile = store.get()
    assert profile is not None and profile.defaultCharacterId == character_id


@pytest.mark.asyncio
async def test_archive_retries_cas_conflict_then_succeeds():
    """并发改档案导致 CAS 冲突 → 重读 revision 后重试成功。"""
    store = _ConflictOnceStore()
    store.bootstrap("妖梦")
    svc = _service(store)
    character_id = await _make_default(svc, store)

    result = await svc.archive(character_id)

    assert store.calls == 2
    assert result.userProfile is not None
    assert result.userProfile.defaultCharacterId == BUILTIN_FALLBACK_CHARACTER_ID
    assert result.userProfile.revision == 4          # 并发改名 + 重定向各推进一次
    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.status == "archived"


@pytest.mark.asyncio
async def test_archive_fails_after_retry_exhaustion():
    """持续冲突 → 409 语义，且角色未归档（可恢复）。"""
    store = _AlwaysConflictStore()
    store.bootstrap("妖梦")
    svc = _service(store)
    character_id = await _make_default(svc, store)

    with pytest.raises(RevisionConflictError):
        await svc.archive(character_id)

    definition = await svc.repo.get_definition(character_id)
    assert definition is not None and definition.status == "active"


@pytest.mark.asyncio
async def test_archive_default_retarget_survives_restart():
    """Mongo 权威：归档后的默认角色回退在「重启」（新 store）后仍成立。"""
    db = FakeDocumentDBBackend()
    store = UserIdentityStore(repo=UserIdentityRepository(db=db))
    await store.connect(user_name="妖梦")
    svc = _service(store)
    await svc.repo.connect()
    await svc.rev_repo.connect()
    detail = await svc.create(_draft(), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId
    await store.update_if_revision(
        1, {"defaultCharacterId": character_id, "initialized": True})

    await svc.archive(character_id)

    restarted = UserIdentityStore(repo=UserIdentityRepository(db=db))
    loaded = await restarted.connect(user_name="启动默认名")
    assert loaded is not None
    assert loaded.defaultCharacterId == BUILTIN_FALLBACK_CHARACTER_ID


@pytest.mark.asyncio
async def test_archive_non_default_character_leaves_profile_untouched():
    """归档的不是默认角色 → 不动 profile，响应 userProfile 为 None。"""
    store = _RecordingStore()
    store.bootstrap("妖梦")
    svc = _service(store)
    await svc.repo.connect()
    await svc.rev_repo.connect()
    detail = await svc.create(_draft(), owner_user_id="usr_local_default")

    result = await svc.archive(detail.summary.characterId)

    assert store.events == []
    assert result.userProfile is None
    profile = store.get()
    assert profile is not None and profile.defaultCharacterId is None
