"""CharacterStateRepository — MongoDB session_state 集合。"""

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository
from core.schema.character import CharacterState
from core.schema.emotion import EmotionState
from core.schema.reflector import ReflectorState

logger = logging.getLogger(__name__)
COLLECTION = "session_state"


class CharacterStateRepository(Repository):
    """角色状态持久化存储（CharacterState / EmotionState / ReflectorState）。

    单一集合 session_state，doc_type 字段区分三种文档类型。
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

    # ---- generic save/load helpers ----

    async def _save(self, session_id: str, doc_type: str, state: BaseModel) -> None:
        if self._db is None:
            return
        doc = state.model_dump(mode="json")
        doc["doc_type"] = doc_type
        await self._db.update_one(
            COLLECTION,
            {"session_id": session_id, "doc_type": doc_type},
            {"$set": doc},
            upsert=True,
        )

    async def _load(self, session_id: str, doc_type: str, model_cls: type[BaseModel]):
        if self._db is None:
            return None
        results = await self._db.find(
            COLLECTION,
            {"session_id": session_id, "doc_type": doc_type},
            limit=1,
        )
        if not results:
            return None
        return model_cls.model_validate(results[0])

    # ---- public interface ----

    async def save_character(self, state: CharacterState) -> None:
        await self._save(state.session_id, "character", state)

    async def load_character(self, session_id: str) -> CharacterState | None:
        return await self._load(session_id, "character", CharacterState)

    async def save_emotion(self, state: EmotionState) -> None:
        await self._save(state.session_id, "emotion", state)

    async def load_emotion(self, session_id: str) -> EmotionState | None:
        return await self._load(session_id, "emotion", EmotionState)

    async def save_reflector(self, state: ReflectorState) -> None:
        await self._save(state.session_id, "reflector", state)

    async def load_reflector(self, session_id: str) -> ReflectorState | None:
        return await self._load(session_id, "reflector", ReflectorState)
