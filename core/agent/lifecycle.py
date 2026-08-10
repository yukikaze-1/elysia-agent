"""Agent 生命周期 — start/stop 编排及所有 _start_*/_stop_*/_flush_* 方法。"""
from __future__ import annotations

import asyncio
import time
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from core.agent._lifecycle_persistence import _LifecyclePersistence
from core.agent._lifecycle_services import _LifecycleServices
from core.agent._lifecycle_tools import _LifecycleTools
from core.agent._lifecycle_task_system import _LifecycleTaskSystem
from core.agent._types import RunState, _PendingMessage, _BackgroundTaskEntry

if TYPE_CHECKING:
    from core.storage.character_version_repo import CharacterVersionRepository
    from core.storage.session_repo import SessionRepository
    from core.storage.message_repo import MessageRepository
    from core.storage.token_repo import TokenRepository
    from core.storage.attachment_repo import AttachmentRepository
    from core.monitoring.token_tracker import TokenTracker
    from core.debug.prompt_recorder import PromptRecorder
    from core.debug.response_recorder import ResponseRecorder
    from core.memory.session import SessionMemory
    from core.memory.session_manager import SessionManager
    from core.memory.long_term import LongTermMemory
    from core.memory.consolidator import MemoryConsolidator
    from core.personality import CharacterEngine
    from core.task import TaskScheduler
    from core.monitoring.event_bus import EventBus
    from core.status.agent_fsm import AgentFSM
    from core.cognitive import CognitiveLoop
    from core.monitoring.idle_monitor import IdleMonitor
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.cognitive.delegation import DelegationPreparer
    from Module.tts.protocol import TTSBackend
    from Module.output import OutputManager
    from core.adapter.llm_adapter import LLMAdapter
    from core.config import AppConfig
    from core.schema.types import IsoTimestamp, SessionId

logger = logging.getLogger(__name__)


class AgentLifecycle(_LifecyclePersistence, _LifecycleServices, _LifecycleTools, _LifecycleTaskSystem):
    """Agent 生命周期 mixin — start/stop 编排。

    所有属性由 Agent.__init__ 初始化，本 mixin 不含 __init__。
    """

    # shutdown 步骤按依赖顺序排列（stop() 中正序执行）
    # C4/P1-13：先关 admission/consumer（不再 post 新 turn）→ cancel/await generation
    # （不再产生新 tail）→ 再 drain after-response tail → flush 记忆 → 逐资源 close。
    # 修复前 flush 在关 consumer 之前——flush 期间当前 turn 还能创建新 tail task。
    _SHUTDOWN_STEPS: list[tuple[str, str]] = [
        ("stop_event_task",      "_stop_event_task"),
        ("stop_cognitive_tts",   "_stop_cognitive_and_tts"),
        ("flush_after_response", "_flush_after_response"),
        ("flush_consolidator",   "_flush_consolidator"),
        ("flush_reflector",      "_flush_reflector"),
        ("flush_compression",    "_flush_compression"),
        ("stop_db_health",       "_stop_db_health"),
        ("stop_mailbox",         "_stop_mailbox"),
        ("stop_task_system",     "_stop_task_system"),
        ("stop_scheduler",       "_stop_scheduler"),
        ("stop_tool_adapter",    "_stop_tool_adapter"),
        ("stop_idle",            "_stop_idle"),
        ("stop_embedding",       "_stop_embedding_if_running"),
        ("stop_persistence",     "_stop_persistence"),
        ("stop_attachment_gc",   "_stop_attachment_gc"),
        ("stop_attachment_repo", "_stop_attachment_repo"),
        ("stop_llm_tts",         "_stop_llm_and_tts"),
        ("stop_output",          "_stop_output"),
    ]

    # startup 步骤按依赖顺序排列（start() 中正序执行）
    # _start_persistence 在 start() 中单独处理——需要捕获返回值
    _STARTUP_STEPS: list[tuple[str, str]] = [
        ("token_and_prompt", "_start_token_and_prompt"),
        ("tool_context",     "_start_tool_context"),
        ("worker_pool",      "_start_worker_pool"),
        ("idle_and_events",  "_start_idle_and_events"),
        ("service_state",    "_apply_service_state"),
        ("tts",              "_start_tts"),
        ("task_system",      "_start_task_system"),
        ("mailbox_polling",  "_start_mailbox_polling"),
        ("db_health",        "_start_db_health"),
        ("attachment_repo",  "_start_attachment_repo"),
        ("attachment_gc",    "_start_attachment_gc"),
    ]

    if TYPE_CHECKING:
        # 由 Agent.__init__ 初始化的属性，供 Pylance/Pyright 静态检查
        _state: RunState
        _started_at: float
        _fsm: AgentFSM
        _llm: LLMAdapter
        session: SessionMemory
        _long_term: LongTermMemory
        _message_repo: MessageRepository
        _version_repo: CharacterVersionRepository
        _character_engine: CharacterEngine
        _token_repo: TokenRepository
        _output: OutputManager
        _cognitive: CognitiveLoop
        _user_name: str
        _scheduler: TaskScheduler
        _config: AppConfig
        _bus: EventBus
        _idle: IdleMonitor
        _consolidator: MemoryConsolidator
        _session_repo: SessionRepository
        _session_manager: SessionManager
        _attachment_repo: AttachmentRepository | None
        attachment_repo: AttachmentRepository
        _task_system: WorkflowEngine | None
        _delegation_preparer: DelegationPreparer | None
        _token_tracker: TokenTracker
        _prompt_recorder: PromptRecorder
        _response_recorder: ResponseRecorder
        _tts: TTSBackend | None
        _event_task: asyncio.Task[None] | None
        _mailbox_task: asyncio.Task[None] | None
        _db_health_task: asyncio.Task[None] | None
        _attachment_gc_task: asyncio.Task[None] | None
        _embed_task: asyncio.Task[Any] | None
        _pending_messages: list[_PendingMessage]
        _background_tasks: dict[str, _BackgroundTaskEntry]
        _started_cognitive_tasks: set[asyncio.Task]  # C1/P1-06（agent 定义，lifecycle 复位）
        _mongo_ok: bool
        _milvus_ok: bool
        _mongo_available: bool
        _milvus_available: bool
        # 由 Agent 定义的跨 mixin 方法
        _process_events: Callable[[], Coroutine[Any, Any, None]]
        _register_background_task: Callable[[str, Any, str, IsoTimestamp], None]
        _poll_mailboxes: Callable[[], Coroutine[Any, Any, None]]
        _release_session_work: Callable[[SessionId | None, bool], None]
        _release_pending_session_work: Callable[[_PendingMessage | dict], None]

    async def start(self) -> None:
        """启动 agent：连接持久化层、空闲监视器、事件循环。

        幂等：已在 RUNNING 状态时 no-op。失败时回滚已启动的步骤（调 stop()），
        避免留下 _state=RUNNING 的僵尸 Agent 和重复后台循环。
        """
        if self._state == RunState.RUNNING:
            logger.info("Agent already running, skip")
            return

        logger.info("Agent starting...")
        self._state = RunState.RUNNING
        self._started_at = time.monotonic()
        self._fsm.reset()
        # C4/P1-07：同一 Agent 实例 restart 复位——_shutting_down 恢复 False、
        # after-response registry 清空、C1 的 started-task 集合清空
        self._cognitive.reset_lifecycle_state()
        self._started_cognitive_tasks.clear()
        # 预热 build_sha 缓存——git 只跑一次（build_server_info 每次握手构造时读取，
        # 模块级缓存保证此处在启动时完成 subprocess，握手路径零延迟）
        from core.version import get_build_sha

        get_build_sha()
        try:
            await self._llm.reconnect()
        except Exception:
            logger.error("LLM reconnect failed", exc_info=True)
            self._state = RunState.STOPPED
            raise

        try:
            # persistence 特殊处理——先于循环，返回值由 _start_persistence() 写入 self
            await self._start_persistence()

            # 其余步骤走 _STARTUP_STEPS 数据驱动循环
            for name, method_name in self._STARTUP_STEPS:
                logger.debug("Startup step: %s", name)
                method = getattr(self, method_name)
                try:
                    result = method()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    logger.error("Startup step %s failed", name, exc_info=True)
                    raise
        except Exception:
            # 回滚——清理已启动的步骤，避免僵尸 Agent + 重复后台循环
            logger.warning("Agent start failed, rolling back", exc_info=True)
            self._state = RunState.STOPPED
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop the agent — 按 _SHUTDOWN_STEPS 顺序执行清理。"""
        logger.info("Agent stopping...")
        self._state = RunState.STOPPED
        for pending in self._pending_messages:
            self._release_pending_session_work(pending)
        self._pending_messages.clear()
        self._background_tasks.clear()  # restart 复用同一实例，清理残留后台任务

        for name, method_name in self._SHUTDOWN_STEPS:
            logger.debug("Shutdown step: %s", name)
            method = getattr(self, method_name)
            try:
                result = method()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.warning("Shutdown step %s failed", name, exc_info=True)

        # 当前 cognitive task 已取消并完成 shutdown；清掉 EventBus 中未消费事件可能
        # 留下的引用，保证同一 Agent 实例 restart 后无陈旧删除门禁。
        self._session_manager.clear_work()

        logger.info("Agent stopped")

    # ---- stop() sub-methods ----

    async def _flush_after_response(self) -> None:
        """等待 fire-and-forget after-response task 完成，防止记忆保存丢失。"""
        try:
            await asyncio.wait_for(self._cognitive.flush_after_response(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("flush_after_response timed out during shutdown")
        except Exception:
            logger.warning("flush_after_response failed", exc_info=True)

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
                logger.warning("Consolidator flush failed", exc_info=True)

    async def _flush_reflector(self) -> None:
        """CharacterReflector buffer 落盘，防止未持久化的性格反思丢失。"""
        if self._character_engine:
            try:
                await asyncio.wait_for(self._character_engine.flush_reflector(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("CharacterReflector flush timed out during shutdown")
            except Exception:
                logger.warning("CharacterReflector flush failed", exc_info=True)

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
                logger.warning("CharacterEngine compression flush failed", exc_info=True)
