"""MongoDB 文档数据库后端 — motor (async-native)。"""

import logging

from motor.motor_asyncio import AsyncIOMotorClient

from .protocol import DocumentDBBackend


class MongoDBBackend(DocumentDBBackend):
    """MongoDB 后端 — 使用 motor 异步驱动。

    find 返回的文档中 _id 会被转换为 str。
    """

    def __init__(
        self,
        *,
        uri: str = "mongodb://localhost:27017",
        database: str = "elysia",
        logger: logging.Logger | None = None,
        server_selection_timeout_ms: int = 3000,
    ):
        self._logger = logger or logging.getLogger(__name__)
        self._client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=server_selection_timeout_ms)
        self._db = self._client[database]

    # ---- Write ----

    async def insert_one(self, collection: str, document: dict) -> str:
        result = await self._db[collection].insert_one(document)
        return str(result.inserted_id)

    async def insert_many(self, collection: str, documents: list[dict]) -> list[str]:
        if not documents:
            return []
        result = await self._db[collection].insert_many(documents)
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
        result = await self._db[collection].update_one(filter_expr, update, upsert=upsert)
        return result.modified_count

    async def delete_many(self, collection: str, filter_expr: dict) -> int:
        result = await self._db[collection].delete_many(filter_expr)
        return result.deleted_count

    async def count(self, collection: str, filter_expr: dict) -> int:
        return await self._db[collection].count_documents(filter_expr)

    # ---- Index ----

    async def ensure_index(
        self, collection: str, keys: list[tuple[str, int]], **kwargs
    ) -> str:
        """创建索引（如不存在）。返回索引名。"""
        return await self._db[collection].create_index(keys, **kwargs)

    # ---- Management ----

    async def close(self) -> None:
        self._client.close()
