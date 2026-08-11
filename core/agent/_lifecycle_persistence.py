"""Agent 生命周期 — 持久化层 (MongoDB/Milvus/session/repos) 启停。"""
from __future__ import annotations

import asyncio
import datetime
import os
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from Module.embedding import EmbeddingFactory

from core.schema.adapters import attachment_resolver
from core.storage.attachment_gc import run_attachment_gc
from core.storage.attachment_repo import STATUS_ACTIVE, StorageUnavailableError

if TYPE_CHECKING:
    from core.storage.character_version_repo import CharacterVersionRepository
    from core.storage.message_repo import MessageRepository
    from core.storage.session_repo import SessionRepository
    from core.storage.token_repo import TokenRepository
    from core.storage.attachment_repo import AttachmentRepository
    from core.storage.idempotency_repo import IdempotencyRepository
    from core.memory.session_manager import SessionManager
    from core.memory.long_term import LongTermMemory
    from core.personality import CharacterEngine
    from core.cognitive.generation_registry import GenerationRegistry
    from core.desktop.bridge import DesktopToolBridge

logger = logging.getLogger(__name__)


class _LifecyclePersistence:
    """持久化层生命周期 mixin。"""

    if TYPE_CHECKING:
        _db_health_task: asyncio.Task[None] | None
        _attachment_gc_task: asyncio.Task[None] | None
        _session_manager: SessionManager
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
        _attachment_repo: AttachmentRepository | None
        attachment_repo: AttachmentRepository  # 类型占位：运行时来自 Agent @property（lazy 构造，首访必非 None）
        _idempotency_repo: IdempotencyRepository | None
        idempotency_repo: IdempotencyRepository  # 类型占位：运行时来自 Agent @property（D6c，首访必非 None）
        _generation_registry: GenerationRegistry
        _desktop_bridge: DesktopToolBridge

    def _start_db_health(self) -> None:
        """启动数据库健康检查后台任务。"""
        self._db_health_task = asyncio.create_task(self._db_health_loop())

    async def _start_persistence(self) -> tuple[bool, bool]:
        """连接 MongoDB/Milvus，加载活动会话，恢复 JSONL 快照。

        Returns:
            (mongo_ok, milvus_ok) 两个布尔值，分别表示 MongoDB 和 Milvus 是否连接成功。
        """
        mongo_ok, milvus_ok = await asyncio.gather(
            self._session_manager.connect_db(),
            self._long_term.connect(),
        )
        # 消息归档独立连接（与 session 共享 MongoDB，但使用独立 repo）
        await self._message_repo.connect()
        # Token 用量独立连接——此前从未 connect（_db 恒 None → 健康循环每 120s
        # 刷 "TokenRepository 离线" WARN，且 record() 静默丢弃 token 记录）。
        # 与 message/session repo 对称：失败记 warning、不阻塞启动。
        if not await self._token_repo.connect():
            logger.warning("TokenRepository 离线")
        # 批次 4：Desktop 调用注册表建连（desktop_calls 集合）。
        # bridge 在 Agent.__init__ 创建（早于 _start_persistence），此处 connect_db
        # 全量 reload 入进程内缓存；Mongo 离线降级为进程内缓存（幂等去重不失效）。
        # try/except 沿用 generation_registry 先例——connect_db 失败不阻断 Agent 启动。
        desktop_bridge = getattr(self, "_desktop_bridge", None)
        if desktop_bridge is not None:
            try:
                await desktop_bridge.connect_db()
            except Exception:
                logger.warning("DesktopCallRepository 建连失败（降级为进程内缓存）", exc_info=True)
        # L2 版本历史
        if not await self._version_repo.connect():
            logger.warning("CharacterVersionRepository 离线")
        # D6c：幂等 claim 仓库建连（失败记警告不阻塞启动——产品模式幂等将
        # fail-closed/503，dev 模式走内存降级；Mongo 恢复由健康循环重连）
        try:
            if not await self.idempotency_repo.connect():
                logger.warning("IdempotencyRepository 离线（幂等走内存降级/产品 fail-closed）")
        except Exception:
            logger.warning("IdempotencyRepository 建连失败", exc_info=True)
        # 批次 3（review 修复，兼容#2）：generation registry 建连 + 崩溃残留清理。
        # 此前 loop 自建 registry 的 repo db=None，register/mark 全量 no-op；此处 connect
        # 后 register/mark 真正落库。connect 成功后再把启动时刻 5 分钟前仍 running 的
        # 旧 generation 标为 interrupted（崩溃残留清理）。
        gen_registry = getattr(self, "_generation_registry", None)
        if gen_registry is not None:
            try:
                if await gen_registry.connect():
                    cutoff = (datetime.datetime.now(datetime.timezone.utc)
                              - datetime.timedelta(minutes=5)).isoformat()
                    await gen_registry.mark_stale_interrupted(cutoff)
            except Exception:
                logger.warning("GenerationRegistry 建连/崩溃残留清理失败", exc_info=True)
        # 无条件 load_active：确保默认会话进入 _loaded（shutdown save_all() 依赖，
        # 否则 MongoDB 离线期间的内存会话在关闭时不会落 JSONL）
        await self._session_manager.load_active()
        # B2/P2-01：扫描所有 pending JSONL 会话（非活动也在内），Mongo 可用时导入
        try:
            recovered = await self._session_manager.recover_pending_sessions(
                import_to_mongo=mongo_ok)
            if recovered:
                logger.info("SessionManager 恢复 %d 个 pending 会话", recovered)
        except Exception:
            logger.warning("pending 会话扫描/导入失败", exc_info=True)
        if mongo_ok:
            # CharacterState 已由 load() 原地恢复到 engine.state（active=default）
            if self._character_engine.state.last_awareness_time:
                self._character_engine._last_awareness_time = self._character_engine.state.last_awareness_time
            self._character_engine._reflector._restore_from_state()
            # MongoDB 离线期间 session 可能有更新的 JSONL 快照，恢复到 MongoDB
            active = self._session_manager.active
            if os.path.exists(active.pending_file):
                if await active.load_from_jsonl(merge=True):
                    if await active.save():
                        # save() 在 Mongo durable 成功后已删除 pending；若仍存在
                        # 说明走了 JSONL fallback（Mongo 写入失败），保留数据
                        if not os.path.exists(active.pending_file):
                            logger.info("Session JSONL 已导入 MongoDB 并删除")
                        else:
                            logger.warning("Session MongoDB 写入失败，保留 JSONL")
                    else:
                        logger.warning("Session MongoDB 写入失败，保留 JSONL")
        if not mongo_ok:
            logger.warning("MongoDB 不可用，会话记忆仅使用内存+JSONL 持久化")
        if not milvus_ok:
            logger.warning("Milvus 不可用，长期记忆检索降级为纯本地匹配")
        self._mongo_ok = mongo_ok
        self._milvus_ok = milvus_ok
        return mongo_ok, milvus_ok

    async def _start_attachment_repo(self) -> None:
        """连接附件仓库（批次 7 Task 2）。

        repo 由 Agent.attachment_repo property lazy 构造（plan review C2 归属
        定一）。失败记 warning 不阻塞 Agent 启动——附件操作 fail-closed：
        MongoDB 离线时读写 raise StorageUnavailableError（503 STORAGE_UNAVAILABLE），
        不做静默 no-op。
        """
        try:
            if not await self.attachment_repo.connect():
                logger.warning("AttachmentRepository 离线（附件操作将 fail-closed）")
        except Exception:
            logger.warning("AttachmentRepository 建连失败（附件操作将 fail-closed）",
                           exc_info=True)

    async def _start_attachment_gc(self) -> None:
        """启动附件 GC 兜底扫描（批次 7 Task 7，置于 attachment_repo 建连步骤后）。

        先预热 AttachmentFileResolver（查一批 active 记录填充 ID→路径映射），随后
        fire-and-forget 后台任务执行 run_attachment_gc——不阻塞启动。Mongo 离线时
        find_by_status raise StorageUnavailableError → 跳过预热与扫描（记日志）。
        """
        repo = getattr(self, "_attachment_repo", None)
        if repo is None:
            return
        try:
            active = await repo.find_by_status(STATUS_ACTIVE)
            if active:
                attachment_resolver.load(active)
                logger.info("AttachmentFileResolver 预热 %d 条 active 附件记录", len(active))
        except StorageUnavailableError:
            logger.warning("AttachmentRepository 离线——跳过 resolver 预热与 GC 扫描")
            return
        except Exception:
            logger.warning("附件记录查询失败——跳过 resolver 预热", exc_info=True)
            return
        self._attachment_gc_task = asyncio.create_task(
            self._attachment_gc_runner(), name="attachment-gc")

    async def _attachment_gc_runner(self) -> None:
        """后台执行附件 GC（fire-and-forget；异常不阻断启动/关停）。"""
        repo = getattr(self, "_attachment_repo", None)
        if repo is None:
            return
        try:
            stats = await run_attachment_gc(repo)
            logger.info("附件 GC 扫描完成: %s", stats)
        except StorageUnavailableError:
            logger.warning("附件 GC 跳过：MongoDB 离线")
        except Exception:
            logger.warning("附件 GC 扫描失败", exc_info=True)

    async def _stop_attachment_gc(self) -> None:
        """取消并等待附件 GC 后台任务（置于 _stop_attachment_repo 前）。"""
        task = getattr(self, "_attachment_gc_task", None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        self._attachment_gc_task = None

    async def _db_health_loop(self) -> None:
        """统一恢复协调器：每 60s 检查全部存储依赖，恢复边界触发补偿 hook（P1-05）。

        覆盖 session/message/token/attachment/version/generation/desktop/LTM 全部
        repo——不再只检查前四类。每个 repo 的 health_check 为真实 ping（断网即时
        感知，`_db is not None` 假阳性已消除）。False→True 恢复边界触发各 repo
        补偿 hook（session 加载 pending、LTM 同步脏条目、generation 崩溃残留清理）。

        Raises:
            单轮探测失败仅记日志，不中断循环。
        """
        prev: dict[str, bool] = {}
        while True:
            await asyncio.sleep(60)
            try:
                probes: dict[str, Callable[[], Awaitable[bool]]] = {
                    "session": self._session_manager.health_check,
                    "ltm": self._long_term.health_check,
                    "message": self._message_repo.health_check,
                    "token": self._token_repo.health_check,
                    "version": self._version_repo.health_check,
                    "idempotency": self.idempotency_repo.health_check,
                }
                attachment = getattr(self, "_attachment_repo", None)
                if attachment is not None:
                    probes["attachment"] = attachment.health_check
                gen_registry = getattr(self, "_generation_registry", None)
                if gen_registry is not None:
                    probes["generation"] = gen_registry.health_check
                desktop_bridge = getattr(self, "_desktop_bridge", None)
                if desktop_bridge is not None:
                    probes["desktop"] = desktop_bridge.storage_health_check

                results: dict[str, bool] = {}
                for name, probe in probes.items():
                    try:
                        results[name] = bool(
                            await asyncio.wait_for(probe(), timeout=3.0))
                    except Exception:
                        results[name] = False

                self._mongo_available = bool(
                    results.get("session") and results.get("message"))
                self._milvus_available = bool(results.get("ltm"))

                # 恢复边界（False→True）→ 各 repo 补偿 hook；hook 失败保留 prev=False 下轮重试
                for name, ok in results.items():
                    if not ok:
                        continue
                    if name not in prev or prev[name]:
                        continue
                    if not await self._recover_dependency(name):
                        results[name] = False  # 补偿未完成 → 下轮重试
                prev = results

                if not results.get("session", False):
                    logger.warning("Session/Mongo 离线")
            except Exception:
                logger.warning("Database health check failed", exc_info=True)

    async def _recover_dependency(self, name: str) -> bool:
        """依赖恢复补偿 hook（False→True 边界触发）。

        Args:
            name: 依赖名（session/ltm/generation 等）。

        Returns:
            True 表示补偿完成或无需补偿；False 表示补偿失败（协调器下轮重试）。
        """
        try:
            if name == "ltm":
                # Milvus 恢复：同步此前仅在内存（_milvus_pk=None）的脏记忆
                synced = await self._long_term.sync_dirty_entries()
                if synced:
                    logger.info("Milvus 恢复，同步 %d 条脏记忆", synced)
            elif name == "session":
                # Session/Mongo 恢复：加载活动会话（pending JSONL 导入由 B2 加强）
                await self._session_manager.load_active()
                # review 复核 #5 D2：重试「删除已提交但 refs/归档未清」的待清理会话
                await self._session_manager.retry_pending_cleanup()
                # clean-start 会话发现（BE-P1-05）：repo 已连接，best-effort 确保
                # default 文档存在（$setOnInsert——不覆盖既有会话上下文，重启不清空
                # LLM 上下文窗口）。仅在恢复边界调用：__init__ 时 repo 未连接无法落库。
                await self._session_manager.ensure_default_persisted()
                logger.info("Session/Mongo 恢复，加载活动会话")
            elif name == "generation":
                # generation 恢复：清理崩溃残留 running（5 分钟前）
                gen_registry = getattr(self, "_generation_registry", None)
                if gen_registry is not None:
                    cutoff = (datetime.datetime.now(datetime.timezone.utc)
                              - datetime.timedelta(minutes=5)).isoformat()
                    await gen_registry.mark_stale_interrupted(cutoff)
            elif name == "idempotency":
                # 幂等恢复（D6c）：重连 repo → store.recover() reconcile（内存→Mongo
                # 对齐，durable 翻回）。connect 失败返回 False——协调器保留 prev=False
                # 下轮重试。get_store 惰性 import：core.agent → core.input 循环导入规避
                # （先例：capabilities.py:79 函数内 import）。
                if not await self.idempotency_repo.connect():
                    return False
                from core.input.idempotency import get_store
                await get_store().recover()
                logger.info("IdempotencyRepository 恢复，幂等表 reconcile 完成")
            # 其余 repo：health_check 内部已重连，无需额外补偿
            return True
        except Exception:
            logger.warning("依赖恢复 hook 失败: %s", name, exc_info=True)
            return False

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
            await asyncio.wait_for(self._session_manager.save_all(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Session save timed out")
        except Exception:
            logger.error("Session save failed on shutdown", exc_info=True)

        # B3/P1-02：关闭归档入口并 drain 全部在途归档任务，必须在 message_repo.close()
        # 之前——否则在途 append 会在 repo 关闭后静默丢失（shutdown 丢消息）。
        try:
            await asyncio.wait_for(self._session_manager.drain_all_archives(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Session archive drain timed out")
        except Exception:
            logger.error("Session archive drain failed on shutdown", exc_info=True)

        # 批次 4：Desktop 桥保存（no-op 预留——repo 即时 upsert、无内存脏集可 flush）
        desktop_bridge = getattr(self, "_desktop_bridge", None)
        if desktop_bridge is not None:
            try:
                await asyncio.wait_for(desktop_bridge.save_all(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Desktop bridge save timed out")
            except Exception:
                logger.error("Desktop bridge save failed on shutdown", exc_info=True)

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

        # D6c：幂等 claim 仓库关闭（try/except 包裹——close 异常不阻断关停；重启后
        # 经 connect 可重连）
        logger.debug("Closing idempotency repo...")
        try:
            await asyncio.wait_for(self.idempotency_repo.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Idempotency repo close timed out")
        except Exception:
            logger.warning("Idempotency repo close failed", exc_info=True)

        logger.debug("Shutting down embedding factory...")
        try:
            await asyncio.wait_for(EmbeddingFactory.shutdown(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Embedding factory shutdown timed out")

    async def _stop_attachment_repo(self) -> None:
        """关闭附件仓库连接（幂等；未初始化则跳过）。"""
        repo = getattr(self, "_attachment_repo", None)
        if repo is None:
            return
        try:
            await asyncio.wait_for(repo.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Attachment repo close timed out")
        except Exception:
            logger.warning("Attachment repo close failed", exc_info=True)
