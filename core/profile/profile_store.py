"""UserIdentityStore — 用户身份存取（Mongo 权威 + 进程内只读缓存）。

BE-STAGEH-02：Mongo `user_identity_profiles` 是权威事实源（`UserIdentityRepository`）；
本 store 只持有读缓存，供同步读路径（`get()`）使用。无 repo 装配（单测/纯本地）时
降级为内存 + 可选 JSONL。

- `get()` 同步返回缓存（重启后由 `connect()` 从 Mongo 装载）；
- `connect()` 异步建连 + 装载/原子 bootstrap；
- `update_if_revision()` 异步 CAS——repo 已装配但不可用时 fail-closed（503），
  绝不悄悄写本地造成「档案已更新」的假象。
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

    # ---- 生命周期 ----

    async def connect(self, *, user_name: str) -> UserIdentity:
        """建连 repo 并装载权威 profile；首次空库原子 bootstrap。

        repo 不可用时保留本地 bootstrap（开发降级）——写路径随后 fail-closed，
        不会让「档案已更新」变成只存在于内存的假象。
        """
        repo = self._repo
        if repo is not None:
            try:
                if await repo.connect():
                    loaded = await repo.get(LOCAL_DEFAULT_USER_ID)
                    if loaded is None:
                        seed = UserIdentity(
                            displayName=user_name, pronouns=LOCAL_DEFAULT_PRONOMS)
                        loaded = await repo.bootstrap_if_absent(seed)
                    with self._lock:
                        self._cached = loaded
                    return loaded
                logger.warning("UserIdentityRepository 未连接，用户身份降级为本地缓存")
            except Exception:
                logger.warning(
                    "UserIdentityRepository 装载失败，用户身份降级为本地缓存",
                    exc_info=True)
        return self.bootstrap(user_name=user_name)

    async def close(self) -> None:
        if self._repo is not None:
            await self._repo.close()

    # ---- 读 ----

    def get(self) -> UserIdentity | None:
        with self._lock:
            if self._cached is not None:
                return self._cached
            if self._storage_path and self._storage_path.exists():
                self._cached = self._load_from_file()
            return self._cached

    # ---- 写 ----

    def bootstrap(self, user_name: str) -> UserIdentity:
        """本地 bootstrap（同步、无 Mongo）——repo 未装配/不可用时的降级路径。"""
        with self._lock:
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
        """写前 touch（updatedAt + revision++）。Mongo 未装配时写本地文件。"""
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

        repo 已装配时以 Mongo 为权威（CAS 原子）；repo 存在但不可用 → fail-closed。
        """
        normalized = self._normalize_updates(updates)
        repo = self._repo
        if repo is not None:
            if not repo.is_connected:
                raise StorageUnavailableError("UserIdentityRepository 未连接")
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
