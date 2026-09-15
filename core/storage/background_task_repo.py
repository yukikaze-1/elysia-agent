"""BackgroundTaskResultRepository — 后台任务终态结果持久化（#6 / 决策 #6）。

**为什么需要它**（决策 #6：后台任务终态**跨重启不丢**）：

后台任务的终态走「WorkflowEngine → mailbox → Agent 轮询 → EventBus →
`_handle_task_result`（唤醒认知循环告知用户）」这条进程内链路。进程一停，两端都
没了：

- `stop()` 第一行就把 `_state` 置 STOPPED → 轮询循环停转；
- `_stop_mailbox` 的收尾排空虽然把停机窗口内到达的终态事件 `bus.post` 出去，
  **但事件消费者 `_process_events` 早在关停第一步就被取消了**——投进的是死队列，
  用户永远等不到那条"任务完成了"；
- 停机时仍未终态的任务（引擎被 cancel）连终态事件都不存在，只有一条 warning 日志。

于是"派了任务 → 服务重启 → 用户永远不知道结果"是必然，不是概率。

**本仓储的角色**：终态事件在**投递之前**落库（`delivered=False`），被活跃事件循环
真正消费后置 `delivered=True`；启动时把未投递的记录重新投进总线。判据是
`_state == RunState.RUNNING`——轮询路径为真（总线有人读），关停路径为假（总线已死）。

文档 schema：
    {
        task_id,           # WorkflowId（reminder 等自生成 ID 也走这里）
        result,            # 呈现给用户的终态文本
        original_intent,   # 原始意图（唤醒时的上下文）
        dispatched_at,     # 派发时间（原始 ISO，来自后台任务登记）
        is_error,          # 失败/取消/中断为 True（消费侧据此选择措辞）
        status,            # completed / failed / cancelled / interrupted
        delivered,         # 是否已被活跃的 Agent 消费
        created_at,        # 建档时间（ISO UTC）
        delivered_at,      # 置为已投递的时间（ISO UTC）
        ttl_expires_at,    # 真实 BSON Date（TTL 基准，7 天后回收）
    }

索引：task_id unique + ttl_expires_at TTL 7d。
"""

import logging

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.utils import now_iso
from core.schema.types import TaskId
from core.storage.base import Repository, StorageUnavailableError, bson_expires

logger = logging.getLogger(__name__)

COLLECTION = "background_task_results"

#: 终态结果保留 7 天（`delivered=True` 的记录只是审计痕迹，`False` 的应在下次启动
#: 就被投递掉；TTL 是"没人再启动"时的兜底回收，不是正常路径）
BACKGROUND_TASK_RESULT_TTL_SECONDS = 7 * 86400

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
#: 停机时仍未终态的任务——引擎已停，它不会再有别的终态了（#6 的第二半）
STATUS_INTERRUPTED = "interrupted"


class BackgroundTaskResultRepository(Repository):
    """后台任务终态结果存储 — MongoDB background_task_results 集合（fail-closed）。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化仓储。

        Args:
            db: 可选的文档数据库后端；None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名。
            base_url: MongoDB 连接串；None 时从环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name
        self._base_url = base_url
        self._injected_db = db is not None

    # ---- lifecycle ----

    async def connect(self) -> bool:
        """建立连接 + 幂等建索引（task_id unique + TTL）。

        Returns:
            True 表示连接成功。
        """
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create(
                f"mongodb/{self._database_name}",
                base_url=self._base_url,
            )
            await self._db.ensure_index(COLLECTION, [("task_id", 1)], unique=True)
            try:
                await self._db.ensure_index(
                    COLLECTION, [("ttl_expires_at", 1)],
                    expireAfterSeconds=BACKGROUND_TASK_RESULT_TTL_SECONDS)
            except Exception as e:
                logger.warning("BackgroundTaskResultRepository: TTL index failed (%s)", e)
            logger.info("BackgroundTaskResultRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("BackgroundTaskResultRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        """关闭连接。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """真实 ping 健康检查（断线自动重连）。"""
        return await self._health_check()

    def _require_db(self) -> DocumentDBBackend:
        """fail-closed：离线时 raise（调用方决定是"降级继续"还是 503）。

        Raises:
            StorageUnavailableError: MongoDB 未连接。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "background_task_results 集合不可用：MongoDB 未连接（离线）")
        return self._db

    # ---- core operations ----

    async def upsert_pending(
        self, *,
        task_id: TaskId,
        result: str,
        original_intent: str,
        dispatched_at: str,
        status: str,
        is_error: bool,
        delivered: bool = False,
    ) -> None:
        """写入（或刷新）一条终态结果，`delivered` 标记是否已被活跃 Agent 消费。

        以 task_id 为唯一键 upsert——同一任务重复投递（轮询路径 + 关停排空）不会
        产生两条记录；`reminder` 等自生成 ID 与工作流 ID 共用本表。

        Args:
            task_id: 任务 ID。
            result: 呈现给用户的终态文本。
            original_intent: 原始意图。
            dispatched_at: 派发时间（ISO）。
            status: completed / failed / cancelled / interrupted。
            is_error: 失败、取消、中断为 True。
            delivered: 是否已投递给活跃的 Agent（关停路径恒 False）。

        Raises:
            StorageUnavailableError: MongoDB 未连接。
        """
        db = self._require_db()
        await db.update_one(
            COLLECTION,
            {"task_id": task_id},
            {"$set": {
                "task_id": task_id,
                "result": result,
                "original_intent": original_intent,
                "dispatched_at": dispatched_at,
                "status": status,
                "is_error": is_error,
                "delivered": delivered,
                "created_at": now_iso(),
                "ttl_expires_at": bson_expires(7),
            }},
            upsert=True,
        )

    async def mark_delivered(self, task_id: TaskId) -> None:
        """标记已投递给活跃 Agent（不再是"待补投"）。

        Raises:
            StorageUnavailableError: MongoDB 未连接。
        """
        db = self._require_db()
        await db.update_one(
            COLLECTION,
            {"task_id": task_id},
            {"$set": {"delivered": True, "delivered_at": now_iso()}},
        )

    async def list_pending(self, *, limit: int = 100) -> list[dict]:
        """未投递的终态结果（按建档时间升序 = 派发顺序）。

        Args:
            limit: 单次返回上限（按时间升序取最早的 N 条，避免大批量一次性唤醒）。

        Returns:
            记录列表（无则空列表）。

        Raises:
            StorageUnavailableError: MongoDB 未连接。
        """
        db = self._require_db()
        docs = await db.find(COLLECTION, {"delivered": False}, limit=limit)
        return sorted(docs, key=lambda d: str(d.get("created_at") or ""))

    async def count_pending(self) -> int:
        """未投递记录数（启动日志/诊断用）。

        Raises:
            StorageUnavailableError: MongoDB 未连接。
        """
        db = self._require_db()
        return len(await db.find(COLLECTION, {"delivered": False}, limit=1000))
