"""CharacterInstanceRepository — character_instances 集合的 instance 持久化。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.utils import now_iso
from core.storage.base import Repository, StorageUnavailableError
from core.character.instance import CharacterInstance

if TYPE_CHECKING:
    from core.schema.character import CharacterRuntimeState

logger = logging.getLogger(__name__)
COLLECTION = "character_instances"
_TOP_LEVEL_KEYS = {
    "instanceId": "instance_id", "characterId": "character_id",
    "revisionPolicy": "revision_policy", "pendingRevision": "pending_revision",
    "pendingStatePolicy": "pending_state_policy",
    "ownerUserId": "owner_user_id", "stateVersion": "state_version",
    "createdAt": "created_at", "updatedAt": "updated_at",
}


class CharacterInstanceRepository(Repository):
    """character_instances 集合 instance CRUD + state_version CAS（fail-closed）。"""

    def __init__(self, db: DocumentDBBackend | None = None, *, base_url: str | None = None):
        self._db = db
        self._base_url = base_url
        self._injected_db = db is not None

    async def connect(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default", base_url=self._base_url)
            await self._db.ensure_index(COLLECTION, [("instance_id", 1)], unique=True)
            await self._db.ensure_index(
                COLLECTION,
                [("owner_user_id", 1), ("character_id", 1), ("canonical", 1)],
                unique=True,
                partialFilterExpression={"canonical": True},
            )
            return True
        except Exception as e:
            logger.warning("CharacterInstanceRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return await self._health_check()

    async def get(self, instance_id: str) -> CharacterInstance | None:
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        db = self._db
        docs = await self._call(lambda: db.find(COLLECTION, {"instance_id": instance_id}, limit=1))
        if not docs:
            return None
        return CharacterInstance.model_validate(self._from_doc(docs[0]))

    async def get_canonical(
        self, owner_user_id: str, character_id: str,
    ) -> CharacterInstance | None:
        """按用户与角色取得 canonical instance；不存在返回 None。"""
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        db = self._db
        docs = await self._call(lambda: db.find(
            COLLECTION,
            {
                "owner_user_id": owner_user_id,
                "character_id": character_id,
                "canonical": True,
            },
            limit=1,
        ))
        if not docs:
            return None
        return CharacterInstance.model_validate(self._from_doc(docs[0]))

    async def list_by_character(
        self,
        character_id: str,
        *,
        owner_user_id: str | None = None,
        limit: int = 10_000,
    ) -> list[CharacterInstance]:
        """列出角色实例，供 revision propagation 使用。

        revisionPolicy 的过滤在模型层完成：Stage H 前创建的记录没有持久化该
        字段，模型默认值会把它们按 follow_latest 解释；数据库查询若直接要求
        ``revision_policy=follow_latest`` 会漏掉这些合法旧记录。
        """
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        query: dict[str, object] = {"character_id": character_id}
        if owner_user_id is not None:
            query["owner_user_id"] = owner_user_id
        db = self._db
        docs = await self._call(
            lambda: db.find(COLLECTION, query, sort=[("instance_id", 1)], limit=limit)
        )
        return [CharacterInstance.model_validate(self._from_doc(doc)) for doc in docs]

    async def save(self, instance: CharacterInstance) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        # $set 包装——update_one 的 update 参数要求 Mongo 更新操作符文档(fake 只识别 $set)。
        db = self._db
        await self._call(lambda: db.update_one(
            COLLECTION, {"instance_id": instance.instanceId}, {"$set": self._to_doc(instance)}, upsert=True))

    async def cas_state_version(self, instance_id: str, *, expected: int,
                                new_state: "CharacterRuntimeState", new_version: int) -> bool:
        """CAS state_version：filter 恒含 state_version==expected + updated_at 必变。"""
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        db = self._db
        mod = await self._call(lambda: db.update_one(
            COLLECTION,
            {"instance_id": instance_id, "state_version": expected},
            {"$set": {"state": new_state.model_dump(mode="json"), "state_version": new_version,
                      "updated_at": now_iso()}},
        ))
        return mod > 0

    async def set_pending_revision(
        self,
        instance_id: str,
        *,
        expected_revision: int,
        expected_pending_revision: int | None,
        pending_revision: int,
        state_policy: str,
    ) -> bool:
        """为 follow_latest instance 持久化待应用 revision/statePolicy。"""
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        db = self._db
        mod = await self._call(lambda: db.update_one(
            COLLECTION,
            {
                "instance_id": instance_id,
                "revision": expected_revision,
                "pending_revision": expected_pending_revision,
            },
            {"$set": {
                "pending_revision": pending_revision,
                "pending_state_policy": state_policy,
                "updated_at": now_iso(),
            }},
        ))
        return mod > 0

    async def cas_apply_revision(
        self,
        instance_id: str,
        *,
        expected_revision: int,
        expected_state_version: int,
        expected_pending_revision: int | None,
        new_revision: int,
        new_state: "CharacterRuntimeState",
        new_version: int,
    ) -> bool:
        """原子应用 revision/state policy，并递增 stateVersion。

        pending marker 也进入 CAS 条件，避免并发的下一次角色编辑刚写入更新 marker
        时，被较旧的 runtime reconcile 清掉。
        """
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        db = self._db
        mod = await self._call(lambda: db.update_one(
            COLLECTION,
            {
                "instance_id": instance_id,
                "revision": expected_revision,
                "state_version": expected_state_version,
                "pending_revision": expected_pending_revision,
            },
            {"$set": {
                "revision": new_revision,
                "state": new_state.model_dump(mode="json"),
                "state_version": new_version,
                "pending_revision": None,
                "pending_state_policy": None,
                "updated_at": now_iso(),
            }},
        ))
        return mod > 0

    @staticmethod
    def _to_doc(inst: CharacterInstance) -> dict:
        doc = {}
        for k, v in inst.model_dump(mode="json").items():
            doc[_TOP_LEVEL_KEYS.get(k, k)] = v
        return doc

    @staticmethod
    def _from_doc(doc: dict) -> dict:
        data = {}
        for k, v in doc.items():
            if k == "_id":
                continue
            rev_key = next((dk for dk, dk_v in _TOP_LEVEL_KEYS.items() if dk_v == k), k)
            data[rev_key] = v
        return data
