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

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, TypeVar

if TYPE_CHECKING:
    from Module.database.documentdb import DocumentDBBackend

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def bson_expires(days: int, *, now: datetime | None = None) -> datetime:
    """返回 BSON Date 兼容的过期时间戳（aware UTC datetime）。

    B6/P2-01：Mongo TTL 只扫描 BSON Date 字段——ISO 字符串永不触发过期。
    临时记录（generations/desktop_calls）的 TTL 基准必须用本函数生成的 datetime，
    落库即 BSON Date，TTL 索引才真正生效。

    Args:
        days: 保留天数。
        now: 基准时刻（测试注入），默认当前 UTC。

    Returns:
        aware UTC datetime（now + days）。
    """
    base = now if now is not None else datetime.now(timezone.utc)
    return base + timedelta(days=days)


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

    @property
    def is_connected(self) -> bool:
        """当前是否已持有后端连接（``_db`` 非空）。

        注意：不代表真实可用——Motor client 断网后 ``_db`` 仍非 None（假阳性）。
        健康判断用 ``health_check()`` 真实 ping；本属性供 durable 初值判定等
        轻量场景（D5-b：configure 初值 ``repo is None or not repo.is_connected``）。
        """
        return self._db is not None

    async def _call(
        self,
        coro_factory: Callable[[], Awaitable[_T]],
        *,
        reason: str = "存储操作失败",
        non_fatal: Callable[[BaseException], bool] | None = None,
    ) -> _T:
        """执行后端操作，连接异常 → StorageUnavailableError + 状态失效（review 复核 #2）。

        B1 之后只有健康检查经 `_mark_unavailable` 感知断线；find/insert/update 抛出的
        连接异常（Motor ServerSelectionTimeoutError 等）会以普通异常冒泡，被 caller
        当作「not found / 存储为空」——session 假加载即源于此。本 helper 把后端操作的
        真实失败转换为统一 StorageUnavailableError 并置 `_db=None`，触发下一轮健康检查
        真正重连（fail-closed，禁止静默 return 被 caller 当成功）。

        Args:
            coro_factory: 返回 awaitable 的零参 callable（惰性求值，避免异常发生在
                await 之前被误判）。
            reason: 日志/错误描述（标记状态失效的原因）。
            non_fatal: 业务冲突谓词（如 DuplicateKeyError）。命中时**裸抛原始异常**——
                不 `_mark_unavailable()`、不包装 StorageUnavailableError（并发同键
                claim 的 winner 分支依赖原始 driver 异常类名判定，见 D6/D6-d）。

        Returns:
            后端操作返回值。

        Raises:
            StorageUnavailableError: 后端操作抛异常（连接失败等）；non_fatal 命中除外。
        """
        try:
            return await coro_factory()
        except StorageUnavailableError:
            raise
        except Exception as e:
            if non_fatal is not None and non_fatal(e):
                # 业务冲突（非存储故障）：不判死、不包装——裸抛原始异常，由调用方分支
                raise
            logger.warning("%s: %s", reason, e)
            if not self._injected_db:
                self._mark_unavailable(reason)
            raise StorageUnavailableError(reason) from e
