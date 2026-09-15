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
import re
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


def is_duplicate_key(exc: BaseException) -> bool:
    """是否唯一索引冲突（并发同键双写）——`_call(non_fatal=...)` 的谓词。

    **为什么它是"良性"**：唯一索引在正确工作时，并发写同一键**必然**有一个 loser。
    把它当存储故障会 `_mark_unavailable()` 判死整个仓储（此后所有读写都 raise，
    直到 60s 健康轮重连），本应返回 409 的竞争变成 503。

    驱动无关：检查异常类名（含 `__cause__` 链）。`Repository._call` 在 `non_fatal`
    命中时**裸抛原始异常**（不包装），raw driver 异常的 `__cause__` 为 None，
    只查 cause 会漏判，故两处都查。

    （原私有 `idempotency_repo._is_duplicate_key` 下沉至此：现在三个角色仓储也要用它，
    放在基类旁边比跨模块导入私有名更合适。）
    """
    if getattr(type(exc), "__name__", "") == "DuplicateKeyError":
        return True
    cause = getattr(exc, "__cause__", None)
    return getattr(type(cause), "__name__", "") == "DuplicateKeyError"


#: 搜索词长度上限——与作者搜索（`MessageRepository.query_search`）既有口径一致。
MAX_SEARCH_LEN = 200


def search_regex_filter(value: str, *, options: str = "i") -> dict | None:
    """构造**已转义**的 `$regex` 子句；空值或超长返回 None（调用方按"无匹配"处理）。

    **为什么要收口（S2-B7c）**：本层有三处"用户输入直接进 `$regex`"的写法，此前只有
    `MessageRepository._build_search_filter` 转了义，另两处（debug 归档搜索、
    角色列表搜索）会把用户输入当**正则**执行——元字符要么改变语义（`a.b` 匹配 `axb`），
    要么构成 ReDoS 面（嵌套量词 + 大量回溯）。收成一处，新增搜索路径不会再各写一遍。
    """
    if not value or len(value) > MAX_SEARCH_LEN:
        return None
    return {"$regex": re.escape(value), "$options": options}


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

    @property
    def backend(self) -> "DocumentDBBackend | None":
        """已连接的后端句柄（None 表示未连接）。

        公开访问器：组合根与只读自检（如 `core/storage/schema_drift.py` 列索引）
        需要共享同一连接，避免为诊断再开一条 Mongo 连接。
        """
        return self._db

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
            await self._mark_unavailable("健康检查 ping 失败")
        try:
            return await self.connect()
        except Exception:
            return False

    async def _mark_unavailable(self, reason: str) -> None:
        """使后端状态失效（P1-05）：先释放旧句柄，再让下次调用真正重连。

        S2-B5：**先 close 再置 None**。原先只做 `self._db = None`，旧客户端失去
        唯一引用却从未 `close()`——Mongo 侧 motor/pymongo 有 `__del__` 兜底，
        **Milvus 侧 pymilvus 3.0.1 的 `MilvusClient` 整条 MRO 都没有 `__del__`**，
        于是 gRPC channel 与线程池永不释放：Milvus 持续不可达时每次健康检查
        （默认 60s 一轮）泄漏一个客户端，8 小时宕机 ≈ 480 个孤儿。
        `MilvusBackend.close()` 的 docstring 早就写明这个风险，只是这条路径没走它。

        **是 async 方法**（两个调用点都在协程里）：释放客户端天然是异步的，
        做成同步就只能 `create_task` 火忘——那正是 R6 门禁禁止的裸后台任务，
        且失败无人观察、关停时无处 drain。
        """
        db, self._db = self._db, None
        if db is None:
            return
        close = getattr(db, "close", None)
        if close is None:
            return
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            # 释放失败不改变"已判定不可用"这一事实，但也不能静默——
            # 这正是 S2-B5 要修的资源泄漏面。
            logger.warning("释放不可用后端失败（%s）", reason, exc_info=True)

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
                await self._mark_unavailable(reason)
            raise StorageUnavailableError(reason) from e
