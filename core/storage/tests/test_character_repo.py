# core/storage/tests/test_character_repo.py
"""CharacterRepository 测试（marker=service，用内存假后端）。"""
import pytest
import pytest_asyncio

from core.storage.character_repo import CharacterRepository
from core.character.definition import CharacterDefinition
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.base import StorageUnavailableError


def _def(**overrides) -> CharacterDefinition:
    base = dict(characterId="chr_custom_x", slug="x", source="custom",
                mutable=True, status="active", currentRevision=1, ownerUserId="usr_local_default")
    base.update(overrides)
    return CharacterDefinition(**base)


@pytest.mark.service
class TestCharacterRepository:
    @pytest_asyncio.fixture
    async def repo(self):
        r = CharacterRepository(db=FakeDocumentDBBackend())
        await r.connect()
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_insert_and_get(self, repo):
        await repo.insert_definition(_def())
        got = await repo.get_definition("chr_custom_x")
        assert got is not None and got.characterId == "chr_custom_x"
        assert got.currentRevision == 1

    @pytest.mark.asyncio
    async def test_cas_pointer_hit(self, repo):
        await repo.insert_definition(_def(currentRevision=1))
        ok = await repo.cas_revision_pointer("chr_custom_x", expected_revision=1, new_revision=2, updated_at="2026-09-04T00:00:00Z")
        assert ok is True
        got = await repo.get_definition("chr_custom_x")
        assert got.currentRevision == 2

    @pytest.mark.asyncio
    async def test_cas_pointer_miss_conflict(self, repo):
        await repo.insert_definition(_def(currentRevision=2))
        ok = await repo.cas_revision_pointer("chr_custom_x", expected_revision=1, new_revision=3, updated_at="2026-09-04T00:00:00Z")
        assert ok is False  # current_revision==2 != expected==1 → 未命中

    @pytest.mark.asyncio
    async def test_archive(self, repo):
        await repo.insert_definition(_def())
        await repo.archive("chr_custom_x")
        got = await repo.get_definition("chr_custom_x")
        assert got.status == "archived"

    @pytest.mark.asyncio
    async def test_owner_filter(self, repo):
        await repo.insert_definition(_def(characterId="a"))
        await repo.insert_definition(_def(characterId="b", ownerUserId="usr_other"))
        lst = await repo.list_definitions(owner_user_id="usr_local_default")
        assert [d.characterId for d in lst] == ["a"]

    @pytest.mark.asyncio
    async def test_status_filter(self, repo):
        await repo.insert_definition(_def(characterId="a", status="active"))
        await repo.insert_definition(_def(characterId="b", status="archived"))
        lst = await repo.list_definitions(status="active")
        assert [d.characterId for d in lst] == ["a"]

    @pytest.mark.asyncio
    async def test_offline_insert_raises(self):
        r = CharacterRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            await r.insert_definition(_def())


@pytest.mark.asyncio
async def test_delete_definition_removes_only_target():
    """BE-STAGEH-05：fixture cleanup 用的物理删除——只删目标 Definition。"""
    repo = CharacterRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.insert_definition(_def(characterId="chr_a"))
    await repo.insert_definition(_def(characterId="chr_b"))

    assert await repo.delete_definition("chr_a") is True
    assert await repo.get_definition("chr_a") is None
    assert await repo.get_definition("chr_b") is not None
    assert await repo.delete_definition("chr_missing") is False


@pytest.mark.asyncio
async def test_delete_definition_fails_closed_without_connection():
    r = CharacterRepository(db=None)
    with pytest.raises(StorageUnavailableError):
        await r.delete_definition("chr_a")
