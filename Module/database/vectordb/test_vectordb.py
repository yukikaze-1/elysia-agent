from unittest.mock import MagicMock, patch

import pytest

from typing import cast

from .milvus import MilvusBackend
from .protocol import VectorDBBackend
from .registry import VectorDBFactory


# ============================================================
# Protocol — ABC constraints
# ============================================================

class TestVectorDBBackendProtocol:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            VectorDBBackend()  # type: ignore[abstract]

    def test_missing_create_collection_raises(self):
        with pytest.raises(TypeError):

            class Bad(VectorDBBackend):
                async def drop_collection(self, name): pass
                async def collection_exists(self, name) -> bool: return False
                async def insert(self, c, v, m): return []
                async def search(self, c, v, *, top_k=10, filter_expr="", output_fields=None): return []
                async def query(self, c, *, filter_expr, output_fields=None, limit=10000): return []
                async def close(self): pass

            Bad()  # type: ignore[abstract]

    def test_missing_insert_raises(self):
        with pytest.raises(TypeError):

            class Bad(VectorDBBackend):
                async def create_collection(self, name, dim, **kw): pass
                async def drop_collection(self, name): pass
                async def collection_exists(self, name) -> bool: return False
                async def search(self, c, v, *, top_k=10, filter_expr="", output_fields=None): return []
                async def query(self, c, *, filter_expr, output_fields=None, limit=10000): return []
                async def close(self): pass

            Bad()  # type: ignore[abstract]

    def test_full_implementation_ok(self):
        class Good(VectorDBBackend):
            async def create_collection(self, name, dim, **kw): pass
            async def drop_collection(self, name): pass
            async def collection_exists(self, name) -> bool: return False
            async def insert(self, c, v, m): return []
            async def search(self, c, v, *, top_k=10, filter_expr="", output_fields=None): return []
            async def query(self, c, *, filter_expr, output_fields=None, limit=10000): return []
            async def close(self): pass

        assert isinstance(Good(), VectorDBBackend)


# ============================================================
# VectorDBFactory
# ============================================================

class TestVectorDBFactory:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid provider_id"):
            VectorDBFactory.create("milvus")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            VectorDBFactory.create("unknown/name")

    def test_pinecone_placeholder_raises(self):
        with pytest.raises(NotImplementedError):
            VectorDBFactory.create("pinecone/index")

    def test_qdrant_placeholder_raises(self):
        with pytest.raises(NotImplementedError):
            VectorDBFactory.create("qdrant/collection")

    def test_milvus_parsing(self):
        with patch("Module.database.vectordb.milvus.MilvusClient", MagicMock()):
            backend = VectorDBFactory.create("milvus/micro_memory")
        backend = cast(MilvusBackend, backend)
        assert backend._name == "micro_memory"

    def test_custom_uri(self):
        with patch("Module.database.vectordb.milvus.MilvusClient", MagicMock()) as mock_client:
            VectorDBFactory.create("milvus/test", base_url="http://192.168.1.1:19530")
        call_kwargs = mock_client.call_args.kwargs
        assert call_kwargs["uri"] == "http://192.168.1.1:19530"


# ============================================================
# MilvusBackend
# ============================================================

@pytest.fixture
def backend():
    with patch("Module.database.vectordb.milvus.MilvusClient", MagicMock()):
        yield VectorDBFactory.create("milvus/test")


class TestMilvusCollection:
    @pytest.mark.asyncio
    async def test_create_collection(self, backend):
        backend._client.has_collection.return_value = False
        await backend.create_collection("test_col", dimension=1024)
        backend._client.has_collection.assert_called_once()
        backend._client.create_schema.assert_called_once()
        backend._client.create_collection.assert_called_once()
        backend._client.create_index.assert_called_once()
        backend._client.load_collection.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_collection_exists_without_replace_raises(self, backend):
        backend._client.has_collection.return_value = True
        with pytest.raises(ValueError, match="already exists"):
            await backend.create_collection("test_col", dimension=1024)

    @pytest.mark.asyncio
    async def test_create_collection_replace(self, backend):
        backend._client.has_collection.return_value = True
        await backend.create_collection("test_col", dimension=1024, replace=True)
        backend._client.drop_collection.assert_called_once()
        backend._client.create_collection.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_index_preset_raises(self, backend):
        backend._client.has_collection.return_value = False
        with pytest.raises(ValueError, match="Unknown index_preset"):
            await backend.create_collection("test_col", dimension=1024, index_preset="nonexistent")

    @pytest.mark.asyncio
    async def test_drop_collection(self, backend):
        await backend.drop_collection("test_col")
        backend._client.drop_collection.assert_called_with("test_col")

    @pytest.mark.asyncio
    async def test_collection_exists(self, backend):
        backend._client.has_collection.return_value = True
        assert await backend.collection_exists("test") is True


class TestMilvusInsert:
    @pytest.mark.asyncio
    async def test_insert(self, backend):
        backend._client.insert.return_value = {"ids": [1, 2]}
        ids = await backend.insert(
            "col",
            vectors=[[0.1, 0.2], [0.3, 0.4]],
            metadata=[{"text": "a"}, {"text": "b"}],
        )
        assert ids == [1, 2]
        call_args = backend._client.insert.call_args
        assert call_args.args[0] == "col"
        data = call_args.args[1]
        assert len(data) == 2
        assert data[0]["embedding"] == [0.1, 0.2]
        assert data[0]["text"] == "a"


class TestMilvusSearch:
    @pytest.mark.asyncio
    async def test_search(self, backend):
        hit = {"id": 1, "entity": {"text": "hello"}, "distance": 0.1}
        backend._client.search.return_value = [[hit]]
        results = await backend.search(
            "col", vector=[0.1, 0.2], top_k=5, output_fields=["text"]
        )
        assert results == [hit]

    @pytest.mark.asyncio
    async def test_search_with_filter(self, backend):
        backend._client.search.return_value = [[]]
        await backend.search(
            "col", vector=[0.1], top_k=3, filter_expr='type == "micro"'
        )
        call_kwargs = backend._client.search.call_args.kwargs
        assert call_kwargs["filter"] == 'type == "micro"'


class TestMilvusQuery:
    @pytest.mark.asyncio
    async def test_query(self, backend):
        backend._client.query.return_value = [{"id": 1, "text": "hi"}]
        results = await backend.query("col", filter_expr='type == "macro"')
        assert results == [{"id": 1, "text": "hi"}]

    @pytest.mark.asyncio
    async def test_query_with_limit_and_fields(self, backend):
        backend._client.query.return_value = []
        await backend.query(
            "col", filter_expr="id > 0", output_fields=["text"], limit=100
        )
        call_kwargs = backend._client.query.call_args.kwargs
        assert call_kwargs["output_fields"] == ["text"]
        assert call_kwargs["limit"] == 100


class TestMilvusClose:
    @pytest.mark.asyncio
    async def test_close(self, backend):
        await backend.close()  # should not raise


# ============================================================
# Protocol — remaining abstract enforcement (5 methods)
# ============================================================

class TestProtocolRemaining:
    def test_missing_drop_collection(self):
        with pytest.raises(TypeError):
            class Bad(VectorDBBackend):
                async def create_collection(self, n, d, **kw): pass
                async def collection_exists(self, n) -> bool: return False
                async def insert(self, c, v, m): return []
                async def search(self, c, v, *, top_k=10, filter_expr="", output_fields=None): return []
                async def query(self, c, *, filter_expr, output_fields=None, limit=10000): return []
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_collection_exists(self):
        with pytest.raises(TypeError):
            class Bad(VectorDBBackend):
                async def create_collection(self, n, d, **kw): pass
                async def drop_collection(self, n): pass
                async def insert(self, c, v, m): return []
                async def search(self, c, v, *, top_k=10, filter_expr="", output_fields=None): return []
                async def query(self, c, *, filter_expr, output_fields=None, limit=10000): return []
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_search(self):
        with pytest.raises(TypeError):
            class Bad(VectorDBBackend):
                async def create_collection(self, n, d, **kw): pass
                async def drop_collection(self, n): pass
                async def collection_exists(self, n) -> bool: return False
                async def insert(self, c, v, m): return []
                async def query(self, c, *, filter_expr, output_fields=None, limit=10000): return []
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_query(self):
        with pytest.raises(TypeError):
            class Bad(VectorDBBackend):
                async def create_collection(self, n, d, **kw): pass
                async def drop_collection(self, n): pass
                async def collection_exists(self, n) -> bool: return False
                async def insert(self, c, v, m): return []
                async def search(self, c, v, *, top_k=10, filter_expr="", output_fields=None): return []
                async def close(self): pass
            Bad()  # type: ignore[abstract]

    def test_missing_close(self):
        with pytest.raises(TypeError):
            class Bad(VectorDBBackend):
                async def create_collection(self, n, d, **kw): pass
                async def drop_collection(self, n): pass
                async def collection_exists(self, n) -> bool: return False
                async def insert(self, c, v, m): return []
                async def search(self, c, v, *, top_k=10, filter_expr="", output_fields=None): return []
                async def query(self, c, *, filter_expr, output_fields=None, limit=10000): return []
            Bad()  # type: ignore[abstract]


# ============================================================
# index_preset / metric_type
# ============================================================

class TestIndexPreset:
    @pytest.mark.asyncio
    async def test_default_metric_type_is_l2(self, backend):
        from .milvus import INDEX_PRESETS
        preset = INDEX_PRESETS["default"]
        assert preset["metric_type"] == "L2"

    @pytest.mark.asyncio
    async def test_cosine_preset(self):
        from .milvus import INDEX_PRESETS
        preset = INDEX_PRESETS["cosine"]
        assert preset["metric_type"] == "COSINE"

    @pytest.mark.asyncio
    async def test_ip_preset(self):
        from .milvus import INDEX_PRESETS
        preset = INDEX_PRESETS["ip"]
        assert preset["metric_type"] == "IP"

    @pytest.mark.asyncio
    async def test_hnsw_l2_preset(self):
        from .milvus import INDEX_PRESETS
        preset = INDEX_PRESETS["hnsw_l2"]
        assert preset["index_type"] == "HNSW"

    @pytest.mark.asyncio
    async def test_unknown_preset_raises(self):
        """未知 index_preset 抛 ValueError。"""
        from .milvus import INDEX_PRESETS
        assert "nonexistent" not in INDEX_PRESETS


# ============================================================
# Connection failure
# ============================================================

class TestMilvusConnectionFailure:
    @pytest.mark.asyncio
    async def test_search_connection_error_propagates(self, backend):
        with patch.object(backend, "search", side_effect=ConnectionError("milvus down")):
            with pytest.raises(ConnectionError):
                await backend.search("col", [0.1] * 128, top_k=5)
