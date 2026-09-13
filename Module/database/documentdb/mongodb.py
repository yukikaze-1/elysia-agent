"""MongoDB 文档数据库后端 — motor (async-native)。"""

import asyncio
import logging
import re
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from Module.database.documentdb.protocol import DocumentDBBackend

# Collection 名白名单 — 仅允许字母、数字、下划线、连字符
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


logger = logging.getLogger(__name__)


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


def _is_namespace_not_found(exc: BaseException) -> bool:
    """异常是否表示"集合不存在"（而非查询失败）。

    pymongo 对不存在的集合抛 `OperationFailure`，错误码 26（NamespaceNotFound）。
    也兼容不同驱动的文本措辞（"ns does not exist" / "ns not found"）。
    """
    if getattr(exc, "code", None) == 26:
        return True
    text = str(exc).lower()
    return "ns does not exist" in text or "ns not found" in text


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

    async def list_indexes(self, collection: str) -> list[dict] | None:
        """列出 collection 的全部索引（只读；schema 漂移自检用）。

        **三态**（S2-B1）——原实现把三种情况压成同一个 `[]`：

        | 返回 | 含义 |
        |---|---|
        | `[...]` | 查询成功（集合存在时 Mongo 至少返回内建 `_id_`）|
        | `[]` | **集合不存在**（NamespaceNotFound，从未写入）|
        | `None` | **查询失败**（权限不足 / 连接断开 / 服务端不支持）|

        为什么必须分开：`schema_drift` 把 `[]` 读成"集合不存在（从未写入）"→ 不报
        漂移、自检"通过"；一旦"查询失败"也被折成 `[]`，**排障时最重要的一类故障
        （索引选项冲突导致仓储不可用）恰好会被自检报告成健康**。
        `scanned` 这个"是否真正完成扫描"的区分位就是在这里被绕过的。
        """
        _validate_collection(collection)
        try:
            cursor = self._db[collection].list_indexes()
            return [dict(idx) async for idx in cursor]
        except Exception as exc:
            if _is_namespace_not_found(exc):
                return []          # 集合不存在——合法状态
            logger.warning(
                "list_indexes 查询失败 collection=%s（并非「集合不存在」）：%s",
                collection, exc)
            return None

    async def drop_index(self, collection: str, name: str) -> bool:
        """删除指定索引；索引不存在返回 False（幂等，不抛错）。

        **为什么必须先 `list_indexes()` 查存在性**：Mongo 的 `dropIndexes` 对
        **不存在的索引不报错**（实测返回 `{nIndexesWas: N, ok: 1}`），因此直接
        `await drop_index(name)` 会让本方法**永远返回 True**——调用方无法区分
        「删掉了」与「本来就没有」。故先查后删（此处竞态无害：属运维/诊断操作）。

        `_id_` 是 Mongo 内建索引，删除会报错——显式拒绝并返回 False。
        """
        _validate_collection(collection)
        if name == "_id_":
            return False
        try:
            live = await self.list_indexes(collection)
            if live is None:
                # S2-B1 三态：查询失败 ≠ 索引不存在。删不得——宁可返回 False
                # （幂等语义"没删成"），也不能在状态未知时执行破坏性操作。
                logger.warning("drop_index 放弃：list_indexes 查询失败 collection=%s", collection)
                return False
            existing = {idx.get("name") for idx in live}
            if name not in existing:
                return False
            await self._db[collection].drop_index(name)
            return True
        except Exception:
            # 权限不足 / 连接失败等 → 视为未删除（幂等语义，不抛）
            return False

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

    async def ping(self) -> bool:
        """真实 server 往返探测（P1-05：`_db 非 None` ≠ 连接可用）。

        Returns:
            True 表示 Mongo server 可达，False 表示不可达/超时/解析失败。
        """
        try:
            await asyncio.wait_for(self._db.command("ping"), timeout=3.0)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._client.close()
