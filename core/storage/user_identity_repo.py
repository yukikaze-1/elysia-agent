"""UserIdentityRepository — user_identity_profiles 集合（用户身份权威持久化）。

BE-STAGEH-02：真实联调复现「重启后端后 `/api/me` 回到 bootstrap」——此前
UserIdentityStore 只有进程内缓存 + 可选 JSONL，Mongo 权威从未兑现。本 repo 是
用户身份的唯一持久化事实源：唯一 bootstrap（setOnInsert）+ revision CAS 更新。

字段命名与其他 repository 一致：domain camelCase ↔ Mongo snake_case 显式映射。
"""

from __future__ import annotations

import logging

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.profile.user_identity import UserIdentity, UserIdentityUpdateOutcome
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
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化。

        S2-R6-1（真实 MongoDB 测试层）：新增 `database_name`——本仓储此前把库名
        **硬编码**成 `"mongodb/default"`，是同层唯一不接受库名的仓储（其余仓储都有
        这个参数）。后果有二：① 真实库测试层无法把它指向测试库（`base_url` 的 path
        **不是**库名——见 `tests/real_mongo/conftest.py` 记录的那次误写生产库事故）；
        ② 部署侧也无法把身份档放到别的库。默认值保持 `"default"`，生产行为不变。
        """
        self._db = db
        self._database_name = database_name
        self._base_url = base_url
        self._injected_db = db is not None

    async def connect(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create(
                f"mongodb/{self._database_name}", base_url=self._base_url)
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
    ) -> UserIdentityUpdateOutcome:
        """CAS 更新：filter 含 revision==expected，$set 同一次原子操作内递增版本。

        返回 `UserIdentityUpdateOutcome`：
        - 命中 → `identity` = 写后回读的身份；
        - 未命中 → `current_revision` = **权威库当场回读**的现值（调用方据此报 409）。

        **N-3 / Batch 6（冲突元数据真值）**：旧签名返回 `UserIdentity | None`，`None`
        把"冲突"与"现值是多少"一起丢掉，路由层只好回头问进程内缓存——多 writer 时
        那是陈旧值，于是 409 谎报 revision。真实 revision 的**唯一权威**是 Mongo 文档
        的 `revision` 字段；冲突产生点（本方法）知道回读它的时机与语义，故由此处回读、
        由此处携带，而不是让 route/store 各自猜一次。

        **语义（必须写清，防止被读成更强的承诺）**：`current_revision` 是**构造冲突
        结果那一刻**可观察到的权威现值（response-time），不保证等于"击败本次写入的
        那个 writer 的 revision"——Mongo 的 `update_one` 在 filter 未命中时只返回计数
        （`matched_count=0`），**拿不到**冲突瞬间的 actual revision（Batch 6 §12：不许
        用额外 read 假装拥有这种更强语义）。并发写者 C 在 CAS 与回读之间提交时，
        上报的是 C 之后的值——这正是客户端恢复所需要的值。

        **只读、无副作用**：回读走 `get()`（纯读，不 bump/reload/repair）；回读失败
        抛 `StorageUnavailableError`（由调用方 fail-closed 成 503），**绝不**回退成
        缓存值或 `expected + 1`。记录不存在（外部删除）→ `current_revision=None`
        （"不知道"），不伪造。

        **三种"读不到/读不准"各有去处（Batch 6 Challenger J-1/J-2/J-4）**：
        - 记录不存在 → `current_revision=None`（409 省略该字段，客户端 GET 重取）；
        - 记录存在但**无法解码**成域对象（外部写入者把字段写坏）→
          `StorageUnavailableError`（503）：这是"权威存储此刻不能服务"，与
          `connect()` 对同一状态的判定同类；**不得**让它以裸 `ValidationError`
          冒泡成 500（那是 fail-crash，不是 fail-closed）；
        - 解码值**等于** `expected_revision` → 报"未知"（None）：CAS 在 BSON 比较域
          里明确未命中，而解码后却与 expected 相等，只可能是存的值类型/形态与域模型
          不一致（如 `revision` 存成字符串 `"1"` 或缺字段走默认值）。上报它会让客户端
          拿着与 expected 相同的值无限重试（N-3 的死循环换个门回来），且违反
          「不得回显客户端提交的 expected revision」（CM2 字面）。
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
            # CAS 未命中：**当场**回读权威现值（唯一权威 = Mongo 文档 revision）。
            # 未命中既可能是 revision 过期，也可能是文档已不存在——两者都经同一次
            # 回读区分：读不到 ⇒ None = 未知（§31：不把 not-found 伪装成有值的冲突）。
            fresh = await self._read_authoritative(user_id)
            current = None if fresh is None else fresh.revision
            if current == expected_revision:
                logger.warning(
                    "用户身份冲突回读值与 expectedRevision 相同（%s）——库中形态与域模型"
                    "不一致，按「未知」上报而不是回显客户端期望值", expected_revision)
                current = None
            return UserIdentityUpdateOutcome(current_revision=current)
        identity = await self._read_authoritative(user_id)
        if identity is None:
            # CAS **已应用**（modified_count > 0）但写后读不回：不能把它报成"冲突"
            # （那会让客户端以为写入失败而重放），也不能报 200（拿不出身份）——
            # 归入"权威存储此刻不能服务"，fail-closed 成 503（Challenger J-4）。
            raise StorageUnavailableError("用户身份 CAS 已应用但写后回读为空")
        return UserIdentityUpdateOutcome(identity=identity)

    async def _read_authoritative(self, user_id: str) -> UserIdentity | None:
        """权威回读（冲突/写后两条路径共用）：解码失败 → `StorageUnavailableError`。

        为什么在 repo 边界把 `ValidationError` 转成存储错误：库里存在一条**读不出域
        对象**的记录时，"存储现在不能服务"是准确的描述（`UserIdentityStore.connect()`
        对同一状态也是这么判定的：装载失败 ⇒ 身份不可用）。让它以裸 pydantic 异常冒泡
        会穿透路由的 `except StorageUnavailableError`，变成 500 INTERNAL ——
        Batch 6 Challenger J-2 实测的 fail-crash 回归（修前该状态是 409）。
        """
        try:
            return await self.get(user_id)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError("用户身份权威记录无法解码") from exc

