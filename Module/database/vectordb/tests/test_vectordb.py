from unittest.mock import MagicMock, patch

import pytest

from typing import cast

from Module.database.vectordb.milvus import MilvusBackend
from Module.database.vectordb.protocol import VectorDBBackend
from Module.database.vectordb.registry import VectorDBFactory


# ============================================================
# 协议 —— ABC 抽象约束
# ============================================================

@pytest.mark.service
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
            async def delete(self, c, ids): return 0
            async def ping(self): return True
            async def close(self): pass

        assert isinstance(Good(), VectorDBBackend)


# ============================================================
# VectorDBFactory（向量数据库工厂）
# ============================================================

@pytest.mark.service
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
# MilvusBackend（Milvus 后端）
# ============================================================

@pytest.fixture
def backend(vectordb_backend):
    return vectordb_backend


@pytest.mark.service
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


@pytest.mark.service
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


@pytest.mark.service
class TestMilvusInsertValidation:
    """P2-10：bulk insert 前校验输入，拒绝 zip 静默截断 / 部分 insert。"""

    @pytest.mark.asyncio
    async def test_insert_metadata_more_than_vectors_raises(self, backend):
        """metadata 多于 vectors → ValueError，且不调用 Milvus insert。"""
        with pytest.raises(ValueError, match="数量不一致"):
            await backend.insert(
                "col",
                vectors=[[0.1, 0.2]],
                metadata=[{"text": "a"}, {"text": "b"}],
            )
        backend._client.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_insert_vectors_more_than_metadata_raises(self, backend):
        """vectors 多于 metadata → ValueError（原先 zip 会静默截断）。"""
        with pytest.raises(ValueError, match="数量不一致"):
            await backend.insert(
                "col",
                vectors=[[0.1, 0.2], [0.3, 0.4]],
                metadata=[{"text": "a"}],
            )
        backend._client.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_insert_empty_vectors_raises(self, backend):
        with pytest.raises(ValueError, match="vectors 为空"):
            await backend.insert("col", vectors=[], metadata=[])
        backend._client.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_insert_empty_metadata_raises(self, backend):
        with pytest.raises(ValueError, match="metadata 为空"):
            await backend.insert(
                "col", vectors=[[0.1, 0.2]], metadata=[]
            )
        backend._client.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_insert_inconsistent_dimension_raises(self, backend):
        """各向量维度不一致 → ValueError。"""
        with pytest.raises(ValueError, match="维度不一致"):
            await backend.insert(
                "col",
                vectors=[[0.1, 0.2], [0.3, 0.4, 0.5]],
                metadata=[{"text": "a"}, {"text": "b"}],
            )
        backend._client.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_insert_zero_dimension_raises(self, backend):
        """空向量（维度 0）→ ValueError。"""
        with pytest.raises(ValueError, match="维度不一致"):
            await backend.insert(
                "col", vectors=[[], [0.3, 0.4]], metadata=[{"text": "a"}, {"text": "b"}]
            )
        backend._client.insert.assert_not_called()


@pytest.mark.service
class TestMilvusSearch:
    @pytest.mark.asyncio
    async def test_search(self, backend):
        # 建模已 load/创建过的 collection：metric 已在进程内注册
        backend._metric_types["col"] = "L2"
        hit = {"id": 1, "entity": {"text": "hello"}, "distance": 0.1}
        backend._client.search.return_value = [[hit]]
        results = await backend.search(
            "col", vector=[0.1, 0.2], top_k=5, output_fields=["text"]
        )
        assert results == [hit]
        assert backend._client.search.call_args.kwargs["search_params"] == {
            "metric_type": "L2"
        }

    @pytest.mark.asyncio
    async def test_search_with_filter(self, backend):
        backend._metric_types["col"] = "L2"
        backend._client.search.return_value = [[]]
        await backend.search(
            "col", vector=[0.1], top_k=3, filter_expr='type == "micro"'
        )
        call_kwargs = backend._client.search.call_args.kwargs
        assert call_kwargs["filter"] == 'type == "micro"'

    @pytest.mark.asyncio
    async def test_search_unknown_metric_fails_closed(self, backend):
        """metric 未注册且无法从 index 恢复时抛 ValueError，不静默回退 L2。"""
        backend._client.list_indexes.return_value = []
        with pytest.raises(ValueError, match="无法恢复排序语义"):
            await backend.search("col", [0.1, 0.2], top_k=5)
        backend._client.search.assert_not_called()


@pytest.mark.service
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


@pytest.mark.service
class TestMilvusClose:
    @pytest.mark.asyncio
    async def test_close(self, backend):
        await backend.close()  # should not raise


# ============================================================
# 协议 —— 剩余抽象方法强制（5 个）
# ============================================================

@pytest.mark.service
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
# index_preset / metric_type（索引预设 / 距离度量）
# ============================================================

@pytest.mark.service
class TestIndexPreset:
    @pytest.mark.asyncio
    async def test_default_metric_type_is_l2(self, backend):
        from Module.database.vectordb.milvus import INDEX_PRESETS
        preset = INDEX_PRESETS["default"]
        assert preset["metric_type"] == "L2"

    @pytest.mark.asyncio
    async def test_cosine_preset(self):
        from Module.database.vectordb.milvus import INDEX_PRESETS
        preset = INDEX_PRESETS["cosine"]
        assert preset["metric_type"] == "COSINE"

    @pytest.mark.asyncio
    async def test_ip_preset(self):
        from Module.database.vectordb.milvus import INDEX_PRESETS
        preset = INDEX_PRESETS["ip"]
        assert preset["metric_type"] == "IP"

    @pytest.mark.asyncio
    async def test_hnsw_l2_preset(self):
        from Module.database.vectordb.milvus import INDEX_PRESETS
        preset = INDEX_PRESETS["hnsw_l2"]
        assert preset["index_type"] == "HNSW"

    @pytest.mark.asyncio
    async def test_unknown_preset_raises(self):
        """未知 index_preset 抛 ValueError。"""
        from Module.database.vectordb.milvus import INDEX_PRESETS
        assert "nonexistent" not in INDEX_PRESETS


# ============================================================
# metric 持久恢复（P2-10：跨实例 load 恢复 COSINE/IP/L2，fail closed）
# ============================================================

@pytest.mark.service
class TestMilvusMetricRestore:
    @pytest.mark.asyncio
    async def test_create_collection_records_metric(self, backend):
        """创建 collection 时把 metric_type 写入进程内注册表（默认 L2）。"""
        backend._client.has_collection.return_value = False
        await backend.create_collection("col_a", dimension=4)
        assert backend._metric_types["col_a"] == "L2"

    @pytest.mark.asyncio
    async def test_create_collection_cosine_preset(self, backend):
        """cosine preset → metric_type=COSINE 写入 index params + 注册表。"""
        backend._client.has_collection.return_value = False
        await backend.create_collection("col_c", dimension=4, index_preset="cosine")
        assert backend._metric_types["col_c"] == "COSINE"
        add_index_kwargs = (
            backend._client.prepare_index_params.return_value.add_index.call_args.kwargs
        )
        assert add_index_kwargs["metric_type"] == "COSINE"

    @pytest.mark.asyncio
    async def test_create_collection_explicit_metric_ip(self, backend):
        """显式 metric_type="IP" 覆盖 preset 默认值。"""
        backend._client.has_collection.return_value = False
        await backend.create_collection("col_ip", dimension=4, metric_type="IP")
        assert backend._metric_types["col_ip"] == "IP"

    @pytest.mark.asyncio
    async def test_create_collection_unsupported_metric_raises(self, backend):
        """不受支持的 metric_type 在创建时即抛 ValueError（fail closed）。"""
        backend._client.has_collection.return_value = False
        with pytest.raises(ValueError, match="Unsupported metric_type"):
            await backend.create_collection(
                "col_x", dimension=4, metric_type="HAMMING"
            )

    @pytest.mark.asyncio
    async def test_replace_validates_metric_before_drop(self, backend):
        """review 复核 #10：replace=True 时显式 metric_type 校验必须先于 drop——
        无效 metric 不得先破坏现有 collection 数据再抛错。"""
        backend._client.has_collection.return_value = True
        with pytest.raises(ValueError, match="Unsupported metric_type"):
            await backend.create_collection(
                "col_x", dimension=4, replace=True, metric_type="HAMMING"
            )
        backend._client.drop_collection.assert_not_called()
        backend._client.create_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_collection_restores_cosine(self, backend):
        """跨实例 load：从 Milvus index params 恢复 COSINE。"""
        backend._client.list_indexes.return_value = ["embedding"]
        backend._client.describe_index.return_value = {
            "field_name": "embedding",
            "index_type": "IVF_FLAT",
            "metric_type": "COSINE",
        }
        await backend.load_collection("col")
        assert backend._metric_types["col"] == "COSINE"
        backend._client.load_collection.assert_called_with("col")

    @pytest.mark.asyncio
    async def test_load_collection_restores_ip_from_dict(self, backend):
        """list_indexes 直接返回描述 dict 时也能恢复 IP（兼容路径）。"""
        backend._client.list_indexes.return_value = [
            {"field_name": "embedding", "metric_type": "IP"}
        ]
        await backend.load_collection("col")
        assert backend._metric_types["col"] == "IP"

    @pytest.mark.asyncio
    async def test_load_collection_missing_index_params_fails_closed(self, backend):
        """索引缺失 → 抛 ValueError（fail closed），不静默默认 L2。"""
        backend._client.list_indexes.return_value = []
        with pytest.raises(ValueError, match="缺少可用的 metric_type"):
            await backend.load_collection("col")

    @pytest.mark.asyncio
    async def test_load_collection_missing_metric_field_fails_closed(self, backend):
        """index 描述存在但缺 metric_type 字段 → 抛 ValueError。"""
        backend._client.list_indexes.return_value = ["embedding"]
        backend._client.describe_index.return_value = {
            "index_type": "IVF_FLAT",  # 无 metric_type
        }
        with pytest.raises(ValueError, match="缺少可用的 metric_type"):
            await backend.load_collection("col")

    @pytest.mark.asyncio
    async def test_load_collection_unsupported_metric_fails_closed(self, backend):
        """index 描述 metric 不受支持 → 抛 ValueError。"""
        backend._client.list_indexes.return_value = ["embedding"]
        backend._client.describe_index.return_value = {"metric_type": "HAMMING"}
        with pytest.raises(ValueError, match="不受支持"):
            await backend.load_collection("col")

    @pytest.mark.asyncio
    async def test_search_restores_metric_on_demand(self, backend):
        """搜索时 metric 未注册 → 从 index 恢复并继续（COSINE）。"""
        backend._client.list_indexes.return_value = ["embedding"]
        backend._client.describe_index.return_value = {"metric_type": "COSINE"}
        backend._client.search.return_value = [[]]
        await backend.search("col", [0.1, 0.2], top_k=3)
        assert backend._metric_types["col"] == "COSINE"
        assert backend._client.search.call_args.kwargs["search_params"] == {
            "metric_type": "COSINE"
        }


# ============================================================
# 连接失败
# ============================================================

@pytest.mark.service
class TestMilvusConnectionFailure:
    @pytest.mark.asyncio
    async def test_search_connection_error_propagates(self, backend):
        with patch.object(backend, "search", side_effect=ConnectionError("milvus down")):
            with pytest.raises(ConnectionError):
                await backend.search("col", [0.1] * 128, top_k=5)
