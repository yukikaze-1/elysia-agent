"""向量数据库后端抽象接口。"""

from abc import ABC, abstractmethod


class VectorDBBackend(ABC):
    """向量数据库后端抽象基类。

    只做向量 CRUD + ANN 搜索，不内嵌 Embedding。
    dimension 应与 Embedding 模型的 dimension 保持一致。
    """

    @abstractmethod
    async def create_collection(
        self, name: str, dimension: int, *,
        replace: bool = False, index_preset: str = "", metric_type: str | None = None,
        **schema_kwargs,
    ) -> None:
        """创建 collection。replace=True 时先删后建。"""
        ...

    @abstractmethod
    async def drop_collection(self, name: str) -> None: ...

    @abstractmethod
    async def collection_exists(self, name: str) -> bool: ...

    # ---- Write ----

    @abstractmethod
    async def insert(
        self,
        collection: str,
        vectors: list[list[float]],
        metadata: list[dict],
    ) -> list[int]:
        """批量插入向量 + 元数据，返回主键列表。"""
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
        """ANN 向量搜索，支持标量过滤。"""
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
        """纯标量查询（无向量），按时间/类型过滤。"""
        ...

    @abstractmethod
    async def close(self) -> None: ...
