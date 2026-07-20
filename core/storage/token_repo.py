"""TokenRepository — MongoDB token_usage 集合操作。"""

import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository

if TYPE_CHECKING:
    from core.schema.token import TokenUsageEntry

logger = logging.getLogger(__name__)

COLLECTION = "token_usage"


class TokenRepository(Repository):
    """Token 用量存储。"""

    def __init__(self, db: DocumentDBBackend | None = None, *, database_name: str = "default"):
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create(f"mongodb/{self._database_name}")
            logger.info("TokenRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("TokenRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return self._db is not None

    async def record(self, entry: "TokenUsageEntry") -> None:
        """记录一条 token 用量。"""
        if self._db is None:
            return
        await self._db.insert_one(COLLECTION, entry.model_dump(mode="json"))

    async def flush(self, entries: list["TokenUsageEntry"]) -> None:
        """批量写入 token 用量。"""
        if self._db is None or not entries:
            return
        docs = [e.model_dump(mode="json") for e in entries]
        await self._db.insert_many(COLLECTION, docs)

    async def query_by_session(self, session_id: str, *,
                               limit: int = 100) -> list["TokenUsageEntry"]:
        if self._db is None:
            return []
        from core.schema.token import TokenUsageEntry
        docs = await self._db.find(
            COLLECTION, {"session_id": session_id}, limit=limit
        )
        return [TokenUsageEntry.model_validate(d) for d in docs]

    async def clear_all(self) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {})
