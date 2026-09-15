"""MemoryRepository 单元测试。

**写路径 fail-closed（#31 写路径收口 4–5/5）**：`insert` / `delete` 离线时 raise
`StorageUnavailableError`，与同仓 `clear_all` 一致——此前用 `[]` / `0` 冒充业务结果，
调用方分不清"离线"与"没有条目"（静默丢写的形状）。降级语义由 `LongTermMemory` 的
两个收口点承载（记 dirty / tombstone + 落 journal），见
`core/memory/tests/test_long_term.py::TestMemoryOfflineConsistency`。

**读路径仍返回空**（`search` / `query` / `count`）：这是有意的降级——LTM 在 repo
读不到时会退回本地条目匹配（CR-05 的恢复路径 + `/health` 的 embedding_ok 探针让
"Milvus 离线"可见），整仓读路径同向（都 EMPTY_OK），故不算不对称。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.base import StorageUnavailableError
from core.storage.memory_repo import MemoryRepository


@pytest.mark.unit
class TestMemoryRepository:
    def test_insert_calls_db_insert(self):
        import asyncio
        repo = MemoryRepository(db=MagicMock())
        repo._db.insert = AsyncMock(return_value=[1, 2])  # type: ignore[reportOptionalMemberAccess]
        result = asyncio.run(repo.insert([[0.1]*1024], [{"text": "test"}]))
        assert result == [1, 2]
        repo._db.insert.assert_called_once()  # type: ignore[reportOptionalMemberAccess]

    def test_search_returns_empty_when_db_none(self):
        import asyncio
        repo = MemoryRepository(db=None)
        result = asyncio.run(repo.search([0.1]*1024, top_k=5))
        assert result == []

    def test_insert_fail_closed_when_db_none(self):
        """离线 insert 必须 raise——空列表是"后端返回空 PK"，两者语义不同。"""
        import asyncio
        repo = MemoryRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.insert([[0.1]*1024], [{"text": "test"}]))

    def test_delete_fail_closed_when_db_none(self):
        """离线 delete 必须 raise——0 是"后端没删到行"。"""
        import asyncio
        repo = MemoryRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.delete([1, 2]))

    def test_empty_pk_result_is_not_offline(self):
        """后端返回空 PK（写进但未落定）不得被当成离线——语义必须可区分。"""
        import asyncio
        repo = MemoryRepository(db=MagicMock())
        repo._db.insert = AsyncMock(return_value=[])  # type: ignore[reportOptionalMemberAccess]
        assert asyncio.run(repo.insert([[0.1]*1024], [{"text": "t"}])) == []
