"""Agent 生命周期 — start/stop 编排及所有 _start_*/_stop_*/_flush_* 方法。"""
from __future__ import annotations

import asyncio
import time
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from ._lifecycle_persistence import _LifecyclePersistence
from ._lifecycle_services import _LifecycleServices
from ._types import RunState, _PendingMessage, _BackgroundTaskEntry

from ..event_bus import EventBus, EventPriority, EventType
from ..agent_fsm import AgentFSM, AgentState
from ..utils import set_request_id, get_request_id
from ..prompt_assembler import PromptAssembler
from ..cognitive import CognitiveLoop
from ..idle_monitor import IdleMonitor
from ..memory.consolidator import MemoryConsolidator
from ..personality import CharacterEngine
from core.personality.reflector import CharacterReflector
from ..memory.memory_reflector import MemoryReflector  # 新增
from core.schema.types import IsoTimestamp, SessionId
from core.schema.character import CharacterState
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
from ..task import TaskScheduler

if TYPE_CHECKING:
    from core.storage.character_version_repo import CharacterVersionRepository
    from core.storage.session_repo import SessionRepository
    from core.storage.message_repo import MessageRepository
    from core.storage.token_repo import TokenRepository
    from core.token_tracker import TokenTracker
    from core.debug.prompt_recorder import PromptRecorder
    from core.debug.response_recorder import ResponseRecorder
    from ..memory.session import SessionMemory
    from ..memory.long_term import LongTermMemory
    from Module.task_system.task_system import TaskSystem
    from Module.task_system.delegation_preparer import DelegationPreparer
    from Module.tts.protocol import TTSBackend
    from Module.output import OutputManager
    from ..adapter.llm_adapter import LLMAdapter
    from ..config import AppConfig

logger = logging.getLogger(__name__)


class AgentLifecycle(_LifecyclePersistence, _LifecycleServices):
    """Agent 生命周期 mixin — start/stop 编排。

    所有属性由 Agent.__init__ 初始化，本 mixin 不含 __init__。
    """

    # shutdown 步骤按依赖顺序排列（stop() 中正序执行）
    _SHUTDOWN_STEPS: list[tuple[str, str]] = [
        ("flush_consolidator",   "_flush_consolidator"),
        ("flush_reflector",      "_flush_reflector"),
        ("flush_compression",    "_flush_compression"),
        ("stop_event_task",      "_stop_event_task"),
        ("stop_cognitive_tts",   "_stop_cognitive_and_tts"),
        ("stop_db_health",       "_stop_db_health"),
        ("stop_mailbox",         "_stop_mailbox"),
        ("stop_task_system",     "_stop_task_system"),
        ("stop_scheduler",       "_stop_scheduler"),
        ("stop_tool_adapter",    "_stop_tool_adapter"),
        ("stop_idle",            "_stop_idle"),
        ("stop_embedding",       "_stop_embedding_if_running"),
        ("stop_persistence",     "_stop_persistence"),
        ("stop_llm_tts",         "_stop_llm_and_tts"),
        ("stop_output",          "_stop_output"),
    ]

    if TYPE_CHECKING:
        # 由 Agent.__init__ 初始化的属性，供 Pylance/Pyright 静态检查
        _state: RunState
        _started_at: float
        _fsm: AgentFSM
        _llm: LLMAdapter
        _session: SessionMemory
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
        _task_system: TaskSystem | None
        _delegation_preparer: DelegationPreparer | None
        _token_tracker: TokenTracker
        _prompt_recorder: PromptRecorder
        _response_recorder: ResponseRecorder
        _tts: TTSBackend | None
        _event_task: asyncio.Task[Any] | None
        _mailbox_task: asyncio.Task[Any] | None
        _db_health_task: asyncio.Task[Any] | None
        _embed_task: asyncio.Task[Any] | None
        _pending_messages: list[_PendingMessage]
        _background_tasks: dict[str, _BackgroundTaskEntry]
        _mongo_ok: bool
        _milvus_ok: bool
        _mongo_available: bool
        _milvus_available: bool
        # 由 Agent 定义的跨 mixin 方法
        _process_events: Callable[[], Coroutine[Any, Any, None]]
        _register_background_task: Callable[[str, Any, str, IsoTimestamp], None]
        _poll_mailboxes: Callable[[], Coroutine[Any, Any, None]]

    async def start(self) -> None:
        """Start the agent: connect persistence, idle monitor, event loop."""
        logger.info("Agent starting...")
        self._state = RunState.RUNNING
        self._started_at = time.monotonic()  # 启动时刻打点，用于 get_runtime_config 显示运行时长

        # Reset FSM — may be stuck from a previous run that was interrupted
        self._fsm.reset()

        # Recreate LLM HTTP client (may have been closed by previous stop())
        await self._llm.reconnect()

        # 1. 持久化连接 + session 加载
        mongo_ok, milvus_ok = await self._start_persistence()

        # 2. Token + Prompt 统计
        await self._start_token_and_prompt()

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
        self._start_db_health()

    async def _start_tool_context(self) -> None:
        """启动 ToolAdapter，注入运行时服务。"""
        await self._tool_adapter.start(extra_services={
            "memory": self._long_term,
            "scheduler": self._scheduler,
            "config": self._config,
            "session": self._session,
            "_started_at": self._started_at,
            "_token_tracker": self._token_tracker,
            "_fsm_state": str(self._fsm.state),
            "_session_msg_count": len(self._session.messages),
        })
        self._cognitive.configure(tool_adapter=self._tool_adapter)
        self._scheduler.set_tool_adapter(self._tool_adapter)

    async def _start_worker_pool(self) -> None:
        """启动 Worker Pool + embedding 预加载后台任务。"""
        await self._scheduler.start()
        self._embed_task = asyncio.create_task(
            self._long_term._ensure_embedding(), name="embedding-preload")

    async def _start_idle_and_events(self) -> None:
        """启动 IdleMonitor + 主事件循环（独立 asyncio.Task）。"""
        await self._idle.start()
        self._event_task = asyncio.create_task(self._process_events())

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
            tool_registry=self._tool_adapter,
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
        logger.info("TaskSystem started")
        # 注入 TaskSystem 到 ToolContext，供 get_runtime_config 查询活跃任务
        self._tool_adapter.tool_context.services["_task_system"] = self._task_system
        self._delegation_preparer = DelegationPreparer()

        # 注入到 CognitiveLoop
        from Module.task_system.schema import UserProfile
        self._cognitive.configure(
            task_system=self._task_system,
            delegation_preparer=self._delegation_preparer,
            user_profile=UserProfile(display_name=self._user_name),
            on_background_task=self._register_background_task,
        )

    def _start_mailbox_polling(self) -> None:
        """启动后台任务 mailbox 轮询（独立 asyncio.Task）。"""
        self._mailbox_task = asyncio.create_task(self._poll_mailboxes())

    async def stop(self) -> None:
        """Stop the agent — 按 _SHUTDOWN_STEPS 顺序执行清理。"""
        logger.info("Agent stopping...")
        self._state = RunState.STOPPED
        self._pending_messages.clear()

        for name, method_name in self._SHUTDOWN_STEPS:
            logger.debug("Shutdown step: %s", name)
            method = getattr(self, method_name)
            try:
                result = method()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.debug("Shutdown step %s failed", name, exc_info=True)

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
        if self._embed_task and not self._embed_task.done():
            self._embed_task.cancel()
        # 等待 task 完成——cancel 后仍需 await 消耗 CancelledError
        if self._embed_task:
            try:
                await asyncio.wait_for(self._embed_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

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

    async def _stop_tool_adapter(self) -> None:
        """关闭 ToolAdapter（清理所有工具插件）。"""
        logger.debug("Stopping ToolAdapter...")
        await self._tool_adapter.shutdown()

    async def _stop_idle(self) -> None:
        """停止 IdleMonitor。"""
        logger.debug("Stopping idle monitor...")
        await self._idle.stop()

