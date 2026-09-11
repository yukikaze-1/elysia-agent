import pytest
from core.storage.memory_repo_v2 import MemoryRepositoryV2

# 与同目录 test_memory_repo.py（v1）同模式：单模块 + MagicMock stub（marker=unit）。
# `real_milvus_collection`：本模块断言的是**生产集合名**（v2 路由回归），必须让
# 真实库防线放过集合改名（防线默认把它重定向到 `long_term_memory_v2__test`）。
# 只用 stub，不连真实 Milvus——见 conftest.py 的 fixture 说明。
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("real_milvus_collection")]


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
    # 关键回归断言：操作必须路由到 v2 collection（非 v1 long_term_memory）。
    # 父类 MemoryRepository 方法用 self.COLLECTION，v2 子类遮蔽为 long_term_memory_v2；
    # 若父类用模块级 COLLECTION（裸名），子类 override 永不生效 → 静默写 v1。
    insert_call = db.insert.call_args[0]
    assert insert_call[0] == "long_term_memory_v2", (
        f"insert 未路由到 v2 collection, 实际={insert_call[0]!r}"
    )
    search_call = db.search.call_args[0]
    assert search_call[0] == "long_term_memory_v2", (
        f"search 未路由到 v2 collection, 实际={search_call[0]!r}"
    )
    await repo.close()
