"""UserIdentityRepository — user_identity_profiles 集合（用户身份权威持久化）。

BE-STAGEH-02：真实联调复现「重启后端后 `/api/me` 回到 bootstrap」——此前
UserIdentityStore 只有进程内缓存 + 可选 JSONL，Mongo 权威从未兑现。本 repo 是
用户身份的唯一持久化事实源：唯一 bootstrap（setOnInsert）+ revision CAS 更新。

字段命名与其他 repository 一致：domain camelCase ↔ Mongo snake_case 显式映射。
"""

from __future__ import annotations

import logging

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.profile.user_identity import UserIdentity
from core.schema.utils import now_iso
from core.storage.base import is_duplicate_key, Repository, StorageUnavailableError

logger = logging.getLogger(__name__)

COLLECTION = "user_identity_profiles"

# UserIdentity camelCase 字段 → Mongo snake_case 文档键
_FIELD_MAP = {
    "userId": "user_id",
    "displayName": "display_name",
    "preferredAddress": "preferred_address",
    "pronouns": "pronouns",
    "locale": "locale",
    "timezone": "timezone",
    "initialized": "initialized",
    "defaultCharacterId": "default_character_id",
    "revision": "revision",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}
_REVERSE_MAP = {v: k for k, v in _FIELD_MAP.items()}

# 可更新白名单（与 UserIdentityStore 的领域规则同源；只读字段不可经此改写）
UPDATABLE_FIELDS = frozenset({
    "displayName", "preferredAddress", "pronouns", "locale",
    "timezone", "defaultCharacterId", "initialized",
})


def _to_doc(identity: UserIdentity) -> dict:
    data = identity.model_dump(mode="json")
    return {_FIELD_MAP[k]: v for k, v in data.items() if k in _FIELD_MAP}


def _from_doc(doc: dict) -> UserIdentity:
    data: dict = {}
    for key, value in doc.items():
        if key == "_id":
            continue
        domain_key = _REVERSE_MAP.get(key, key)
        data[domain_key] = value
    return UserIdentity.model_validate(data)


class UserIdentityRepository(Repository):
    """user_identity_profiles 集合（唯一 user_id + revision CAS，fail-closed）。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 base_url: str | None = None):
        self._db = db
        self._base_url = base_url
        self._injected_db = db is not None

    async def connect(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default", base_url=self._base_url)
            await self._db.ensure_index(COLLECTION, [("user_id", 1)], unique=True)
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

    async def get(self, user_id: str) -> UserIdentity | None:
        if self._db is None:
            raise StorageUnavailableError("UserIdentityRepository 未连接")
        docs = await self._call(
            lambda: self._db.find(COLLECTION, {"user_id": user_id}, limit=1),  # type: ignore[union-attr]
            reason="UserIdentityRepository.get 查失败",
        )
        if not docs:
            return None
        return _from_doc(docs[0])

    async def bootstrap_if_absent(self, identity: UserIdentity) -> UserIdentity:
        """原子 bootstrap：已存在则不覆盖（setOnInsert），返回权威文档。

        并发启动时 setOnInsert 保证只有一个 winner——失败方随后 get() 读到 winner。
        """
        if self._db is None:
            raise StorageUnavailableError("UserIdentityRepository 未连接")
        db = self._db
        try:
            await self._call(
                lambda: db.update_one(
                    COLLECTION,
                    {"user_id": identity.userId},
                    {"$setOnInsert": _to_doc(identity)},
                    upsert=True,
                ),
                # S2-B2：并发 bootstrap 时 setOnInsert 只有一个 winner，loser 可能撞
                # 唯一索引 → 良性竞争，不得判死整个仓储（否则身份存储 60s 不可用，
                # 而 `/ready` 的 chatReady 含 identity 门禁 → 全站 503）。
                non_fatal=is_duplicate_key,
                reason="UserIdentityRepository.bootstrap_if_absent 写失败",
            )
        except Exception as exc:
            if not is_duplicate_key(exc):
                raise
            # **docstring 承诺的那一步**：loser 读回 winner。此前异常直接冒泡，
            # 永远到不了下面的 get()——"读回 winner"只写在注释里。
            logger.info("用户身份并发 bootstrap 冲突（良性），读回 winner：%s",
                        identity.userId)
        loaded = await self.get(identity.userId)
        if loaded is None:
            raise StorageUnavailableError("UserIdentityRepository bootstrap 后读取失败")
        return loaded

    async def update_if_revision(
        self,
        user_id: str,
        expected_revision: int,
        updates: dict[str, object],
    ) -> UserIdentity | None:
        """CAS 更新：filter 含 revision==expected，$set 同一次原子操作内递增版本。

        返回 None 表示 expectedRevision 已过期（调用方映射 409）。未知/只读字段跳过。
        """
        if self._db is None:
            raise StorageUnavailableError("UserIdentityRepository 未连接")
        patch: dict = {}
        for key, value in updates.items():
            if key not in UPDATABLE_FIELDS:
                continue
            patch[_FIELD_MAP[key]] = value
        patch["revision"] = expected_revision + 1
        patch["updated_at"] = now_iso()
        db = self._db
        modified = await self._call(
            lambda: db.update_one(
                COLLECTION,
                {"user_id": user_id, "revision": expected_revision},
                {"$set": patch},
                upsert=False,
            ),
            reason="UserIdentityRepository.update_if_revision CAS 失败",
        )
        if modified <= 0:
            return None
        return await self.get(user_id)
