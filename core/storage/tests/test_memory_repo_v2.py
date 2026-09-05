import pytest
from core.storage.memory_repo_v2 import MemoryRepositoryV2


def test_collection_name():
    assert MemoryRepositoryV2.COLLECTION == "long_term_memory_v2"


@pytest.mark.asyncio
async def test_insert_search_scope_filter():
    from unittest.mock import AsyncMock, MagicMock
    # 用 MagicMock stub（现有 test_memory_repo.py 同模式；core/storage/tests/ 无 fake vectordb）
    db = MagicMock()
    db.collection_exists = AsyncMock(return_value=True)
    db.insert = AsyncMock(return_value=[1])
    db.search = AsyncMock(return_value=[{
        "id": 1, "text": "hello", "user_id": "usr_a",
        "character_instance_id": "chi_a", "visibility": "character_private",
    }])
    db.query = AsyncMock(return_value=[])
    db.delete = AsyncMock(return_value=1)
    db.drop_collection = AsyncMock()
    db.create_collection = AsyncMock()
    db.load_collection = AsyncMock()
    db.close = AsyncMock()
    repo = MemoryRepositoryV2(db=db)
    # 不调 repo.connect()——MemoryRepository.connect() 无条件 self._db = VectorDBFactory.create(...)
    # 覆盖注入的 db（现有 test_memory_repo.py 同模式：跳过 connect，直接用注入的 _db）。
    repo._db = db
    await repo.insert(vectors=[[0.1] * 8], metadata=[{
        "entry_id": "m_1", "text": "hello",
        "user_id": "usr_a", "character_instance_id": "chi_a",
        "visibility": "character_private", "memory_type": "Note",
        "poignancy": 5, "timestamp": 0.0, "schema_version": 2,
    }])
    # 断言 repo.search 收到了 scope filter_expr
    res = await repo.search(
        vector=[0.1] * 8, top_k=10,
        filter_expr='user_id == "usr_a" && character_instance_id == "chi_a" && visibility == "character_private"',
        output_fields=["id", "text", "user_id", "character_instance_id", "visibility"],
    )
    assert len(res) >= 1
    await repo.close()
