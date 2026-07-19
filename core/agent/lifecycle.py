"""Agent 生命周期 — start/stop 编排及所有 _start_*/_stop_*/_flush_* 方法。"""
from __future__ import annotations

import asyncio
import os
import time
import logging

from ._types import RunState

from Module.output import OutputManager, ConsoleChannel

from ..app_config import AppConfig
from ..event_bus import EventBus, EventPriority, EventType
from ..agent_fsm import AgentFSM, AgentState
from ..log_setup import set_request_id, get_request_id
from ..character_config import CharacterConfig
from ..memory.session import SessionMemory
from ..memory.long_term import LongTermMemory
from ..prompt_assembler import PromptAssembler
from Module.tool import ToolManager, ToolContext, PluginContext
from core.tools import ALL_PLUGINS
from ..cognitive import CognitiveLoop
from ..idle_monitor import IdleMonitor
from ..adapter.llm_adapter import LLMAdapter
from ..memory.consolidator import MemoryConsolidator
from ..personality import CharacterEngine
from core.personality.reflector import CharacterReflector
from ..memory.memory_reflector import MemoryReflector  # 新增
from core.schema.types import IsoTimestamp, SessionId
from core.schema.character import CharacterState
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
from Module.embedding import EmbeddingFactory
from ..task_scheduler import TaskScheduler
from core.storage.session_repo import SessionRepository
from core.storage.message_repo import MessageRepository
from core.storage.token_repo import TokenRepository
from core.storage.memory_repo import MemoryRepository

logger = logging.getLogger(__name__)


class AgentLifecycle:
    """Agent 生命周期 mixin — start/stop 编排。

    所有属性由 Agent.__init__ 初始化，本 mixin 不含 __init__。
    """

    async def start(self) -> None:
        """Start the agent: connect persistence, idle monitor, event loop."""
        self._state = RunState.RUNNING
        self._started_at = time.monotonic()  # 启动时刻打点，用于 get_runtime_config 显示运行时长

        # Reset FSM — may be stuck from a previous run that was interrupted
        self._fsm.reset()

        # Recreate LLM HTTP client (may have been closed by previous stop())
        await self._llm.reconnect()

        # 1. 持久化连接 + session 加载
        mongo_ok, milvus_ok = await self._start_persistence()

        # 2. Token + Prompt 统计
        await self._start_token_and_prompt(mongo_ok)

        # 3. 工具上下文 + 插件加载
        await self._start_tool_context()

        # 4. Worker Pool + embedding 预加载
        await self._start_worker_pool()

        # 5. IdleMonitor + 主事件循环
        await self._start_idle_and_events()

        # 6. TTS（条件启动）
        await self._start_tts()

        # 7. TaskSystem + DelegationPreparer
        await self._start_task_system()

        # 8. Mailbox 轮询
        self._start_mailbox_polling()

        # 9. 数据库健康检查循环（120s 间隔自动重连）
        self._db_health_task = asyncio.create_task(self._db_health_loop())

        # 10. 保存启动状态供入口打印横幅
        self._mongo_ok = mongo_ok
        self._milvus_ok = milvus_ok

    async def _start_persistence(self) -> tuple[bool, bool]:
        """连接 MongoDB/Milvus，加载 session，恢复 JSONL 快照。"""
        mongo_ok, milvus_ok = await asyncio.gather(
            self._session.connect_db(),
            self._long_term.connect(),
        )
        # 消息归档独立连接（与 session 共享 MongoDB，但使用独立 repo）
        await self._message_repo.connect()
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

    async def _start_token_and_prompt(self, mongo_ok: bool) -> None:
        """创建 TokenTracker + PromptRecorder，注入 CognitiveLoop。"""
        from core.token_tracker import TokenTracker
        self._token_tracker = TokenTracker(
            session_id=self._session.session_id,
            output_manager=self._output,
            repo=self._token_repo if mongo_ok else None,
        )
        self._cognitive.set_token_tracker(self._token_tracker)
        if mongo_ok:
            await self._token_tracker.flush_pending()

        from core.debug.prompt_recorder import PromptRecorder
        self._prompt_recorder = PromptRecorder(max_records=20)
        self._cognitive.set_prompt_recorder(self._prompt_recorder)
        from core.debug.response_recorder import ResponseRecorder
        self._response_recorder = ResponseRecorder(max_records=20)
        self._cognitive.set_response_recorder(self._response_recorder)

    async def _start_tool_context(self) -> None:
        """注入 ToolContext，加载工具插件，设置 HITL broadcast。"""
        self._tool_ctx = ToolContext(
            session_id=self._session.session_id,
            user_id=self._user_name,
            services={
                "memory": self._long_term,
                "scheduler": self._scheduler,
                "config": self._config,
                "session": self._session,
                "_tool_manager": self._tool_manager,          # ToolManager 实例
                "_started_at": self._started_at,               # 启动时刻
                "_token_tracker": self._token_tracker,         # TokenTracker 实例
                "_tool_names": list(self._tool_manager.tool_names),
                "_fsm_state": str(self._fsm.state),
                "_session_msg_count": len(self._session.messages),
                "_tool_count": len(self._tool_manager._plugins),
            },
        )
        self._cognitive.set_tool_context(self._tool_ctx)

        self._plugin_ctx.session_id = self._session.session_id
        self._plugin_ctx.memory = self._long_term
        self._plugin_ctx.scheduler = self._scheduler
        await self._tool_manager.load_all(ALL_PLUGINS, self._plugin_ctx)
        # 修复：load_all 后更新工具名列表（之前在此处快照时 _plugins 为空）
        self._tool_ctx.services["_tool_names"] = list(self._tool_manager.tool_names)
        self._tool_ctx.services["_tool_count"] = len(self._tool_manager._plugins)
        self._tool_manager.set_hitl_broadcast(self._output)
        self._scheduler.set_tool_context(self._tool_ctx)

    async def _start_worker_pool(self) -> None:
        """启动 Worker Pool + embedding 预加载后台任务。"""
        await self._scheduler.start()
        self._embed_task = asyncio.create_task(
            self._long_term._ensure_embedding(), name="embedding-preload")

    async def _start_idle_and_events(self) -> None:
        """启动 IdleMonitor + 主事件循环（独立 asyncio.Task）。"""
        await self._idle.start()
        self._event_task = asyncio.create_task(self._process_events())

    async def _start_tts(self) -> None:
        """创建 TTS 后端（条件执行——由 config.tts.enabled 控制）。

        创建后测试连通性，不可用时自动降级为纯文本模式。
        """
        if self._config.tts.enabled:
            from Module.tts import TTSFactory
            try:
                self._tts = TTSFactory.create(self._config.tts.model)
                # 快速连通性测试——1s 超时，避免卡启动
                import asyncio as _asyncio
                result = await _asyncio.wait_for(self._tts.test(), timeout=1.0)
                if result.get("ok"):
                    self._cognitive.set_tts(self._tts)
                    logger.info("TTS backend connected")
                else:
                    self._tts = None
                    logger.info("TTS not available — text-only mode (%s)", result.get("error", "unknown"))
            except Exception:
                self._tts = None
                logger.info("TTS not available — text-only mode")

    async def _start_task_system(self) -> None:
        """创建 TaskSystem + DelegationPreparer + HITLNotifier，注入 CognitiveLoop。"""
        from Module.task_system.task_system import TaskSystem
        from Module.task_system.delegation_preparer import DelegationPreparer
        from Module.task_system.infra import AgentInfra
        from Module.task_system.schema import ConstraintConfig

        _ts_infra = AgentInfra(
            llm=self._llm.get_raw_client(),  # type: ignore[arg-type]
            milvus=None,
            http=None,
            tool_registry=self._tool_manager,
            constraints=ConstraintConfig(),
        )

        from Module.task_system.protocol import HITLNotifier

        class _EventBusHITLNotifier(HITLNotifier):
            def __init__(self, bus):
                self._bus = bus
            async def request_approval(self, event_data):
                pass  # v1: 审批由 ToolManager SafetyPipeline 直接处理，不经过此通道
            async def cancel_approval(self, approval_ids):
                pass  # v1: 同上，审批全流程走 ToolManager，不经过此通道

        _hitl = _EventBusHITLNotifier(self._bus)
        self._task_system = TaskSystem(infra=_ts_infra, hitl_notifier=_hitl)
        await self._task_system.start()
        # 注入 TaskSystem 到 ToolContext，供 get_runtime_config 查询活跃任务
        self._tool_ctx.services["_task_system"] = self._task_system
        self._delegation_preparer = DelegationPreparer()

        # 注入到 CognitiveLoop
        self._cognitive.set_task_system(self._task_system)
        self._cognitive.set_delegation_preparer(self._delegation_preparer)
        from Module.task_system.schema import UserProfile
        self._cognitive.set_user_profile(
            UserProfile(display_name=self._user_name)
        )
        self._cognitive.set_on_background_task(self._register_background_task)

    def _start_mailbox_polling(self) -> None:
        """启动后台任务 mailbox 轮询（独立 asyncio.Task）。"""
        self._mailbox_task = asyncio.create_task(self._poll_mailboxes())

    async def stop(self) -> None:
        """Stop the agent, save session, clean up resources."""
        logger.info("Agent stopping...")
        self._state = RunState.STOPPED

        # 清空排队 — shutdown 期间不处理新消息
        self._pending_messages.clear()

        # 1. Flush memory consolidation buffer
        await self._flush_consolidator()

        # 1.5. Flush character reflection buffer
        await self._flush_reflector()

        # 1.55. Flush personality compression (L3→L2)
        await self._flush_compression()

        # 1.56. 角色状态持久化保存
        # CharacterState persisted via session.save() — engine.save() removed

        # 2. 先停事件循环 — 防止它在 shutdown 期间继续 post 新任务
        await self._stop_event_task()

        # 3. Cancel cognitive + TTS tasks（belt-and-suspenders）
        self._stop_cognitive_and_tts()

        # 3.5. 停止数据库健康检查
        await self._stop_db_health()

        # 4. 停止 mailbox 轮询
        await self._stop_mailbox()

        # 5. 停止 TaskSystem
        await self._stop_task_system()

        # 6. 停止 Scheduler Worker Pool
        await self._stop_scheduler()

        # 7. 关闭 ToolManager
        await self._stop_tool_manager()

        # 8. 停止 IdleMonitor
        await self._stop_idle()

        # 9. 等待 embedding 任务完成（cancel + await）
        await self._stop_embedding_if_running()

        # 10. 持久化关闭（save → close → shutdown embedding）
        await self._stop_persistence()

        # 11. 关闭 LLM / TTS 客户端
        await self._stop_llm_and_tts()

        # 12. 关闭输出通道
        await self._stop_output()

        logger.info("Agent stopped")

    # ---- stop() sub-methods ----

    async def _flush_consolidator(self) -> None:
        """Memory consolidation buffer 落盘，防止未持久化的记忆丢失。"""
        if self._consolidator:
            try:
                items = await asyncio.wait_for(self._consolidator.flush(), timeout=5.0)
                if items:
                    logger.info("Flushed %d consolidator items on shutdown", len(items))
            except asyncio.TimeoutError:
                logger.warning("Consolidator flush timed out during shutdown")
            except Exception:
                logger.debug("Consolidator flush failed", exc_info=True)

    async def _flush_reflector(self) -> None:
        """CharacterReflector buffer 落盘，防止未持久化的性格反思丢失。"""
        if self._character_engine:
            try:
                await asyncio.wait_for(self._character_engine.flush_reflector(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("CharacterReflector flush timed out during shutdown")
            except Exception:
                logger.debug("CharacterReflector flush failed", exc_info=True)

    async def _flush_compression(self) -> None:
        """L3→L2 性格压缩 flush — 如有未处理的 state_history 条目，触发压缩。"""
        if self._character_engine:
            try:
                await asyncio.wait_for(
                    self._character_engine.flush_compression(), timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("CharacterEngine compression flush timed out")
            except Exception:
                logger.debug("CharacterEngine compression flush failed", exc_info=True)

    async def _stop_event_task(self) -> None:
        """停止 _process_events 事件循环——防止它在 shutdown 期间继续 post 新任务。"""
        if self._event_task:
            logger.debug("Cancelling event task...")
            self._event_task.cancel()
            try:
                await asyncio.wait_for(self._event_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._event_task = None

    def _stop_cognitive_and_tts(self) -> None:
        """Cancel 当前 cognitive task + TTS 任务（belt-and-suspenders）。"""
        self._cognitive.cancel_current()
        self._cognitive._tts_mgr.cancel_all()

    async def _stop_embedding_if_running(self) -> None:
        """取消并等待 embedding 预加载任务完成。"""
        if getattr(self, "_embed_task", None) and not self._embed_task.done():
            self._embed_task.cancel()
        # 等待 task 完成——cancel 后仍需 await 消耗 CancelledError
        if getattr(self, "_embed_task", None):
            try:
                await asyncio.wait_for(self._embed_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

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

    async def _stop_mailbox(self) -> None:
        """停止后台任务 mailbox 轮询——cancel 后必须 await 消耗 CancelledError。"""
        if self._mailbox_task:
            self._mailbox_task.cancel()
            try:
                await asyncio.wait_for(self._mailbox_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._mailbox_task = None

    async def _stop_task_system(self) -> None:
        """停止 TaskSystem（5s 超时）。"""
        if self._task_system:
            try:
                await asyncio.wait_for(self._task_system.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("TaskSystem stop timed out")

    async def _stop_scheduler(self) -> None:
        """停止 Scheduler Worker Pool。"""
        logger.debug("Stopping scheduler Worker Pool...")
        await self._scheduler.stop()

    async def _stop_tool_manager(self) -> None:
        """关闭 ToolManager（清理所有工具插件）。"""
        logger.debug("Stopping ToolManager...")
        await self._tool_manager.shutdown()

    async def _stop_idle(self) -> None:
        """停止 IdleMonitor。"""
        logger.debug("Stopping idle monitor...")
        await self._idle.stop()

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
            logger.debug("Session save failed", exc_info=True)

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

        logger.debug("Shutting down embedding factory...")
        try:
            await asyncio.wait_for(EmbeddingFactory.shutdown(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Embedding factory shutdown timed out")

    async def _stop_llm_and_tts(self) -> None:
        """关闭 LLM 和 TTS 客户端连接。"""
        logger.debug("Closing LLM client...")
        try:
            await asyncio.wait_for(self._llm.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("LLM close timed out")

        if self._tts:
            logger.debug("Closing TTS backend...")
            try:
                await asyncio.wait_for(self._tts.close(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("TTS close timed out")

    async def _stop_output(self) -> None:
        """关闭所有输出通道。"""
        logger.debug("Closing output channels...")
        try:
            await asyncio.wait_for(self._output.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Output close timed out")
