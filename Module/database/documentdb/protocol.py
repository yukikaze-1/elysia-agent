"""文档数据库后端抽象接口。"""

from abc import ABC, abstractmethod
from typing import Any


class DocumentDBBackend(ABC):
    """文档数据库后端抽象基类。

    MongoDB 等 schema-less 数据库的通用接口。
    """

    @abstractmethod
    async def insert_one(self, collection: str, document: dict) -> str:
        """插入单条文档。

        Args:
            collection: 目标 collection 名称。
            document: 待插入的文档，dict 格式。

        Returns:
            插入文档的 _id 字符串。
        """
        ...

    @abstractmethod
    async def insert_many(self, collection: str, documents: list[dict]) -> list[str]:
        """批量插入文档。

        Args:
            collection: 目标 collection 名称。
            documents: 待插入的文档列表。

        Returns:
            插入文档的 _id 字符串列表。
        """
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
        """查询文档列表，支持排序和分页。

        Args:
            collection: 目标 collection 名称。
            filter_expr: MongoDB 查询过滤条件。
            sort: 排序规则，列表元素为 (字段名, 方向) 元组，1 升序 -1 降序。
            limit: 最大返回文档数，默认 100。
            skip: 跳过的文档数，用于分页。

        Returns:
            匹配的文档列表，_id 已转换为字符串。
        """
        ...

    @abstractmethod
    async def update_one(
        self, collection: str, filter_expr: dict, update: dict, upsert: bool = False,
    ) -> int:
        """更新单条文档。

        Args:
            collection: 目标 collection 名称。
            filter_expr: 匹配条件。
            update: 更新操作符（如 {"$set": {...}}）。
            upsert: 为 True 时，如果不存在则插入。

        Returns:
            实际修改的文档数（0 或 1）。
        """
        ...

    @abstractmethod
    async def delete_many(self, collection: str, filter_expr: dict) -> int:
        """按条件删除文档。

        Args:
            collection: 目标 collection 名称。
            filter_expr: 删除条件。

        Returns:
            被删除的文档数。
        """
        ...

    @abstractmethod
    async def count(self, collection: str, filter_expr: dict) -> int:
        """按条件计数。

        Args:
            collection: 目标 collection 名称。
            filter_expr: 计数条件。

        Returns:
            匹配的文档数。
        """
        ...

    @abstractmethod
    async def ensure_index(
        self, collection: str, keys: list[tuple[str, int]], **kwargs: Any,
    ) -> str:
        """创建索引（如不存在）。

        Args:
            collection: 目标 collection 名称。
            keys: 索引键列表，元素为 (字段名, 方向) 元组。
            **kwargs: 传递给底层 create_index 的附加参数。

        Returns:
            创建的索引名。
        """
        ...

    @abstractmethod
    async def list_indexes(self, collection: str) -> list[dict] | None:
        """列出 collection 的全部索引（schema 漂移自检用，只读）。

        Args:
            collection: 目标 collection 名称。

        Returns:
            索引描述列表，每项含 name/key/unique/TTL 等元信息；
            collection 不存在时返回空列表（不抛错——自检不得因缺集合而失败）。
        """
        ...

    @abstractmethod
    async def drop_index(self, collection: str, name: str) -> bool:
        """删除指定索引（**schema 变更的唯一出路**）。

        `ensure_index` 是幂等**只增**的：改了索引键会新建一个，旧索引**永远留着**
        （Mongo 按键自动命名，键不同即名字不同），而残留索引会静默改变行为
        （实测案例：`sessions` 的 30 天 TTL 索引从代码删除后仍留在库里，
        若不清掉会话会静默过期消失）。本方法补上这个缺失的原语。

        Args:
            collection: 目标 collection 名称。
            name: 索引名。

        Returns:
            True 表示已删除；False 表示索引不存在（幂等，不抛错）。
        """
        ...

    @abstractmethod
    async def aggregate(self, collection: str, pipeline: list[dict]) -> list[dict]:
        """执行 MongoDB 聚合管道。

        Args:
            collection: 目标 collection 名称。
            pipeline: MongoDB 聚合管道阶段列表。

        Returns:
            聚合结果文档列表，_id 已转换为字符串。
        """
        ...

    @abstractmethod
    async def ping(self) -> bool:
        """真实连通性探测（带超时）。

        B1/P1-05：`_db is not None` 不代表连接可用——Motor client 断网后仍非
        None。健康检查必须经此方法做真实 server 往返，失败返回 False。

        Returns:
            True 表示后端可达，False 表示不可达/超时。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭数据库连接。"""
        ...
