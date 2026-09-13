"""GenerationRegistry — generation 生命周期持久化的薄封装。

批次 3（BE-024）：把 CognitiveLoop 的 register/mark 调用转译为 Mongo 写入。
核心约束：Mongo 离线必须不阻塞 generation 本身——所有方法内部 try/except，
失败仅记录 debug 日志降级为 no-op。register 由 loop 同步 await（保证建档顺序），
mark 为 upsert（mark-before-register 仍能建档）。
"""

import logging

from core.schema.types import GenerationId, MessageId, RequestId, SessionId
from core.schema.utils import now_iso
from core.storage.generation_repo import (
    GenerationRepository,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
)

logger = logging.getLogger(__name__)

# 终态集合：落位时同时写 finished_at
_TERMINAL_STATUSES = {
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
}


class GenerationRegistry:
    """generation 生命周期注册表（Mongo 持久化，Mongo 离线自动降级为 no-op）。"""

    def __init__(self, repo: GenerationRepository | None = None):
        """初始化注册表。

        Args:
            repo: 可选的 GenerationRepository，为 None 时自建（db 懒连接，离线安全）。
        """
        self._repo = repo if repo is not None else GenerationRepository()

    async def connect(self) -> bool:
        """建立底层 GenerationRepository 连接（建索引 + TTL）。

        review 修复（兼容#2）：agent 启动时调用——此前 loop 自建 registry 的 repo
        db=None，upsert 全量 no-op；此处连接后 register/mark 真正落库。

        Returns:
            True 表示连接成功，False 表示失败（Mongo 离线，register/mark 继续降级 no-op）。
        """
        try:
            return await self._repo.connect()
        except Exception as e:
            logger.debug("GenerationRegistry.connect 失败: %s", e)
            return False

    async def health_check(self) -> bool:
        """底层 GenerationRepository 健康检查（P1-05 恢复协调器用）。"""
        try:
            return await self._repo.health_check()
        except Exception:
            return False

    async def register(self, generation_id: GenerationId, *, session_id: SessionId,
                       user_message_id: MessageId, message_id: MessageId, model: str,
                       request_id: RequestId | None = None) -> None:
        """generation 开始建档（status=running）。

        Mongo 离线/异常不抛——仅日志，绝不阻塞 generation。

        Args:
            generation_id: 生成实例 ID（gen_ 前缀）。
            session_id: 目标会话 ID。
            user_message_id: 触发生成的用户消息 ID。
            message_id: assistant 父消息 ID（落库一致）。
            model: 生成所用模型。
            request_id: 关联的 WS 侧请求 ID（批次 7，来源 UserInputData.request_id），无则 None。
        """
        if not generation_id:
            return
        now = now_iso()
        doc = {
            "generation_id": generation_id,
            "session_id": session_id,
            "user_message_id": user_message_id,
            "message_id": message_id,
            "model": model,
            "status": STATUS_RUNNING,
            "finish_reason": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        if request_id is not None:
            doc["request_id"] = request_id
        try:
            await self._repo.upsert(doc)
        except Exception as e:
            logger.debug("GenerationRegistry.register 失败（Mongo 离线？）: %s", e)

    async def mark(self, generation_id: GenerationId, *, status: str,
                   finish_reason: str | None = None,
                   error: str | dict | None = None) -> None:
        """更新 generation 状态（upsert 语义，mark-before-register 也建档）。

        终态（completed/failed/cancelled/interrupted）同时落 finished_at。

        Args:
            generation_id: 生成实例 ID。
            status: 目标状态（running/completed/failed/cancelled/interrupted）。
            finish_reason: 完成原因（stop/error），非 None 时写入。
            error: 错误信息（status=failed 时）。批次 8c（F6）：接受结构化 dict
                {"code","message","retryable"} 原样落库（向前兼容字符串）。
        """
        if not generation_id:
            return
        now = now_iso()
        doc = {
            "generation_id": generation_id,
            "status": status,
            "updated_at": now,
        }
        if finish_reason is not None:
            doc["finish_reason"] = finish_reason
        if error is not None:
            doc["error"] = error
        if status in _TERMINAL_STATUSES:
            doc["finished_at"] = now
        try:
            await self._repo.upsert(doc)
        except Exception as e:
            logger.debug("GenerationRegistry.mark 失败（Mongo 离线？）: %s", e)

    async def get(self, generation_id: GenerationId) -> dict | None:
        """按 generation_id 取文档。异常/离线返回 None。"""
        try:
            return await self._repo.find_one(generation_id)
        except Exception as e:
            logger.debug("GenerationRegistry.get 失败: %s", e)
            return None

    async def query_by_session(self, session_id: SessionId, *, status: str | None = None,
                               cursor: str | None = None,
                               limit: int = 20) -> tuple[list[dict], bool, str | None]:
        """按 session 查询 generation（支持状态过滤 + keyset 分页）。

        Returns:
            (docs, has_more, next_cursor)。异常/离线返回 ([], False, None)。
        """
        try:
            return await self._repo.find_by_session(
                session_id, status=status, cursor=cursor, limit=limit)
        except Exception as e:
            logger.debug("GenerationRegistry.query_by_session 失败: %s", e)
            return [], False, None

    async def find_by_message(self, message_id: MessageId) -> dict | None:
        """按 assistant 父消息 ID 反查最近一条 generation（D-q 搜索用）。"""
        try:
            return await self._repo.find_by_message(message_id)
        except Exception as e:
            logger.debug("GenerationRegistry.find_by_message 失败: %s", e)
            return None

    async def find_by_user_message(self, user_message_id: MessageId) -> dict | None:
        """按 user_message_id 反查最近一条 generation（幂等 commit 证据，Task 3/4）。

        恢复 stale CLAIMED 时的判定依据：存在 generation 记录 → 该用户消息已处理。

        **不吞异常**（S2-B6）：原实现在异常/离线时返回 None，与"确实没有 generation"
        同形，调用方据此 TAKEOVER 重新处理 → 重复 generation。存储不可用时必须让调用方
        看见（`StorageUnavailableError` → 可重试拒绝），而不是替它下结论。

        Raises:
            StorageUnavailableError: 存储不可用 / 查询失败。
        """
        return await self._repo.find_by_user_message(user_message_id)

    async def mark_stale_interrupted(self, cutoff_iso: str) -> int:
        """把 cutoff 之前仍 running 的 generation 标为 interrupted（崩溃残留清理）。

        Args:
            cutoff_iso: 过期阈值（ISO UTC）。

        Returns:
            修改的文档数。异常/离线返回 0。
        """
        try:
            return await self._repo.mark_stale(STATUS_RUNNING, cutoff_iso)
        except Exception as e:
            logger.debug("GenerationRegistry.mark_stale_interrupted 失败: %s", e)
            return 0
