"""Milvus 向量数据库后端 — pymilvus 实现。

同步 pymilvus API 通过 asyncio.to_thread() 包装为异步。
"""

import asyncio
import logging
from typing import Any, cast

from pymilvus import DataType, MilvusClient

from Module.database.vectordb.protocol import VectorDBBackend

# 索引预设配置 — create_collection 时通过 index_preset 选择
INDEX_PRESETS: dict[str, dict] = {
    "default": {"index_type": "IVF_FLAT", "metric_type": "L2", "params": {"nlist": 1024}},
    "cosine": {"index_type": "IVF_FLAT", "metric_type": "COSINE", "params": {"nlist": 1024}},
    "ip":      {"index_type": "IVF_FLAT", "metric_type": "IP", "params": {"nlist": 1024}},
    "hnsw_l2": {"index_type": "HNSW", "metric_type": "L2", "params": {"M": 16, "efConstruction": 200}},
}

# 支持的 metric_type（距离度量）白名单 — 白名单之外一律 fail closed
SUPPORTED_METRIC_TYPES: frozenset[str] = frozenset({"L2", "COSINE", "IP"})


class MilvusBackend(VectorDBBackend):
    """Milvus 向量数据库后端。

    create_collection 默认不替换已有 collection（防止误删数据）；
    传 replace=True 可先删后建。index_preset 控制索引类型和度量方式。

    metric_type 持久化于 Milvus index params：create_collection 时写入，
    load_collection / search 时从 Milvus 读取恢复（跨实例重启保持排序语义）。
    读取失败或 metric 缺失/不受支持时抛 ValueError（fail closed），
    禁止静默回退 L2 后继续返回看似正常的排序。
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
        **schema_kwargs: Any,
    ) -> None:
        preset = INDEX_PRESETS.get(index_preset)
        if preset is None:
            raise ValueError(
                f"Unknown index_preset '{index_preset}'. "
                f"Known: {', '.join(sorted(INDEX_PRESETS))}"
            )

        # review 复核 #10：metric 校验必须先于 drop/recreate——此前 replace=True 时
        # 先 drop_collection 再校验显式 metric_type，无效 metric 会先破坏现有数据。
        metric = (metric_type or preset["metric_type"]).upper()
        if metric not in SUPPORTED_METRIC_TYPES:
            raise ValueError(
                f"Unsupported metric_type '{metric}' for collection '{name}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_METRIC_TYPES))}"
            )

        exists = await asyncio.to_thread(self._client.has_collection, name)
        if exists:
            if not replace:
                raise ValueError(
                    f"Collection '{name}' already exists. "
                    f"Pass replace=True to drop and recreate."
                )
            await asyncio.to_thread(self._client.drop_collection, name)

        # Schema：id（自增）+ embedding（float 向量）
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

        self._metric_types[name] = metric

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type=preset["index_type"],
            metric_type=metric,
            params=preset["params"],
        )
        await asyncio.to_thread(
            self._client.create_index, name, index_params
        )
        await asyncio.to_thread(self._client.load_collection, name)

    async def drop_collection(self, name: str) -> None:
        await asyncio.to_thread(self._client.drop_collection, name)

    async def load_collection(self, name: str) -> None:
        """加载 collection 到内存，并从 Milvus index params 恢复 metric_type。

        跨实例/重启后进程内 `_metric_types` 为空，此处读取 Milvus 端持久化的
        metric_type 恢复排序语义（COSINE/IP/L2），避免静默默认 L2 改变排序。
        metric 读取失败/缺失时抛 ValueError（fail closed）。
        """
        metric = await asyncio.to_thread(self._read_metric_type, name)
        self._metric_types[name] = metric
        await asyncio.to_thread(self._client.load_collection, name)

    async def collection_exists(self, name: str) -> bool:
        return cast(bool, await asyncio.to_thread(
            self._client.has_collection, name))

    # ---- Write ----

    async def insert(
        self,
        collection: str,
        vectors: list[list[float]],
        metadata: list[dict],
    ) -> list[int]:
        """批量插入。vectors 和 metadata 按索引一一对应，合并为 Milvus row。

        调用 Milvus 前先做输入校验（数量非空、长度相等、维度一致），
        不合法输入抛 ValueError 且不产生任何部分 insert（不做 zip 静默截断）。
        """
        self._validate_batch(collection, vectors, metadata)
        data = []
        for vec, meta in zip(vectors, metadata):
            row = {"embedding": vec, **meta}
            data.append(row)
        result = await asyncio.to_thread(
            self._client.insert, collection, data
        )
        return result["ids"]

    def _validate_batch(
        self,
        collection: str,
        vectors: list[list[float]],
        metadata: list[dict],
    ) -> None:
        """校验批量插入输入：非空、数量相等、维度一致。

        不合法输入抛 ValueError（稳定 validation exception），不调用 Milvus，
        保证不产生部分 insert。

        Args:
            collection: 目标 collection 名称。
            vectors: 向量列表。
            metadata: 元数据列表，与 vectors 按索引一一对应。

        Raises:
            ValueError: 输入为空、数量不等或维度不一致时抛出。
        """
        if not vectors:
            raise ValueError(
                f"批量插入 collection '{collection}' 失败：vectors 为空，拒绝插入。"
            )
        if not metadata:
            raise ValueError(
                f"批量插入 collection '{collection}' 失败：metadata 为空，拒绝插入。"
            )
        if len(vectors) != len(metadata):
            raise ValueError(
                f"批量插入 collection '{collection}' 失败：vectors({len(vectors)}) "
                f"与 metadata({len(metadata)}) 数量不一致，拒绝插入"
                f"（避免 zip 静默截断较长一侧）。"
            )
        dims = {len(vec) for vec in vectors}
        if len(dims) != 1 or 0 in dims:
            raise ValueError(
                f"批量插入 collection '{collection}' 失败：向量维度不一致"
                f"（检测到维度集合 {sorted(dims)}），拒绝插入。"
            )

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
        """ANN 搜索。使用 collection 的 metric_type（创建时写入 / load 时恢复）。

        进程内 `_metric_types` 缺失（如未经过 load_collection 直接搜索）时，
        尝试从 Milvus index params 恢复；恢复失败则抛 ValueError（fail closed），
        禁止静默默认 L2 后继续返回看似正常的排序。
        """
        metric = self._metric_types.get(collection)
        if metric is None:
            metric = await asyncio.to_thread(self._read_metric_type, collection)
            self._metric_types[collection] = metric
        kwargs: dict = {
            "collection_name": collection,
            "data": [vector],
            "limit": top_k,
            "search_params": {"metric_type": metric},
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

    def _read_metric_type(self, name: str) -> str:
        """从 Milvus index params 读取 collection 的 metric_type。

        list_indexes 返回索引名列表，逐个 describe_index 取 metric_type；
        也兼容 list_indexes 直接返回描述 dict 的情况（测试/旧版客户端）。

        Returns:
            metric_type 字符串（规范大写，如 "COSINE"）。

        Raises:
            ValueError: 索引缺失、index 描述缺少 metric_type、metric 不受支持、
                或读取失败时抛出——fail closed，要求显式重建 collection / 显式传入
                metric，禁止静默默认 L2。
        """
        try:
            index_items = self._client.list_indexes(name)
        except Exception as e:
            raise ValueError(
                f"Collection '{name}' 读取索引失败，无法恢复 metric_type"
                f"（{e}）。请显式重建 collection 或显式传入 metric_type。"
            ) from e

        for item in index_items:
            if isinstance(item, str):
                # list_indexes 返回索引名 → describe_index 取详情
                try:
                    desc = self._client.describe_index(name, item)
                except Exception as e:
                    raise ValueError(
                        f"Collection '{name}' 读取 index '{item}' 描述失败，"
                        f"无法恢复 metric_type（{e}）。请显式重建 collection "
                        f"或显式传入 metric_type。"
                    ) from e
                if not isinstance(desc, dict):
                    continue
                metric = desc.get("metric_type")
            elif isinstance(item, dict):
                # 直接给定 index 描述 dict（测试/兼容路径）
                metric = item.get("metric_type")
            else:
                continue

            if not metric:
                continue
            metric = str(metric).upper()
            if metric not in SUPPORTED_METRIC_TYPES:
                raise ValueError(
                    f"Collection '{name}' index 的 metric_type='{metric}' 不受支持"
                    f"（支持：{', '.join(sorted(SUPPORTED_METRIC_TYPES))}）。"
                    f"请显式重建 collection 或显式传入 metric_type。"
                )
            return metric

        raise ValueError(
            f"Collection '{name}' 缺少可用的 metric_type（索引缺失或 index 描述"
            f"缺少 metric_type），无法恢复排序语义。请显式重建 collection "
            f"或显式传入 metric_type。"
        )

    async def ping(self) -> bool:
        """真实连通性探测（P1-05）：list_collections 一次 server 往返。

        Returns:
            True 表示 Milvus 可达，False 表示不可达/超时。
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._client.list_collections), timeout=3.0)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """关闭 Milvus 客户端连接，释放 gRPC 通道/线程池。

        pymilvus 的 MilvusClient 有显式 close()（内部调用 connections.disconnect）。
        不关闭会导致 /restart 每次 start() 新建连接时旧 gRPC 通道/线程随重启累积。
        """
        try:
            await asyncio.to_thread(self._client.close)
        except Exception:
            # close 失败 = gRPC 通道/线程泄漏隐患，记录 warning 而非静默
            self._logger.warning("MilvusClient close 失败（连接可能泄漏）",
                                 exc_info=True)
