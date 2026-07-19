from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ..protocol import DocumentDBBackend
from ..registry import DocumentDBFactory


# ============================================================
# Protocol — ABC constraints
# ============================================================

@pytest.mark.service
class TestDocumentDBBackendProtocol:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            DocumentDBBackend()  # type: ignore[abstract]

    def test_missing_insert_one_raises(self):
        with pytest.raises(TypeError):

            class Bad(DocumentDBBackend):
                async def insert_many(self, c, d): return []
                async def find(self, c, f, *, sort=None, limit=100, skip=0): return []
                async def update_one(self, c, f, u): return 0
                async def delete_many(self, c, f): return 0
                async def count(self, c, f): return 0
                async def ensure_index(self, c, k, **kw): return ""
                async def close(self): pass

            Bad()  # type: ignore[abstract]

    def test_full_implementation_ok(self):
        class Good(DocumentDBBackend):
            async def insert_one(self, c, d): return ""
            async def insert_many(self, c, d): return []
            async def find(self, c, f, *, sort=None, limit=100, skip=0): return []
            async def update_one(self, c, f, u): return 0
            async def delete_many(self, c, f): return 0
            async def count(self, c, f): return 0
            async def ensure_index(self, c, k, **kw): return ""
            async def close(self): pass

        assert isinstance(Good(), DocumentDBBackend)


# ============================================================
# DocumentDBFactory
# ============================================================

@pytest.mark.service
class TestDocumentDBFactory:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid provider_id"):
            DocumentDBFactory.create("mongodb")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            DocumentDBFactory.create("unknown/db")

    def test_mongodb_parsing(self):
        with patch("motor.motor_asyncio.AsyncIOMotorClient", MagicMock()):
            backend = DocumentDBFactory.create("mongodb/elysia")
        assert backend._db is not None


# ============================================================
# MongoDBBackend
# ============================================================

def _make_insert_result(inserted_id="abc123"):
    result = MagicMock()
    result.inserted_id = inserted_id
    return result


def _make_insert_many_result(ids=None):
    if ids is None:
        ids = ["a", "b", "c"]
    result = MagicMock()
    result.inserted_ids = ids
    return result


def _setup_mock_collection(backend, collection: str):
    mock_coll = MagicMock()
    backend._db = {collection: mock_coll}
    return mock_coll


@pytest.fixture
def backend(documentdb_backend):
    return documentdb_backend


@pytest.mark.service
class TestMongoDBInsert:
    @pytest.mark.asyncio
    async def test_insert_one(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_coll.insert_one = AsyncMock(return_value=_make_insert_result("id1"))
        result = await backend.insert_one("test", {"text": "hello"})
        assert result == "id1"
        mock_coll.insert_one.assert_called_once_with({"text": "hello"})

    @pytest.mark.asyncio
    async def test_insert_many(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_coll.insert_many = AsyncMock(
            return_value=_make_insert_many_result(["x", "y"])
        )
        result = await backend.insert_many("test", [{"a": 1}, {"b": 2}])
        assert result == ["x", "y"]

    @pytest.mark.asyncio
    async def test_insert_many_empty(self, backend):
        result = await backend.insert_many("test", [])
        assert result == []


@pytest.mark.service
class TestMongoDBFind:
    @pytest.mark.asyncio
    async def test_find(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(
            return_value=[{"_id": MagicMock(), "text": "hello"}]
        )
        # MotorCursor supports chaining
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = mock_cursor
        mock_coll.find.return_value = mock_cursor

        results = await backend.find("test", {"text": "hello"}, limit=10)
        assert results[0]["text"] == "hello"
        assert results[0]["_id"] != ""
        mock_coll.find.assert_called_once_with({"text": "hello"})

    @pytest.mark.asyncio
    async def test_find_with_sort_and_skip(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = mock_cursor
        mock_coll.find.return_value = mock_cursor

        await backend.find("test", {}, sort=[("timestamp", -1)], skip=10, limit=5)
        mock_cursor.sort.assert_called_once_with([("timestamp", -1)])
        mock_cursor.skip.assert_called_once_with(10)
        mock_cursor.limit.assert_called_once_with(5)
        mock_cursor.to_list.assert_called_once_with(length=5)


@pytest.mark.service
class TestMongoDBUpdate:
    @pytest.mark.asyncio
    async def test_update_one(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        result_mock = MagicMock()
        result_mock.modified_count = 1
        mock_coll.update_one = AsyncMock(return_value=result_mock)
        result = await backend.update_one("test", {"_id": "x"}, {"$set": {"a": 1}})
        assert result == 1


@pytest.mark.service
class TestMongoDBDelete:
    @pytest.mark.asyncio
    async def test_delete_many(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        result_mock = MagicMock()
        result_mock.deleted_count = 5
        mock_coll.delete_many = AsyncMock(return_value=result_mock)
        result = await backend.delete_many("test", {"status": "old"})
        assert result == 5


@pytest.mark.service
class TestMongoDBCount:
    @pytest.mark.asyncio
    async def test_count(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_coll.count_documents = AsyncMock(return_value=42)
        result = await backend.count("test", {"type": "text"})
        assert result == 42


@pytest.mark.service
class TestMongoDBIndex:
    @pytest.mark.asyncio
    async def test_ensure_index(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_coll.create_index = AsyncMock(return_value="idx_1")
        result = await backend.ensure_index("test", [("timestamp", -1)])
        assert result == "idx_1"


@pytest.mark.service
class TestMongoDBClose:
    @pytest.mark.asyncio
    async def test_close(self, backend):
        await backend.close()  # should not raise
        backend._client.close.assert_called_once()


# ============================================================
# Protocol — remaining abstract enforcement (6 methods)
# ============================================================

@pytest.mark.service
class TestProtocolRemaining:
    def test_missing_insert_many(self):
        with pytest.raises(TypeError):
            class Bad(DocumentDBBackend):
                async def insert_one(self, c, d): return ""
                async def find(self, c, f, *, sort=None, limit=100, skip=0): return []
                async def update_one(self, c, f, u): return 0
                async def delete_many(self, c, f): return 0
                async def count(self, c, f): return 0
                async def ensure_index(self, c, k, **kw): return ""
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_find(self):
        with pytest.raises(TypeError):
            class Bad(DocumentDBBackend):
                async def insert_one(self, c, d): return ""
                async def insert_many(self, c, d): return []
                async def update_one(self, c, f, u): return 0
                async def delete_many(self, c, f): return 0
                async def count(self, c, f): return 0
                async def ensure_index(self, c, k, **kw): return ""
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_update_one(self):
        with pytest.raises(TypeError):
            class Bad(DocumentDBBackend):
                async def insert_one(self, c, d): return ""
                async def insert_many(self, c, d): return []
                async def find(self, c, f, *, sort=None, limit=100, skip=0): return []
                async def delete_many(self, c, f): return 0
                async def count(self, c, f): return 0
                async def ensure_index(self, c, k, **kw): return ""
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_delete_many(self):
        with pytest.raises(TypeError):
            class Bad(DocumentDBBackend):
                async def insert_one(self, c, d): return ""
                async def insert_many(self, c, d): return []
                async def find(self, c, f, *, sort=None, limit=100, skip=0): return []
                async def update_one(self, c, f, u): return 0
                async def count(self, c, f): return 0
                async def ensure_index(self, c, k, **kw): return ""
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_count(self):
        with pytest.raises(TypeError):
            class Bad(DocumentDBBackend):
                async def insert_one(self, c, d): return ""
                async def insert_many(self, c, d): return []
                async def find(self, c, f, *, sort=None, limit=100, skip=0): return []
                async def update_one(self, c, f, u): return 0
                async def delete_many(self, c, f): return 0
                async def ensure_index(self, c, k, **kw): return ""
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_ensure_index(self):
        with pytest.raises(TypeError):
            class Bad(DocumentDBBackend):
                async def insert_one(self, c, d): return ""
                async def insert_many(self, c, d): return []
                async def find(self, c, f, *, sort=None, limit=100, skip=0): return []
                async def update_one(self, c, f, u): return 0
                async def delete_many(self, c, f): return 0
                async def count(self, c, f): return 0
                async def close(self): pass
            Bad()  # type: ignore[abstract]


# ============================================================
# MongoDB — zero results / upsert / edge cases
# ============================================================

@pytest.mark.service
class TestMongoDBEdges:
    @pytest.mark.asyncio
    async def test_find_empty_results(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = mock_cursor
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_coll.find.return_value = mock_cursor
        result = await backend.find("test", {})
        assert result == []

    @pytest.mark.asyncio
    async def test_update_one_zero_modified(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_result = MagicMock()
        mock_result.modified_count = 0
        mock_coll.update_one = AsyncMock(return_value=mock_result)
        result = await backend.update_one("test", {}, {"$set": {"x": 1}})
        assert result == 0

    @pytest.mark.asyncio
    async def test_delete_many_zero_deleted(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_result = MagicMock()
        mock_result.deleted_count = 0
        mock_coll.delete_many = AsyncMock(return_value=mock_result)
        result = await backend.delete_many("test", {})
        assert result == 0

    @pytest.mark.asyncio
    async def test_count_returns_zero(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_coll.count_documents = AsyncMock(return_value=0)
        result = await backend.count("test", {})
        assert result == 0

    @pytest.mark.asyncio
    async def test_update_one_with_upsert(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_result = MagicMock()
        mock_result.modified_count = 1
        mock_coll.update_one = AsyncMock(return_value=mock_result)
        await backend.update_one("test", {"k": "v"}, {"$set": {"x": 1}}, upsert=True)
        mock_coll.update_one.assert_called_with(
            {"k": "v"}, {"$set": {"x": 1}}, upsert=True
        )

    @pytest.mark.asyncio
    async def test_ensure_index_with_kwargs(self, backend):
        mock_coll = _setup_mock_collection(backend, "test")
        mock_coll.create_index = AsyncMock(return_value="idx_unique")
        result = await backend.ensure_index("test", [("email", 1)], unique=True)
        assert result == "idx_unique"
        mock_coll.create_index.assert_called_with([("email", 1)], unique=True)

    @pytest.mark.asyncio
    async def test_connection_failure_propagates(self, backend):
        from pymongo.errors import ServerSelectionTimeoutError
        mock_coll = _setup_mock_collection(backend, "test")
        mock_coll.find = MagicMock(side_effect=ServerSelectionTimeoutError("timeout"))
        with pytest.raises(ServerSelectionTimeoutError):
            await backend.find("test", {})
