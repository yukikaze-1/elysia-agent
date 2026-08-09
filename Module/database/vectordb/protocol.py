"""向量数据库后端抽象接口。"""

from abc import ABC, abstractmethod
from typing import Any


class VectorDBBackend(ABC):
    """向量数据库后端抽象基类。

    只做向量 CRUD + ANN 搜索，不内嵌 Embedding。
    dimension 应与 Embedding 模型的 dimension 保持一致。
    """

    @abstractmethod
    async def create_collection(
        self, name: str, dimension: int, *,
        replace: bool = False, index_preset: str = "", metric_type: str | None = None,
        **schema_kwargs: Any,
    ) -> None:
        """创建 collection。

        Args:
            name: collection 名称。
            dimension: 向量维度，需与 embedding 模型输出维度一致。
            replace: 为 True 时先删后建（幂等重建）。
            index_preset: 索引预设名（如 "default"、"cosine"），对应 INDEX_PRESETS。
            metric_type: 距离度量类型（如 "L2"、"COSINE"、"IP"），
                未指定时使用 index_preset 的默认值。
            **schema_kwargs: 传递给底层 schema 创建的附加参数。

        Raises:
            ValueError: collection 已存在且 replace=False 时抛出。
        """
        ...

    @abstractmethod
    async def drop_collection(self, name: str) -> None:
        """删除 collection。

        Args:
            name: 待删除的 collection 名称。
        """
        ...

    @abstractmethod
    async def collection_exists(self, name: str) -> bool:
        """检查 collection 是否存在。

        Args:
            name: collection 名称。

        Returns:
            True 表示 collection 存在。
        """
        ...

    async def load_collection(self, name: str) -> None:
        """加载 collection 到内存（默认 no-op，Milvus 等外部 DB 需覆盖）。

        Args:
            name: 待加载的 collection 名称。
        """
        pass

    # ---- Write ----

    @abstractmethod
    async def insert(
        self,
        collection: str,
        vectors: list[list[float]],
        metadata: list[dict],
    ) -> list[int]:
        """批量插入向量 + 元数据。

        Args:
            collection: 目标 collection 名称。
            vectors: 向量列表，每个元素为 float 列表，长度等于 dimension。
            metadata: 元数据列表，与 vectors 按索引一一对应。

        Returns:
            插入记录的主键列表。
        """
        ...

    # ---- Read ----

    @abstractmethod
    async def search(
        self,
        collection: str,
        vector: list[float],
        *,
        top_k: int = 10,
        filter_expr: str = "",
        output_fields: list[str] | None = None,
    ) -> list[dict]:
        """ANN 向量搜索，支持标量过滤。

        Args:
            collection: 目标 collection 名称。
            vector: 查询向量。
            top_k: 返回最近邻数量，默认 10。
            filter_expr: 标量过滤表达式（如 "type == 'memory'"）。
            output_fields: 需要返回的标量字段列表。

        Returns:
            搜索结果列表，每条包含 id、distance 及请求的 output_fields。
        """
        ...

    @abstractmethod
    async def query(
        self,
        collection: str,
        *,
        filter_expr: str,
        output_fields: list[str] | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        """纯标量查询（无向量），按时间/类型过滤。

        Args:
            collection: 目标 collection 名称。
            filter_expr: 标量过滤表达式。
            output_fields: 需要返回的标量字段列表。
            limit: 最大返回行数，默认 10000。

        Returns:
            匹配的文档列表。
        """
        ...

    @abstractmethod
    async def delete(self, collection: str, ids: list[int]) -> int:
        """按主键删除向量。

        Args:
            collection: 目标 collection 名称。
            ids: 待删除的主键列表。

        Returns:
            实际删除的记录数。
        """
        ...

    @abstractmethod
    async def ping(self) -> bool:
        """真实连通性探测（带超时，P1-05）。

        Returns:
            True 表示后端可达，False 表示不可达/超时。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭数据库连接。"""
        ...
