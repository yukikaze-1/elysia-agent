"""Milvus 向量数据库后端 — pymilvus 实现。

同步 pymilvus API 通过 asyncio.to_thread() 包装为异步。
"""

import asyncio
import logging

from pymilvus import DataType, MilvusClient

from .protocol import VectorDBBackend

# 索引预设配置 — create_collection 时通过 index_preset 选择
INDEX_PRESETS: dict[str, dict] = {
    "default": {"index_type": "IVF_FLAT", "metric_type": "L2", "params": {"nlist": 1024}},
    "cosine": {"index_type": "IVF_FLAT", "metric_type": "COSINE", "params": {"nlist": 1024}},
    "ip":      {"index_type": "IVF_FLAT", "metric_type": "IP", "params": {"nlist": 1024}},
    "hnsw_l2": {"index_type": "HNSW", "metric_type": "L2", "params": {"M": 16, "efConstruction": 200}},
}


class MilvusBackend(VectorDBBackend):
    """Milvus 向量数据库后端。

    create_collection 默认不替换已有 collection（防止误删数据）；
    传 replace=True 可先删后建。index_preset 控制索引类型和度量方式。
    search 使用的 metric_type 与最近一次 create_collection 保持一致。
    """

    def __init__(
        self,
        *,
        uri: str,
        token: str = "",
        name: str = "default",
        logger: logging.Logger | None = None,
    ):
        """初始化 Milvus 后端。

        Args:
            uri: Milvus 服务地址（如 http://localhost:19530）。
            token: Milvus 认证 token，默认空串表示无认证。
            name: 后端实例名称，默认 "default"。
            logger: 日志记录器，未提供时自动创建。
        """
        self._name = name
        self._metric_types: dict[str, str] = {}  # collection_name → metric_type
        self._logger = logger or logging.getLogger(__name__)
        kwargs: dict = {"uri": uri}
        if token:
            kwargs["token"] = token
        self._client = MilvusClient(**kwargs)

    # ---- Collection lifecycle ----

    async def create_collection(
        self,
        name: str,
        dimension: int,
        *,
        replace: bool = False,
        index_preset: str = "default",
        metric_type: str | None = None,
        **schema_kwargs,
    ) -> None:
        preset = INDEX_PRESETS.get(index_preset)
        if preset is None:
            raise ValueError(
                f"Unknown index_preset '{index_preset}'. "
                f"Known: {', '.join(sorted(INDEX_PRESETS))}"
            )

        exists = await asyncio.to_thread(self._client.has_collection, name)
        if exists:
            if not replace:
                raise ValueError(
                    f"Collection '{name}' already exists. "
                    f"Pass replace=True to drop and recreate."
                )
            await asyncio.to_thread(self._client.drop_collection, name)

        # Schema: id (auto) + embedding (float vector)
        schema = self._client.create_schema(
            auto_id=True, enable_dynamic_field=True
        )
        schema.add_field(
            field_name="id", datatype=DataType.INT64, is_primary=True
        )
        schema.add_field(
            field_name="embedding", datatype=DataType.FLOAT_VECTOR, dim=dimension
        )
        await asyncio.to_thread(
            self._client.create_collection, name, schema=schema
        )

        self._metric_types[name] = metric_type or preset["metric_type"]

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type=preset["index_type"],
            metric_type=self._metric_types[name],
            params=preset["params"],
        )
        await asyncio.to_thread(
            self._client.create_index, name, index_params
        )
        await asyncio.to_thread(self._client.load_collection, name)

    async def drop_collection(self, name: str) -> None:
        await asyncio.to_thread(self._client.drop_collection, name)

    async def load_collection(self, name: str) -> None:
        await asyncio.to_thread(self._client.load_collection, name)

    async def collection_exists(self, name: str) -> bool:
        return await asyncio.to_thread(self._client.has_collection, name)

    # ---- Write ----

    async def insert(
        self,
        collection: str,
        vectors: list[list[float]],
        metadata: list[dict],
    ) -> list[int]:
        """批量插入。vectors 和 metadata 按索引一一对应，合并为 Milvus row。"""
        data = []
        for vec, meta in zip(vectors, metadata):
            row = {"embedding": vec, **meta}
            data.append(row)
        result = await asyncio.to_thread(
            self._client.insert, collection, data
        )
        return result["ids"]

    # ---- Read ----

    async def search(
        self,
        collection: str,
        vector: list[float],
        *,
        top_k: int = 10,
        filter_expr: str = "",
        output_fields: list[str] | None = None,
    ) -> list[dict]:
        """ANN 搜索。使用 collection 创建时设定的 metric_type。"""
        kwargs: dict = {
            "collection_name": collection,
            "data": [vector],
            "limit": top_k,
            "search_params": {"metric_type": self._metric_types.get(collection, "L2")},
            "consistency_level": "Strong",
        }
        if filter_expr:
            kwargs["filter"] = filter_expr
        if output_fields:
            kwargs["output_fields"] = output_fields
        results = await asyncio.to_thread(self._client.search, **kwargs)
        return results[0] if results else []

    async def query(
        self,
        collection: str,
        *,
        filter_expr: str,
        output_fields: list[str] | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        """标量查询 — 按 filter_expr 过滤，不涉及向量搜索。"""
        kwargs: dict = {
            "collection_name": collection,
            "filter": filter_expr,
            "limit": limit,
        }
        if output_fields:
            kwargs["output_fields"] = output_fields
        return await asyncio.to_thread(self._client.query, **kwargs)

    async def delete(self, collection: str, ids: list[int]) -> int:
        """按主键删除向量。"""
        if not ids:
            return 0
        result = await asyncio.to_thread(
            self._client.delete, collection, ids=ids,
        )
        return result.get("delete_count", len(ids))

    # ---- Management ----

    async def close(self) -> None:
        pass  # pymilvus MilvusClient has no explicit close
