"""CharacterRevisionRepository — character_revisions 集合（append-only 版本）。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.character.revision import CharacterRevision
from core.storage.base import (
    Repository,
    StorageUnavailableError,
    is_duplicate_key,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

COLLECTION = "character_revisions"

# CharacterRevision camelCase 顶层字段 → snake_case 文档键
_TOP_LEVEL_KEYS = {
    "characterId": "character_id",
    "schemaVersion": "schema_version",
    "displayName": "display_name",
    "createdBy": "created_by",
    "createdAt": "created_at",
}


def _to_doc(rev: CharacterRevision) -> dict:
    """CharacterRevision → Mongo 文档（顶层 snake_case，嵌套 dict 原样）。

    promptProfile/runtimeDefaults/presentation 是通用 JSON，model_dump(mode="json")
    原样落库（键保留 camelCase，对齐 session_repo.py:139 的 character_state 先例）。
    """
    data = rev.model_dump(mode="json")
    doc: dict = {}
    for k, v in data.items():
        if k in _TOP_LEVEL_KEYS:
            doc[_TOP_LEVEL_KEYS[k]] = v
        else:
            doc[k] = v  # revision/schemaVersion 等无下划线字段 + 嵌套 dict 原样
    return doc


def _from_doc(doc: dict) -> CharacterRevision:
    """Mongo 文档 → CharacterRevision（snake_case 顶层反桥接）。"""
    data: dict = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        if k in _TOP_LEVEL_KEYS.values():
            rev_key = next(dk for dk, dk_val in _TOP_LEVEL_KEYS.items() if dk_val == k)
            data[rev_key] = v
        else:
            data[k] = v
    return CharacterRevision.model_validate(data)


class CharacterRevisionRepository(Repository):
    """character_revisions 集合 —— append-only 版本存储。

    append_revision 写孤儿 revision（create 时 Definition 尚未插入）；CAS 失败回滚
    用 delete_revision。max_revision 用 find+sort+limit 而非 aggregate（假后端兼容）。
    """

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 base_url: str | None = None):
        self._db: DocumentDBBackend | None = db
        self._base_url = base_url
        self._injected_db = db is not None

    async def connect(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default", base_url=self._base_url)
            await self._db.ensure_index(COLLECTION, [("character_id", 1), ("revision", -1)], unique=True)
            return True
        except Exception:
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return await self._health_check()

    async def append_revision(self, revision: CharacterRevision) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        doc = _to_doc(revision)
        # S2-B2：唯一索引冲突（并发同 id+revision 双写）是**良性**竞争信号，
        # 不是存储故障。不加 non_fatal 会 `_mark_unavailable()` 判死整个仓储——
        # 此后所有读写都 raise 直到 60s 健康轮重连，本应 409 的竞争变成 503。
        try:
            await self._call(
                lambda: self._db.insert_one(COLLECTION, doc),  # type: ignore[union-attr]
                reason="CharacterRevisionRepository.append_revision 写失败",
                non_fatal=is_duplicate_key,
            )
        except Exception as exc:
            if not is_duplicate_key(exc):
                raise
            # 良性竞争：唯一索引在正常工作（同一 character_id+revision 并发双写）。
            # **裸抛原始 DuplicateKeyError**——storage 层不 import domain 异常
            # （那会造成 storage→character 反向依赖），由 `core.character.service`
            # 在调用点映射成 `RevisionConflictError`（语义归属那一层）。
            logger.info(
                "CharacterRevisionRepository.append_revision 并发冲突（良性）："
                "character=%s revision=%s", revision.characterId, revision.revision)
            raise

    async def get_revision(self, character_id: str, revision: int) -> CharacterRevision | None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        docs = await self._call(
            lambda: self._db.find(COLLECTION, {"character_id": character_id, "revision": revision}, limit=1),  # type: ignore[union-attr]
            reason="CharacterRevisionRepository.get_revision 查失败",
        )
        return _from_doc(docs[0]) if docs else None

    async def list_revisions(self, character_id: str, *, cursor: str | None = None,
                             limit: int = 50) -> list[CharacterRevision]:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        filt: dict = {"character_id": character_id}
        if cursor:
            filt["revision"] = {"$lt": int(cursor)}  # revision 降序游标
        docs = await self._call(
            lambda: self._db.find(COLLECTION, filt, sort=[("revision", -1)], limit=limit),  # type: ignore[union-attr]
            reason="CharacterRevisionRepository.list_revisions 查失败",
        )
        return [_from_doc(d) for d in docs]

    async def list_revisions_page(
        self, character_id: str, *, cursor: int | None = None, limit: int = 50
    ) -> tuple[list[CharacterRevision], bool]:
        """Return one revision page and whether another page exists.

        The repository uses the numeric revision only internally.  REST exposes an
        opaque encoded cursor through CharacterService, so clients never learn this
        storage detail.
        """
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        filt: dict = {"character_id": character_id}
        if cursor is not None:
            filt["revision"] = {"$lt": cursor}
        docs = await self._call(
            lambda: self._db.find(  # type: ignore[union-attr]
                COLLECTION, filt, sort=[("revision", -1)], limit=limit + 1
            ),
            reason="CharacterRevisionRepository.list_revisions_page 查失败",
        )
        has_more = len(docs) > limit
        return ([_from_doc(d) for d in docs[:limit]], has_more)

    async def max_revision(self, character_id: str) -> int:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        docs = await self._call(
            lambda: self._db.find(COLLECTION, {"character_id": character_id},  # type: ignore[union-attr]
                                   sort=[("revision", -1)], limit=1),
            reason="CharacterRevisionRepository.max_revision 查失败",
        )
        return int(docs[0]["revision"]) if docs else 0

    async def delete_revision(self, character_id: str, revision: int) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        await self._call(
            lambda: self._db.delete_many(COLLECTION, {"character_id": character_id, "revision": revision}),  # type: ignore[union-attr]
            reason="CharacterRevisionRepository.delete_revision 删失败",
        )
