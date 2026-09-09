"""UserIdentityStore — 用户身份存取（Mongo 权威 + 进程内只读缓存）。

BE-STAGEH-02：Mongo `user_identity_profiles` 是权威事实源（`UserIdentityRepository`）；
本 store 只持有读缓存，供同步读路径（`get()`）使用。无 repo 装配（单测/显式本地
模式）时降级为内存 + 可选 JSON 文件。

P0-3（fail-closed）：
- 装配了 repo 时**绝不**伪造本地 profile——`connect()` 装载失败即 `get()` 返回
  None、`is_available` 为 False，依赖身份的业务（`/api/me` 写、preview、会话创建、
  generation）一律 fail-closed 返回稳定公开错误；
- `health_check()` 供数据库恢复协调器探测；Mongo 恢复后 `connect()` 重新装载权威
  档案并原子替换缓存（不覆盖 Mongo 中更高 revision 的文档）。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from core.profile.user_identity import (
    UserIdentity,
    LOCAL_DEFAULT_PRONOMS,
    LOCAL_DEFAULT_USER_ID,
)
from core.storage.base import StorageUnavailableError
from core.storage.user_identity_repo import UserIdentityRepository

logger = logging.getLogger(__name__)


class UserIdentityStore:
    """UserIdentity 存取（Mongo 权威 + 进程内缓存）。"""

    def __init__(self, *, storage_path: str | None = None,
                 repo: UserIdentityRepository | None = None):
        self._storage_path = Path(storage_path) if storage_path else None
        self._repo = repo
        self._cached: UserIdentity | None = None
        self._lock = threading.RLock()

    # ---- 权威性判定 ----

    @property
    def is_authoritative(self) -> bool:
        """是否装配了权威 repo（生产 Agent 装配路径）。"""
        return self._repo is not None

    @property
    def is_available(self) -> bool:
        """权威身份是否可用。非权威模式（本地/单测）恒 True。"""
        if self._repo is None:
            return True
        return self._cached is not None and self._repo.is_connected

    # ---- 生命周期 ----

    async def connect(self, *, user_name: str) -> UserIdentity | None:
        """建连 repo 并装载权威 profile；首次空库原子 bootstrap。

        权威模式装载失败**不**降级为本地 profile——返回 None 并清空缓存，业务侧
        据此 fail-closed。无 repo 时走本地 bootstrap。

        Returns:
            装载到的权威身份；权威模式不可用时为 None。
        """
        repo = self._repo
        if repo is None:
            return self.bootstrap(user_name=user_name)
        try:
            if not await repo.connect():
                raise StorageUnavailableError("UserIdentityRepository 未连接")
            loaded = await repo.get(LOCAL_DEFAULT_USER_ID)
            if loaded is None:
                seed = UserIdentity(
                    displayName=user_name, pronouns=LOCAL_DEFAULT_PRONOMS)
                loaded = await repo.bootstrap_if_absent(seed)
            with self._lock:
                self._cached = loaded
            return loaded
        except Exception:
            logger.warning(
                "用户身份权威装载失败——身份不可用（fail-closed，不伪造本地档案）",
                exc_info=True)
            with self._lock:
                self._cached = None
            return None

    async def health_check(self) -> bool:
        """权威 repo 可达性（恢复协调器探针）。无 repo（本地模式）视为 ok。"""
        repo = self._repo
        if repo is None:
            return True
        try:
            return bool(await repo.health_check())
        except Exception:
            return False

    async def close(self) -> None:
        if self._repo is not None:
            await self._repo.close()

    # ---- 读 ----

    def get(self) -> UserIdentity | None:
        with self._lock:
            if self._repo is not None:
                # 权威模式：只信 Mongo 装载结果——绝不回退 JSON 文件或内存伪造。
                return self._cached
            if self._cached is not None:
                return self._cached
            if self._storage_path and self._storage_path.exists():
                self._cached = self._load_from_file()
            return self._cached

    def placeholder(self, user_name: str) -> UserIdentity:
        """构造期占位身份——固定单用户 userId，不缓存、不持久化。

        仅供 Agent 构造期的 scope/owner 常量使用（`usr_local_default` 是 V1 单用户
        常量）。权威档案必须由 `connect()` 从 Mongo 装载；`get()` 在装载成功前恒为
        None。本地模式（无 repo）下等价于 `bootstrap()`。
        """
        with self._lock:
            if self._cached is not None:
                return self._cached
        return self.bootstrap(user_name=user_name)

    # ---- 写 ----

    def bootstrap(self, user_name: str) -> UserIdentity:
        """本地 bootstrap——仅无 repo 的本地/单测模式（写入内存 + 可选 JSON 文件）。

        权威模式（repo 已装配）下**不**缓存、不落盘，只返回占位身份：生产装配路径
        的真实身份只能来自 `connect()` 的 Mongo 装载，装载失败即 fail-closed。
        """
        with self._lock:
            if self._repo is not None:
                return UserIdentity(
                    userId=LOCAL_DEFAULT_USER_ID,
                    displayName=user_name,
                    pronouns=LOCAL_DEFAULT_PRONOMS,
                )
            if self._cached is not None:
                return self._cached
            if self._storage_path and self._storage_path.exists():
                self._cached = self._load_from_file()
                if self._cached is not None:
                    return self._cached
            self._cached = UserIdentity(displayName=user_name, pronouns=LOCAL_DEFAULT_PRONOMS)
            self._persist(self._cached)
            return self._cached

    async def save(self, identity: UserIdentity) -> UserIdentity:
        """写前 touch（updatedAt + revision++）。无 repo 时写本地文件。"""
        with self._lock:
            identity.touch()
            self._cached = identity
            self._persist(identity)
            return identity

    async def update_if_revision(self, expected_revision: int,
                                 updates: dict[str, object]) -> UserIdentity | None:
        """CAS 更新：仅当当前 revision == expected_revision 时应用并递增。

        Updates 白名单为 displayName/preferredAddress/pronouns/locale/timezone/
        defaultCharacterId/initialized；不接收 userId/revision/createdAt/updatedAt。
        冲突返回 None（调用方抛 USER_PROFILE_REVISION_CONFLICT）。

        repo 已装配时以 Mongo 为权威（CAS 原子）；权威身份不可用 → fail-closed
        （StorageUnavailableError → 503），不写本地造成「档案已更新」的假象。
        """
        normalized = self._normalize_updates(updates)
        repo = self._repo
        if repo is not None:
            if not self.is_available:
                raise StorageUnavailableError("用户身份权威存储不可用")
            new = await repo.update_if_revision(
                LOCAL_DEFAULT_USER_ID, expected_revision, normalized)
            if new is not None:
                with self._lock:
                    self._cached = new
            return new
        return self._update_local(expected_revision, normalized)

    # ---- 内部 ----

    @staticmethod
    def _normalize_updates(updates: dict[str, object]) -> dict[str, object]:
        """白名单过滤 + initialized 只能置 true（C3 fix：未知字段跳过而非 setattr）。"""
        updatable = (
            "displayName", "preferredAddress", "pronouns", "locale",
            "timezone", "defaultCharacterId", "initialized",
        )
        normalized: dict[str, object] = {}
        for key, value in updates.items():
            if key not in updatable:
                continue
            normalized[key] = True if key == "initialized" else value
        return normalized

    def _update_local(self, expected_revision: int,
                      updates: dict[str, object]) -> UserIdentity | None:
        """无 repo 时的内存 + JSONL CAS（单进程）。"""
        with self._lock:
            cur = self.get()
            if cur is None:
                return None
            if cur.revision != expected_revision:
                return None
            new = cur.model_copy(deep=True)
            for key, value in updates.items():
                setattr(new, key, value)
            new.touch()
            self._cached = new
            self._persist(new)
            return new

    def _load_from_file(self) -> UserIdentity | None:
        if self._storage_path is None:
            return None
        try:
            data = json.loads(self._storage_path.read_text("utf-8"))
            return UserIdentity.model_validate(data)
        except Exception:
            logger.warning("UserIdentity 文件解析失败，重新 bootstrap：%s", self._storage_path)
            return None

    def _persist(self, identity: UserIdentity) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._storage_path.write_text(
            identity.model_dump_json(), encoding="utf-8")
