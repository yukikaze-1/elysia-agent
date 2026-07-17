"""CharacterStateRepository — MongoDB session_state 集合。"""

import logging

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository
from core.schema.character import CharacterState

logger = logging.getLogger(__name__)
COLLECTION = "session_state"


class CharacterStateRepository(Repository):
    """角色状态持久化存储。

    单一集合 session_state，存储 CharacterState（合并了旧 emotion/reflector 文档）。
    旧 doc_type="emotion" 和 doc_type="reflector" 文档不再使用，
    启动时自动迁移到 CharacterState 并清理旧文档。
    """

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            logger.info("CharacterStateRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("CharacterStateRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return self._db is not None

    # ---- public interface ----

    async def save_character(self, state: CharacterState) -> None:
        """保存 CharacterState。"""
        if self._db is None:
            return
        doc = state.model_dump(mode="json")
        await self._db.update_one(
            COLLECTION,
            {"session_id": state.session_id},
            {"$set": doc},
            upsert=True,
        )

    async def load_character(self, session_id: str) -> CharacterState | None:
        """加载 CharacterState。"""
        if self._db is None:
            return None
        results = await self._db.find(
            COLLECTION,
            {"session_id": session_id},
            limit=1,
        )
        if not results:
            return None
        return CharacterState.model_validate(results[0])
