"""UserIdentityStore — 用户身份持久化（bootstrap + CRUD）。

无 Mongo 时降级内存 + JSONL fallback（架构稿 §5.1：启动用 config.agent.user_name 建
initialized=false 的 bootstrap profile；Mongo profile 就绪后为权威）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.profile.user_identity import UserIdentity, LOCAL_DEFAULT_PRONOMS

logger = logging.getLogger(__name__)


class UserIdentityStore:
    """UserIdentity 存取（本次先内存 + JSONL file，Mongo 集成就绪后换 repo）。"""

    def __init__(self, *, storage_path: str | None = None):
        self._storage_path = Path(storage_path) if storage_path else None
        self._cached: UserIdentity | None = None

    def bootstrap(self, user_name: str) -> UserIdentity:
        """若不存在则从 user_name 建 initialized=false 的 bootstrap profile（幂等）。"""
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
        if self._cached is not None:
            return self._cached
        if self._storage_path and self._storage_path.exists():
            self._cached = self._load_from_file()
        return self._cached

    def save(self, identity: UserIdentity) -> UserIdentity:
        """写前 touch（updatedAt + revision++）。"""
        identity.touch()
        self._cached = identity
        self._persist(identity)
        return identity

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
