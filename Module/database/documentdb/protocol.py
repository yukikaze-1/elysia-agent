"""文档数据库后端抽象接口。"""

from abc import ABC, abstractmethod


class DocumentDBBackend(ABC):
    """文档数据库后端抽象基类。

    MongoDB 等 schema-less 数据库的通用接口。
    """

    @abstractmethod
    async def insert_one(self, collection: str, document: dict) -> str:
        """插入单条文档，返回 _id。"""
        ...

    @abstractmethod
    async def insert_many(self, collection: str, documents: list[dict]) -> list[str]:
        """批量插入，返回 _id 列表。"""
        ...

    @abstractmethod
    async def find(
        self,
        collection: str,
        filter_expr: dict,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int = 100,
        skip: int = 0,
    ) -> list[dict]:
        """查询文档列表，支持排序和分页。"""
        ...

    @abstractmethod
    async def update_one(
        self, collection: str, filter_expr: dict, update: dict
    ) -> int:
        """更新单条文档，返回修改数。"""
        ...

    @abstractmethod
    async def delete_many(self, collection: str, filter_expr: dict) -> int:
        """按条件删除，返回删除数。"""
        ...

    @abstractmethod
    async def count(self, collection: str, filter_expr: dict) -> int:
        """按条件计数。"""
        ...

    @abstractmethod
    async def ensure_index(
        self, collection: str, keys: list[tuple[str, int]], **kwargs
    ) -> str:
        """创建索引（如不存在）。返回索引名。"""
        ...

    @abstractmethod
    async def close(self) -> None: ...
