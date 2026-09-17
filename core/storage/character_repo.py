"""CharacterRepository — characters 集合（Definition 持久化 + CAS + last-known-good）。"""

from __future__ import annotations

import logging
from collections import OrderedDict

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.character.definition import CharacterDefinition
from core.storage.base import (
    is_duplicate_key,
    Repository,
    StorageUnavailableError,
    search_regex_filter,
)

logger = logging.getLogger(__name__)

COLLECTION = "characters"
CACHE_MAX = 64

# CharacterDefinition camelCase 顶层字段 → snake_case 文档键
# 注意：CharacterDefinition 无 displayName 字段（displayName 在 CharacterRevision）——
# 列表展示的 displayName 由 CharacterService 连接 Revision 得到，不走 Definition 文档。
_TOP_LEVEL_KEYS = {
    "characterId": "character_id",
    "currentRevision": "current_revision",
    "ownerUserId": "owner_user_id",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}


def _to_doc(defn: CharacterDefinition) -> dict:
    data = defn.model_dump(mode="json")
    doc: dict = {}
    for k, v in data.items():
        if k in _TOP_LEVEL_KEYS:
            doc[_TOP_LEVEL_KEYS[k]] = v
        else:
            doc[k] = v
    return doc


def _from_doc(doc: dict) -> CharacterDefinition:
    data: dict = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        if k in _TOP_LEVEL_KEYS.values():
            rev_key = next(dk for dk, dk_val in _TOP_LEVEL_KEYS.items() if dk_val == k)
            data[rev_key] = v
        else:
            data[k] = v
    return CharacterDefinition.model_validate(data)


class CharacterRepository(Repository):
    """characters 集合。

    cas_revision_pointer：filter 带 current_revision（CAS gate）+ $set 恒定含 updated_at
    （必变字段规避 MongoDB no-op 误判——后端只暴露 modified_count，mongodb.py:106）。
    last-known-good：读成功缓存；写成功同步失效（read-your-writes）。
    """

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 base_url: str | None = None):
        self._db = db
        self._base_url = base_url
        self._injected_db = db is not None
        self._cache: OrderedDict[str, CharacterDefinition] = OrderedDict()

    async def connect(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default", base_url=self._base_url)
            await self._db.ensure_index(COLLECTION, [("character_id", 1)], unique=True)
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

    # ---- cache helpers ----

    def _cache_put(self, key: str, val: CharacterDefinition) -> None:
        self._cache[key] = val
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)

    def _cache_invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    async def insert_definition(self, definition: CharacterDefinition) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        try:
            await self._call(
                lambda: self._db.insert_one(COLLECTION, _to_doc(definition)),  # type: ignore[union-attr]
                reason="CharacterRepository.insert_definition 写失败",
                non_fatal=is_duplicate_key,
            )
        except Exception as exc:
            # S2-B2：并发创建同 id 是良性竞争（唯一索引在正常工作）——不判死仓储、
            # 不包装成 503，写缓存后照常返回（后续读回 winner）。
            if not is_duplicate_key(exc):
                raise
            logger.info("角色定义已存在（并发创建竞争）：%s", definition.characterId)
        self._cache_put(definition.characterId, definition)

    async def get_definition(
        self, character_id: str, *, fresh: bool = False
    ) -> CharacterDefinition | None:
        """读角色定义（默认走 read-through 缓存）。

        Args:
            character_id: 角色 ID。
            fresh: True 时**绕过缓存**直读库，并把读到的真值写回缓存。

        为什么需要 `fresh`（#46 缺口行 8 · 5 号缺陷）：CAS 落败时调用方手里那份定义
        **按定义就是过期的**（落败恰恰意味着库里已经不是那个版本），而它又是缓存里的
        同一个对象——不回读就会把过期版本号写进 409 的 `details.currentRevision`，
        客户端按它重试必然再次冲突（死循环）。成功写路径会 `_cache_invalidate`，
        所以"缓存陈旧"只在失败/并发路径上暴露；这里给调用方一条拿到真值的路。
        """
        if not fresh and character_id in self._cache:
            return self._cache[character_id]
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        docs = await self._call(
            lambda: self._db.find(COLLECTION, {"character_id": character_id}, limit=1),  # type: ignore[union-attr]
            reason="CharacterRepository.get_definition 查失败",
        )
        if not docs:
            return None
        got = _from_doc(docs[0])
        # fresh 读也写缓存：它是**更权威**的值，用它顺带修正陈旧条目（否则回读只帮到
        # 本次调用的错误消息，后续读者仍拿到旧值）。
        self._cache_put(character_id, got)
        return got

    async def list_definitions(self, *, source: str | None = None, status: str | None = None,
                               owner_user_id: str | None = None, search: str | None = None,
                               cursor: str | None = None, limit: int = 50) -> list[CharacterDefinition]:
        """列角色定义（目录页）。

        **S3-20 评估：有意不为本方法的过滤组合加索引。** 它是**管理/目录**路径
        （`source` / `status` / `owner_user_id` / `slug` 正则四种可选过滤的任意组合），
        只在上层"列出角色"时走一次、且由 `MAX_CHARACTER_LIST_SCAN` 限界；为组合过滤
        逐一套索引的写放大比它省下的扫描更贵。**真正每次恢复都走的热路径是
        `GenerationRepository.find_by_user_message`**（已补复合索引）。
        若将来目录规模或调用频次显著上升，再按实际查询形态针对性加。
        """
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        filt: dict = {}
        if source:
            filt["source"] = source
        if status:
            filt["status"] = status
        if owner_user_id:
            filt["owner_user_id"] = owner_user_id
        if search:
            # Definition 无 displayName 字段——search 用 slug（create 时 slug=displayName）。
            # S2-B7c：用户输入不得当正则执行；超长按"无匹配"（**不能忽略过滤**——
            # 忽略会返回全部角色，比空结果更危险）。
            clause = search_regex_filter(search)
            if clause is None:
                logger.warning("角色搜索词非法（超长 %d）——按无匹配处理", len(search))
                return []
            filt["slug"] = clause
        docs = await self._call(
            lambda: self._db.find(COLLECTION, filt, sort=[("updated_at", -1)], limit=limit),  # type: ignore[union-attr]
            reason="CharacterRepository.list_definitions 查失败",
        )
        return [_from_doc(d) for d in docs]

    async def cas_revision_pointer(self, character_id: str, expected_revision: int,
                                   new_revision: int, updated_at: str) -> bool:
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        # $set 恒定含 updated_at（必变）——modified_count>0 即命中；==0 表示 expected 过期
        modified = await self._call(
            lambda: self._db.update_one(  # type: ignore[union-attr]
                COLLECTION,
                {"character_id": character_id, "current_revision": expected_revision},
                {"$set": {"current_revision": new_revision, "updated_at": updated_at}},
                upsert=False,
            ),
            reason="CharacterRepository.cas_revision_pointer CAS 失败",
        )
        if modified > 0:
            self._cache_invalidate(character_id)
            return True
        return False

    async def archive(self, character_id: str) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        await self._call(
            lambda: self._db.update_one(  # type: ignore[union-attr]
                COLLECTION, {"character_id": character_id}, {"$set": {"status": "archived"}},
                upsert=False,
            ),
            reason="CharacterRepository.archive 写失败",
        )
        self._cache_invalidate(character_id)

    async def delete_definition(self, character_id: str) -> bool:
        """物理删除 Definition（BE-STAGEH-05 fixture cleanup 用；生产路径不调用）。"""
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        deleted = await self._call(
            lambda: self._db.delete_many(  # type: ignore[union-attr]
                COLLECTION, {"character_id": character_id}),
            reason="CharacterRepository.delete_definition 写失败",
        )
        self._cache_invalidate(character_id)
        return deleted > 0

    async def _health_check(self) -> bool:
        return await super()._health_check()
