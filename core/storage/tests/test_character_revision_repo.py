"""CharacterRevisionRepository 测试（marker=service，用内存假后端）。

用 core/storage/tests/_fake_documentdb.py 的 FakeDocumentDBBackend 注入 db，不触碰真实 Mongo。
"""
import pytest
import pytest_asyncio

from core.storage.character_revision_repo import CharacterRevisionRepository
from core.character.revision import CharacterRevision
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend


def _rev(character_id="chr_custom_x", revision=1) -> CharacterRevision:
    return CharacterRevision(
        characterId=character_id, revision=revision, displayName="测试",
        promptProfile={"corePersonality": "b", "speakingStyle": "s"},
    )


@pytest.mark.service
class TestCharacterRevisionRepository:
    @pytest_asyncio.fixture
    async def repo(self):
        r = CharacterRevisionRepository(db=FakeDocumentDBBackend())
        await r.connect()
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_append_and_get(self, repo):
        await repo.append_revision(_rev())
        got = await repo.get_revision("chr_custom_x", 1)
        assert got is not None and got.displayName == "测试"

    @pytest.mark.asyncio
    async def test_max_revision_no_docs_is_zero(self, repo):
        assert await repo.max_revision("chr_empty") == 0

    @pytest.mark.asyncio
    async def test_max_revision_after_append(self, repo):
        await repo.append_revision(_rev("chr_custom_x", 1))
        await repo.append_revision(_rev("chr_custom_x", 2))
        assert await repo.max_revision("chr_custom_x") == 2

    @pytest.mark.asyncio
    async def test_nested_dict_keys_preserved_camel(self, repo):
        # promptProfile 内嵌核心字符串保留 camelCase（不转 snake）
        r = CharacterRevision(characterId="chr_n", revision=1, displayName="N",
                              promptProfile={"corePersonality": "body"})
        await repo.append_revision(r)
        got = await repo.get_revision("chr_n", 1)
        assert got.promptProfile["corePersonality"] == "body"

    @pytest.mark.asyncio
    async def test_delete_revision(self, repo):
        await repo.append_revision(_rev())
        await repo.delete_revision("chr_custom_x", 1)
        assert await repo.get_revision("chr_custom_x", 1) is None

    @pytest.mark.asyncio
    async def test_offline_append_raises(self):
        r = CharacterRevisionRepository(db=None)  # 未连接
        with pytest.raises(Exception):  # StorageUnavailableError
            await r.append_revision(_rev())
