"""MongoDB 文档数据库后端 — motor (async-native)。"""

import logging
import re
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from Module.database.documentdb.protocol import DocumentDBBackend

# Collection 名白名单 — 仅允许字母、数字、下划线、连字符
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_collection(name: str) -> None:
    """校验 collection 名称是否合法（仅允许字母、数字、下划线、连字符）。

    Args:
        name: 待校验的 collection 名称。

    Raises:
        ValueError: 名称包含不允许的字符时抛出。
    """
    if not _COLLECTION_NAME_RE.match(name):
        raise ValueError(
            f"无效的 collection 名: {name!r}，"
            f"仅允许字母、数字、下划线和连字符"
        )


class MongoDBBackend(DocumentDBBackend):
    """MongoDB 后端 — 使用 motor 异步驱动。

    find 返回的文档中 _id 会被转换为 str。
    """

    def __init__(
        self,
        *,
        uri: str,
        database: str = "elysia",
        logger: logging.Logger | None = None,
        server_selection_timeout_ms: int = 3000,
    ):
        """初始化 MongoDB 后端。

        Args:
            uri: MongoDB 连接串（如 mongodb://localhost:27017）。
            database: 目标数据库名，默认 "elysia"。
            logger: 日志记录器，未提供时自动创建。
            server_selection_timeout_ms: 服务端选择超时毫秒数，默认 3000。
        """
        self._logger = logger or logging.getLogger(__name__)
        self._client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=server_selection_timeout_ms)
        self._db = self._client[database]
        _validate_collection(database)

    # ---- Write ----

    async def insert_one(self, collection: str, document: dict) -> str:
        _validate_collection(collection)
        # motor 的 insert_one 会修改 document（注入 _id），
        # 用 copy 保护调用方不受副作用影响。
        result = await self._db[collection].insert_one(dict(document))
        return str(result.inserted_id)

    async def insert_many(self, collection: str, documents: list[dict]) -> list[str]:
        _validate_collection(collection)
        if not documents:
            return []
        # motor 会修改每个 document（注入 _id），用 copy 保护调用方
        result = await self._db[collection].insert_many([dict(d) for d in documents])
        return [str(id) for id in result.inserted_ids]

    # ---- Read ----

    async def find(
        self,
        collection: str,
        filter_expr: dict,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int = 100,
        skip: int = 0,
    ) -> list[dict]:
        """查询文档。支持排序、分页。_id 转换为 str。"""
        _validate_collection(collection)
        cursor = self._db[collection].find(filter_expr)
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        docs = await cursor.to_list(length=limit)
        for doc in docs:
            doc["_id"] = str(doc["_id"])
        return docs

    async def update_one(
        self, collection: str, filter_expr: dict, update: dict, upsert: bool = False,
    ) -> int:
        _validate_collection(collection)
        result = await self._db[collection].update_one(filter_expr, update, upsert=upsert)
        return result.modified_count

    async def delete_many(self, collection: str, filter_expr: dict) -> int:
        _validate_collection(collection)
        result = await self._db[collection].delete_many(filter_expr)
        return result.deleted_count

    async def count(self, collection: str, filter_expr: dict) -> int:
        _validate_collection(collection)
        return await self._db[collection].count_documents(filter_expr)

    # ---- Index ----

    async def ensure_index(
        self, collection: str, keys: list[tuple[str, int]], **kwargs: Any
    ) -> str:
        """创建索引（如不存在）。返回索引名。"""
        _validate_collection(collection)
        return await self._db[collection].create_index(keys, **kwargs)

    # ---- Aggregate ----

    async def aggregate(self, collection: str, pipeline: list[dict]) -> list[dict]:
        """执行 MongoDB 聚合管道。_id 转换为 str。"""
        _validate_collection(collection)
        cursor = self._db[collection].aggregate(pipeline)
        docs = await cursor.to_list(length=None)
        for doc in docs:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
        return docs

    # ---- Management ----

    async def close(self) -> None:
        self._client.close()
