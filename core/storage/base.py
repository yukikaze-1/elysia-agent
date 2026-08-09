# core/storage/base.py — Repository 抽象基类
"""Repository 抽象基类 — 所有存储后端的最低接口 + 统一不可用语义（B1）。

存储一致性语义（P1-05，2026-08-10）：
- `StorageUnavailableError` 为存储不可用的统一 typed 异常。repo 操作在存储
  未连接/断连时必须抛它（fail-closed），禁止静默 return 被 caller 当作成功。
  `AttachmentRepository` 是既有 fail-closed 先例；其余 repo 在 Batch B 各任务
  中逐一切换为 raise。
- 真实健康检查：`health_check` 应委托 `_health_check()`——调用后端 `ping()`
  做带超时的真实探测，而非仅判断 `_db is not None`（Motor client 断网后仍非
  None，假阳性会让 `/ready` 在存储失效时继续 200）。
- 操作/探测失败后经 `_mark_unavailable()` 使后端状态失效（`_db = None`），
  触发下一轮健康检查真正重连。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.database.documentdb import DocumentDBBackend


class StorageUnavailableError(RuntimeError):
    """存储后端不可用（MongoDB 未连接/断连/分页失败）。

    存储操作 fail-closed 的统一异常，由调用方映射为 503 STORAGE_UNAVAILABLE。
    历史导入从 `core.storage.attachment_repo` re-export（向后兼容）。
    """


class Repository(ABC):
    """存储仓库基类。

    子类约定：
    - `__init__` 注入 `db` 参数时置 `self._injected_db = True`（测试 fake）——
      健康检查判定其 down 时不重连真实 Mongo。
    - `connect()` 成功建连、失败时置 `self._db = None`。
    - `health_check()` 直接 `return await self._health_check()`。
    """

    _db: "DocumentDBBackend | None" = None
    _injected_db: bool = False

    @abstractmethod
    async def connect(self) -> bool:
        """建立连接。

        Returns:
            True 表示连接成功，False 表示失败。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接，释放资源。"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查。

        Returns:
            True 表示可用，False 表示不可用。
        """
        ...

    async def _health_check(self) -> bool:
        """真实健康检查：ping 失败即状态失效并重连（P1-05）。

        覆盖“Motor client 断网后 `_db` 仍非 None”的假阳性：`_db is not None`
        不再代表健康，必须 `ping()` 真实探测。测试注入的 fake 判定 down 时保留
        引用（供 ping_ok 恢复再探测），不重连真实 Mongo。

        Returns:
            True 表示后端可达，False 表示不可用（真实后端已尝试重连）。
        """
        db = self._db
        if db is not None:
            try:
                if await db.ping():
                    return True
            except Exception:
                pass
            if self._injected_db:
                # 测试注入 fake：判定 down，保留引用供恢复，不重连真实 Mongo
                return False
            self._mark_unavailable("健康检查 ping 失败")
        try:
            return await self.connect()
        except Exception:
            return False

    def _mark_unavailable(self, reason: str) -> None:
        """使后端状态失效（P1-05）：下次健康检查/操作触发真正重连。"""
        self._db = None
