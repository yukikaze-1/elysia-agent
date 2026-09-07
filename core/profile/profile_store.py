"""UserIdentityStore — 用户身份持久化（bootstrap + CRUD）。

无 Mongo 时降级内存 + JSONL fallback（架构稿 §5.1：启动用 config.agent.user_name 建
initialized=false 的 bootstrap profile；Mongo profile 就绪后为权威）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from core.profile.user_identity import UserIdentity, LOCAL_DEFAULT_PRONOMS

logger = logging.getLogger(__name__)


class UserIdentityStore:
    """UserIdentity 存取（本次先内存 + JSONL file，Mongo 集成就绪后换 repo）。"""

    def __init__(self, *, storage_path: str | None = None):
        self._storage_path = Path(storage_path) if storage_path else None
        self._cached: UserIdentity | None = None
        self._lock = threading.RLock()

    def bootstrap(self, user_name: str) -> UserIdentity:
        """若不存在则从 user_name 建 initialized=false 的 bootstrap profile（幂等）。"""
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

    def get(self) -> UserIdentity | None:
        with self._lock:
            if self._cached is not None:
                return self._cached
            if self._storage_path and self._storage_path.exists():
                self._cached = self._load_from_file()
            return self._cached

    def save(self, identity: UserIdentity) -> UserIdentity:
        """写前 touch（updatedAt + revision++）。"""
        with self._lock:
            identity.touch()
            self._cached = identity
            self._persist(identity)
            return identity

    def update_if_revision(self, expected_revision: int,
                           updates: dict[str, object]) -> UserIdentity | None:
        """CAS 更新：仅当当前 revision == expected_revision 时应用 updates 并 touch()。

        Updates 白名单为 displayName/preferredAddress/pronouns/locale/timezone/
        defaultCharacterId/initialized；不接收 userId/revision/createdAt/updatedAt。
        冲突返回 None（调用方抛 USER_PROFILE_REVISION_CONFLICT）；成功返回新 UserIdentity。
        单进程下用内存锁 + revision 比对实现原子 CAS（JSONL 单文件）。

        C3 fix：对非白名单/未知字段**跳过**而非 setattr——直接 setattr 一个 UserIdentity
        不存在的字段会抛 ValueError（pydantic strict）→ 500；正确行为是忽略未知输入
        （前端可能带多余字段），不阻断合法更新。
        """
        with self._lock:
            cur = self.get()
            if cur is None:
                return None
            if cur.revision != expected_revision:
                return None
            # 白名单：仅 UserIdentity 可更新字段；userId/revision/createdAt/updatedAt 只读
            _UPDATABLE = (
                "displayName", "preferredAddress", "pronouns", "locale",
                "timezone", "defaultCharacterId", "initialized",
            )
            new = cur.model_copy(deep=True)
            for k, v in updates.items():
                if k not in _UPDATABLE:
                    continue  # 未知/只读字段跳过（避免直接 setattr 抛 500）
                if k == "initialized":
                    v = True  # 只能置 true，不能改回 false
                setattr(new, k, v)
            new.touch()
            self._cached = new
            self._persist(new)
            return new

    # ---- 持久化 ----

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
