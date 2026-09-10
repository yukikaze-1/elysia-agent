"""TokenRepository — MongoDB token_usage 集合操作。"""

import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from core.schema.types import SessionId
from core.storage.attachment_repo import StorageUnavailableError
from core.storage.base import Repository

if TYPE_CHECKING:
    from core.schema.token import TokenUsage

logger = logging.getLogger(__name__)

COLLECTION = "token_usage"


class TokenRepository(Repository):
    """Token 用量存储。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化 Token 用量存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name
        self._base_url = base_url
        self._injected_db = db is not None

    async def connect(self) -> bool:
        """建立 MongoDB 连接。

        Returns:
            True 表示连接成功，False 表示连接失败。
        """
        if self._db is not None:
            # 已注入 db（测试内存 fake）——视为已连接，不覆盖为真实 Mongo。
            return True
        try:
            self._db = DocumentDBFactory.create(
                f"mongodb/{self._database_name}",
                base_url=self._base_url,
            )
            # 真实验证可达性 + 建查询索引（与 message/session repo 对称；
            # token_usage 按 session_id 查询/聚合）
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            logger.info("TokenRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("TokenRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        """关闭数据库连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """健康检查：真实 ping（P1-05），失败时经 connect() 重连 + 建索引。

        Returns:
            True 表示可用，False 表示不可用。
        """
        return await self._health_check()

    async def record(self, entry: "TokenUsage") -> None:
        """记录一条 token 用量。

        未连接（db=None）时抛 StorageUnavailableError——让调用方（TokenTracker._persist）
        except 路径落 JSONL 兜底，不静默丢弃（修复前静默 return → 数据双失）。
        """
        if self._db is None:
            raise StorageUnavailableError("TokenRepository 未连接（MongoDB 不可用）")
        await self._db.insert_one(COLLECTION, entry.model_dump(mode="json"))

    async def flush(self, entries: list["TokenUsage"]) -> None:
        """批量写入 token 用量。

        未连接（db=None）且有数据时抛 StorageUnavailableError——flush_pending 的
        os.remove 不执行，pending 文件保留下次启动重试（修复前静默 no-op + 删文件丢数据）。
        """
        if not entries:
            return
        if self._db is None:
            raise StorageUnavailableError("TokenRepository 未连接（MongoDB 不可用）")
        docs = [e.model_dump(mode="json") for e in entries]
        await self._db.insert_many(COLLECTION, docs)

    async def query_by_session(self, session_id: str, *,
                               limit: int = 100) -> list["TokenUsage"]:
        """按会话查询 Token 用量记录。

        旧文档（prompt_tokens/completion_tokens 字段）validate 失败时跳过，
        不迁移、自然老化。
        """
        if self._db is None:
            return []
        from core.schema.token import TokenUsage
        docs = await self._db.find(COLLECTION, {"session_id": session_id}, limit=limit)
        result: list[TokenUsage] = []
        for d in docs:
            try:
                result.append(TokenUsage.model_validate(d))
            except Exception:
                continue  # 旧文档跳过
        return result

    async def delete_by_session(self, session_id: SessionId) -> int:
        """按会话删除 Token 用量记录（与 MessageRepository.delete_by_session 对称）。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的记录数（0 = 无记录，幂等）。

        Raises:
            StorageUnavailableError: MongoDB 未连接——离线删除不得静默返回 0
                被 caller 当成功。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "TokenRepository 未连接（MongoDB 不可用），token 用量未删除")
        removed = await self._db.delete_many(COLLECTION, {"session_id": session_id})
        logger.info(
            "TokenRepository delete_by_session: session_id=%s, deleted=%d",
            session_id, removed)
        return removed

    async def total_tokens_by_session(self) -> dict[str, int]:
        """聚合每个 session 的 token 总量（MongoDB $group，单次查询）。

        Returns:
            session_id -> token 总量 的字典。若数据库不可用返回空字典。
        """
        if self._db is None:
            return {}
        results = await self._db.aggregate(COLLECTION, [
            {"$group": {"_id": "$session_id", "total": {"$sum": "$total_tokens"}}},
        ])
        return {r["_id"]: r["total"] for r in results}

    async def total_tokens_by_model(self, session_id: str) -> dict[str, int]:
        """聚合指定 session 按 model 分组的 token 量。

        Args:
            session_id: 会话 ID。

        Returns:
            model_name -> token 总量 的字典。若数据库不可用返回空字典。
        """
        if self._db is None:
            return {}
        results = await self._db.aggregate(COLLECTION, [
            {"$match": {"session_id": session_id}},
            {"$group": {"_id": "$model", "total": {"$sum": "$total_tokens"}}},
        ])
        return {r["_id"]: r["total"] for r in results}

    async def clear_all(self) -> int:
        """清空全部 Token 用量记录。

        Returns:
            删除的记录数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        result = await self._db.delete_many(COLLECTION, {})
        logger.warning("TokenRepository clear_all: deleted=%d records", result)
        return result
