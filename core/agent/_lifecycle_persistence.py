"""Agent 生命周期 — 持久化层 (MongoDB/Milvus/session/repos) 启停。"""
from __future__ import annotations

import asyncio
import os
import logging
from typing import TYPE_CHECKING

from Module.embedding import EmbeddingFactory

if TYPE_CHECKING:
    from core.storage.character_version_repo import CharacterVersionRepository
    from core.storage.message_repo import MessageRepository
    from core.storage.session_repo import SessionRepository
    from core.storage.token_repo import TokenRepository
    from ..memory.session import SessionMemory
    from ..memory.long_term import LongTermMemory
    from ..personality import CharacterEngine

logger = logging.getLogger(__name__)


class _LifecyclePersistence:
    """持久化层生命周期 mixin。"""

    if TYPE_CHECKING:
        _db_health_task: asyncio.Task[None] | None
        _session: SessionMemory
        _long_term: LongTermMemory
        _message_repo: MessageRepository
        _version_repo: CharacterVersionRepository
        _character_engine: CharacterEngine
        _mongo_ok: bool
        _milvus_ok: bool
        _mongo_available: bool
        _milvus_available: bool
        _token_repo: TokenRepository
        _session_repo: SessionRepository

    def _start_db_health(self) -> None:
        """启动数据库健康检查后台任务。"""
        self._db_health_task = asyncio.create_task(self._db_health_loop())

    async def _start_persistence(self) -> tuple[bool, bool]:
        """连接 MongoDB/Milvus，加载 session，恢复 JSONL 快照。

        Returns:
            (mongo_ok, milvus_ok) 两个布尔值，分别表示 MongoDB 和 Milvus 是否连接成功。
        """
        mongo_ok, milvus_ok = await asyncio.gather(
            self._session.connect_db(),
            self._long_term.connect(),
        )
        # 消息归档独立连接（与 session 共享 MongoDB，但使用独立 repo）
        await self._message_repo.connect()
        # L2 版本历史
        if not await self._version_repo.connect():
            logger.warning("CharacterVersionRepository 离线")
        if mongo_ok:
            await self._session.load()
            # CharacterState 已由 session.load() 原地恢复到 engine.state
            if self._character_engine.state.last_awareness_time:
                self._character_engine._last_awareness_time = self._character_engine.state.last_awareness_time
            self._character_engine._reflector._restore_from_state()
            # MongoDB 离线期间 session 可能有更新的 JSONL 快照，恢复到 MongoDB
            if os.path.exists(self._session.pending_file):
                if await self._session.load_from_jsonl(merge=True):
                    if await self._session.save():
                        os.remove(self._session.pending_file)
                        logger.info("Session JSONL 已导入 MongoDB 并删除")
                    else:
                        logger.warning("Session MongoDB 写入失败，保留 JSONL")
        if not mongo_ok:
            logger.warning("MongoDB 不可用，会话记忆仅使用内存+JSONL 持久化")
        if not milvus_ok:
            logger.warning("Milvus 不可用，长期记忆检索降级为纯本地匹配")
        self._mongo_ok = mongo_ok
        self._milvus_ok = milvus_ok
        return mongo_ok, milvus_ok

    async def _db_health_loop(self) -> None:
        """每 120s 检查数据库连接，失败时尝试自动重连。"""
        while True:
            await asyncio.sleep(120)
            try:
                mongo_ok = await self._session.health_check()
                milvus_ok = await self._long_term.health_check()
                self._mongo_available = mongo_ok
                self._milvus_available = milvus_ok
                # 检查其他 repo 连接状态（不阻塞，仅日志）
                if not await self._message_repo.health_check():
                    logger.debug("MessageRepository 离线")
                if not await self._token_repo.health_check():
                    logger.debug("TokenRepository 离线")
            except Exception:
                logger.debug("Database health check failed", exc_info=True)

    async def _stop_db_health(self) -> None:
        """停止数据库健康检查循环。"""
        if self._db_health_task:
            logger.debug("Cancelling db health task...")
            self._db_health_task.cancel()
            try:
                await asyncio.wait_for(self._db_health_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._db_health_task = None

    async def _stop_persistence(self) -> None:
        """持久化关闭：session.save() → long_term.close() → EmbeddingFactory.shutdown()。

        顺序要求：先 save session（需要 L2 连接仍然有效），再关闭 L2，最后关 embedding。
        """
        logger.debug("Saving session...")
        try:
            await asyncio.wait_for(self._session.save(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Session save timed out")
        except Exception:
            logger.error("Session save failed on shutdown", exc_info=True)

        logger.debug("Closing long-term memory...")
        try:
            await asyncio.wait_for(self._long_term.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Long-term memory close timed out")

        logger.debug("Closing session repo...")
        try:
            await asyncio.wait_for(self._session_repo.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Session repo close timed out")

        logger.debug("Closing message repo...")
        try:
            await asyncio.wait_for(self._message_repo.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Message repo close timed out")

        logger.debug("Closing token repo...")
        try:
            await asyncio.wait_for(self._token_repo.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Token repo close timed out")

        logger.debug("Closing version repo...")
        try:
            await asyncio.wait_for(self._version_repo.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Version repo close timed out")

        logger.debug("Shutting down embedding factory...")
        try:
            await asyncio.wait_for(EmbeddingFactory.shutdown(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Embedding factory shutdown timed out")
