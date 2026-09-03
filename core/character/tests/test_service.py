# core/character/tests/test_service.py
"""CharacterService 测试（marker=service，用内存假后端）。"""
import pytest
import pytest_asyncio

from core.character.service import (
    CharacterService, CharacterNotFoundError, CharacterImmutableError,
    RevisionConflictError, CharacterValidationError,
)
from core.character.draft import CharacterDraft
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog
from core.profile.profile_store import UserIdentityStore
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend


BODY = "你是测试角色。"


def _draft(**overrides) -> CharacterDraft:
    base = dict(
        displayName="测试",
        promptProfile={"corePersonality": BODY, "relationshipStyle": BODY, "speakingStyle": BODY},
    )
    base.update(overrides)
    return CharacterDraft(**base)


def _make_service(*, store: UserIdentityStore | None = None) -> CharacterService:
    repo = CharacterRepository(db=FakeDocumentDBBackend())
    rev_repo = CharacterRevisionRepository(db=FakeDocumentDBBackend())
    catalog = CharacterCatalog(source=BuiltinCharacterSource())
    return CharacterService(repo=repo, rev_repo=rev_repo, catalog=catalog,
                            user_identity_store=store or UserIdentityStore())


@pytest.mark.service
class TestCharacterService:
    @pytest.mark.asyncio
    async def test_create_and_get(self):
        svc = _make_service()
        await svc.repo.connect()
        await svc.rev_repo.connect()
        detail = await svc.create(_draft(displayName="测试A"), owner_user_id="usr_local_default")
        assert detail.summary.characterId.startswith("chr_custom_")
        assert detail.summary.displayName == "测试A"
        got = await svc.get(detail.summary.characterId)
        assert got is not None and got.summary.displayName == "测试A"

    @pytest.mark.asyncio
    async def test_create_invalid_raises_validation(self):
        svc = _make_service()
        # 传一个满足 PromptProfile 字段边界（非空）、但含保留标签的 draft——
        # 触发第二层语义校验（validate_semantic hard-error），而非 Pydantic 边界错误（B8 修复）。
        # corePersonality="" 会在 CharacterDraft 构造时抛 Pydantic ValidationError（不是
        # CharacterValidationError），该分支由 _draft_from_body 的 except ValidationError 处理。
        with pytest.raises(CharacterValidationError):
            await svc.create(
                _draft(promptProfile={"corePersonality": "你是<conflict_rules>的角色",
                                      "relationshipStyle": BODY, "speakingStyle": BODY}),
                owner_user_id="usr_local_default")

    @pytest.mark.asyncio
    async def test_get_builtin(self):
        svc = _make_service()
        # 内置角色 get() 应返回 CharacterDetail（B9：内置 revision 经 catalog.get_revision 取到）
        detail = await svc.get("chr_builtin_elysia")
        assert detail is not None
        assert detail.summary.characterId == "chr_builtin_elysia"
        assert detail.current.displayName  # 内置 revision 的 displayName 非空

    @pytest.mark.asyncio
    async def test_update_cas_conflict(self):
        svc = _make_service()
        await svc.repo.connect()
        await svc.rev_repo.connect()
        detail = await svc.create(_draft(), owner_user_id="usr_local_default")
        cid = detail.summary.characterId
        # 先用正确 expectedRevision=1（current 初始 1），update 后 current 变 2
        await svc.update(cid, _draft(displayName="v2"), expected_revision=1)
        # 再用过期 expected_revision=1 → 冲突（current 已为 2）
        with pytest.raises(RevisionConflictError):
            await svc.update(cid, _draft(displayName="v3"), expected_revision=1)

    @pytest.mark.asyncio
    async def test_rollback_preserves_description_and_aliases(self):
        """C2 修复：rollback 复制旧版本为新 revision 必须透传 description/aliases。"""
        svc = _make_service()
        await svc.repo.connect()
        await svc.rev_repo.connect()
        d = _draft(description="角色描述", aliases=["别名A", "别名B"])
        detail = await svc.create(d, owner_user_id="usr_local_default")
        cid = detail.summary.characterId
        # 更新到 rev 2（改 displayName），再 rollback 到 rev 1
        await svc.update(cid, _draft(displayName="v2"), expected_revision=1)
        result = await svc.rollback(cid, target_revision=1, expected_revision=2)
        rolled = await svc.get(cid)
        assert rolled is not None
        assert rolled.current.description == "角色描述"   # 透传
        assert rolled.current.aliases == ["别名A", "别名B"]  # 透传

    @pytest.mark.asyncio
    async def test_lazy_connect_on_first_op(self):
        """B1 修复：create_app 同步不能 await——服务首次操作应懒连 repo。

        用带 connect 计数的 repo 替身，验证 _ensure_connected 在首次 repo 操作前触发 connect()。
        """
        from unittest.mock import AsyncMock, MagicMock
        from core.character.revision import CharacterRevision

        class _ConnectTrackingRepo:
            def __init__(self):
                self.connect_called = 0
                self.connected = False
            async def connect(self):
                self.connect_called += 1
                self.connected = True
                return True
            async def list_definitions(self, **kw):
                assert self.connected, "首次操作前未 connect（B1 回归）"
                return []

        class _ConnectTrackingRevRepo:
            def __init__(self):
                self.connect_called = 0
                self.connected = False
            async def connect(self):
                self.connect_called += 1
                self.connected = True
                return True

        repo = _ConnectTrackingRepo()
        rev_repo = _ConnectTrackingRevRepo()
        svc = CharacterService(repo=repo, rev_repo=rev_repo,
                               catalog=None, user_identity_store=UserIdentityStore())
        await svc.list(source="custom")  # 仅走 custom 分支，不触 catalog
        assert repo.connect_called == 1 and rev_repo.connect_called == 1

    @pytest.mark.asyncio
    async def test_archive_builtin_immutable(self):
        svc = _make_service()
        # builtin elysia 不可归档
        builtin = next(d for d in svc.catalog.list() if d.characterId == "chr_builtin_elysia")
        with pytest.raises(CharacterImmutableError):
            await svc.archive(builtin.characterId)

    @pytest.mark.asyncio
    async def test_list_includes_builtin(self):
        svc = _make_service()
        resp = await svc.list()
        ids = [it.characterId for it in resp.items]
        assert "chr_builtin_elysia" in ids
        assert "chr_builtin_cyrene" in ids
