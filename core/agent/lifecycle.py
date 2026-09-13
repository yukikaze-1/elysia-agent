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
from Module._common.task_scope import AsyncTaskScope

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
    from core.tool_runtime import ToolCallDispatcher
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
    from Module.workflow_contract.schema import WorkflowId

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
        ("drain_cognitive",      "_drain_cognitive_turns"),
        ("flush_after_response", "_flush_after_response"),
        ("flush_consolidator",   "_flush_consolidator"),
        ("flush_reflector",      "_flush_reflector"),
        ("flush_compression",    "_flush_compression"),
        ("drain_personality",    "_drain_personality_tasks"),
        ("stop_db_health",       "_stop_db_health"),
        # S2-A4：task_system 必须在 mailbox **之前**停。顺序反了会让 `_stop_mailbox`
        # 在引擎仍在收尾时就做收尾排空，那时终态事件还没写进 mailbox；引擎停完之后
        # 再排空才能一次读全（mailbox 是有界保留队列，终态事件优先保留，晚读不丢）。
        # 配套改动：`_background_tasks.clear()` 从 `stop()` 入口移进了 `_stop_mailbox`
        # ——否则排空时注册表已空，无条目可查（这正是 S2-A4 的原缺陷）。
        ("stop_task_system",     "_stop_task_system"),
        ("stop_mailbox",         "_stop_mailbox"),
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
        ("character_integrity", "_check_character_data_integrity"),
        ("schema_drift",     "_check_schema_drift"),
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
        _runtime_registry: Any
        _consolidator: MemoryConsolidator
        _token_repo: TokenRepository
        _output: OutputManager
        _cognitive: CognitiveLoop
        _user_name: str
        _scheduler: ToolCallDispatcher
        _config: AppConfig
        _bus: EventBus
        _idle: IdleMonitor
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
        _background_tasks: dict[WorkflowId, _BackgroundTaskEntry]
        _started_cognitive_tasks: set[asyncio.Task]  # C1/P1-06（agent 定义，lifecycle 复位）
        _lifecycle_tasks: AsyncTaskScope  # ARCH-INC-P2-03：Agent 长期循环
        _turns: AsyncTaskScope            # ARCH-INC-P2-03：每回合认知任务
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
            # review 复核 #3：stop() 经 drain_all_archives 关闭全部归档，restart 必须
            # 重开——否则重启后 add_message 不再写 MessageRepository，新消息只在内存。
            self._session_manager.reopen_all_archives()

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
        except BaseException:
            # 回滚——清理已启动的步骤，避免僵尸 Agent + 重复后台循环。
            # CR-02：捕获 **BaseException** 而非 Exception——`CancelledError` /
            # `KeyboardInterrupt` 都是 BaseException 子类，原先它们绕过回滚，
            # 留下"半初始化 Agent"（已 spawn 的后台 task、已建 DB 客户端、
            # 已启动的 scheduler worker 全部被丢弃，而 `_state` 停在 RUNNING）。
            logger.warning("Agent start failed, rolling back", exc_info=True)
            self._state = RunState.STOPPED
            try:
                await self.stop()
            except BaseException:
                # 回滚中再次被取消/中断：记录后仍然重抛**原始**异常，
                # 否则调用方看到的是二次异常、根因被掩盖。
                logger.error("回滚期间 stop() 再次失败", exc_info=True)
            raise

    async def stop(self) -> None:
        """Stop the agent — 按 _SHUTDOWN_STEPS 顺序执行清理。

        CR-02：逐步清理捕获 **BaseException**——原先只 catch `Exception`，
        而 `CancelledError`/`KeyboardInterrupt` 是 BaseException 子类，一旦关停
        过程中被取消（用户再按一次 Ctrl+C、超时取消、SIGINT 落在 stop 上），
        **剩余清理步骤被整段跳过**（会话落库 / 归档 drain / 3 个 flush /
        生成终态 / WS 关闭帧全部不做）。现在保证"已进入 stop() 就走完"，
        并把非 `Exception` 的那个异常（取消/中断）在收尾后重抛，不吞掉取消语义。

        Raises:
            BaseException: 逐步清理中出现的第一个非 `Exception` 异常（通常是
                `asyncio.CancelledError`），在本方法收尾完成后重抛。
        """
        logger.info("Agent stopping...")
        self._state = RunState.STOPPED
        for pending in self._pending_messages:
            self._release_pending_session_work(pending)
        self._pending_messages.clear()
        # S2-A4：**不在这里**清 `_background_tasks`（原先在此 clear）。
        # 清在这里等于把停机窗口内到达的后台任务终态事件变成不可达：轮询循环随即
        # 因 `_state != RUNNING` 停转，而 WorkflowEngine 还要好几步之后才停，中间
        # 完成/失败/取消的任务其 mailbox 事件无人读、注册表条目也已消失。清理与
        # 「最后一次同步排空」一起放在 `_stop_mailbox`（引擎停完之后）执行。

        pending_exc: BaseException | None = None
        for name, method_name in self._SHUTDOWN_STEPS:
            logger.debug("Shutdown step: %s", name)
            method = getattr(self, method_name)
            try:
                result = method()
                if hasattr(result, "__await__"):
                    await result
            except BaseException as exc:
                logger.warning("Shutdown step %s failed", name, exc_info=True)
                if not isinstance(exc, Exception) and pending_exc is None:
                    pending_exc = exc

        # ARCH-INC-P2-03：兜底 drain——上面逐步收尾已各自限时，这里只处理
        # 未被任何具名字段接管的残留后台任务。用 drain 而非 aclose：同一实例
        # 支持 stop→start 重启，封闭 scope 会让重启后一律 TaskScopeClosedError。
        leftover = await self._lifecycle_tasks.drain(timeout=2.0)
        if leftover:
            logger.warning("Agent 关停后仍有 %d 个后台任务未落定", leftover)

        # 当前 cognitive task 已取消并完成 shutdown；清掉 EventBus 中未消费事件可能
        # 留下的引用，保证同一 Agent 实例 restart 后无陈旧删除门禁。
        self._session_manager.clear_work()

        logger.info("Agent stopped")

        # CR-02：收尾完成后重抛被捕获的取消/中断——清理走完了，但取消语义不能吞。
        if pending_exc is not None:
            raise pending_exc

    # ---- stop() sub-methods ----

    async def _drain_cognitive_turns(self) -> None:
        """等待被取消的回合任务真正跑完收尾，再让后续步骤关掉它们依赖的资源。

        ARCH-INC-P2-03 修复：``cancel_current()`` 只发 `.cancel()` 就丢弃引用，
        而 ``CognitiveLoop.process`` 会吞掉 CancelledError 转去 ``_handle_interrupt``
        继续写 generation 终态/租约。此前 stop() 不 await 它，于是
        ``_stop_persistence`` 可能在回合收尾**仍在写库**时关掉 Mongo/repo。
        """
        leftover = await self._turns.drain(timeout=3.0)
        if leftover:
            logger.warning("回合任务未在 3s 内收尾（%d 个仍在运行）", leftover)

    async def _flush_after_response(self) -> None:
        """等待 fire-and-forget after-response task 完成，防止记忆保存丢失。"""
        try:
            await asyncio.wait_for(self._cognitive.flush_after_response(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("flush_after_response timed out during shutdown")
        except Exception:
            logger.warning("flush_after_response failed", exc_info=True)

    async def _flush_consolidator(self) -> None:
        """Memory consolidation buffer 落盘，防止未持久化的记忆丢失。

        Task 7（记忆 v2 隔离）：不再全局 _consolidator 单例——per-runtime 化后经
        RuntimeRegistry 遍历各 instance runtime 的 consolidator，对每个触发 flush。
        与 _flush_reflector 同模式：getattr(self, "_runtime_registry", None)——agent 属性
        是 _runtime_registry（非 _registry）。
        """
        registry = getattr(self, "_runtime_registry", None)
        if registry is None:
            return
        for rt in registry._runtimes.values():  # noqa: SLF001 — 组合根遍历 registry 持有的 runtime
            if rt.consolidator:
                try:
                    items = await asyncio.wait_for(rt.consolidator.flush(), timeout=5.0)
                    if items:
                        logger.info("Flushed %d consolidator items on shutdown", len(items))
                except asyncio.TimeoutError:
                    logger.warning("Consolidator flush timed out during shutdown")
                except Exception:
                    logger.warning("Consolidator flush failed", exc_info=True)

    async def _flush_reflector(self) -> None:
        """CharacterReflector buffer 落盘，防止未持久化的性格反思丢失。

        Task 11（M3）：不再全局 _character_engine——经 RuntimeRegistry 遍历各 instance
        runtime 的 engine，对每个 engine 触发 reflector flush（engine per-instance）。
        """
        registry = getattr(self, "_runtime_registry", None)
        if registry is None:
            return
        for rt in registry._runtimes.values():  # noqa: SLF001 — 组合根遍历 registry 持有的 runtime
            engine = rt.engine
            if engine is None:
                continue
            try:
                await asyncio.wait_for(engine.flush_reflector(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("CharacterReflector flush timed out during shutdown")
            except Exception:
                logger.warning("CharacterReflector flush failed", exc_info=True)

    async def _flush_compression(self) -> None:
        """L3→L2 性格压缩 flush — 如有未处理的 state_history 条目，触发压缩。

        Task 11（M3）：不再全局 _character_engine——经 RuntimeRegistry 遍历各 instance
        runtime 的 engine，对每个 engine 触发 compression flush（engine per-instance）。
        """
        registry = getattr(self, "_runtime_registry", None)
        if registry is None:
            return
        for rt in registry._runtimes.values():  # noqa: SLF001 — 组合根遍历 registry 持有的 runtime
            engine = rt.engine
            if engine is None:
                continue
            try:
                await asyncio.wait_for(engine.flush_compression(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("CharacterEngine compression flush timed out")
            except Exception:
                logger.warning("CharacterEngine compression flush failed", exc_info=True)

    async def _drain_personality_tasks(self) -> None:
        """等待 personality 域的 fire-and-forget 任务落定（ARCH-INC-P2-03）。

        这些任务是 awareness LLM 判定与表情帧推送（``core/personality/_utils.py``
        的 ``_schedule``）。含 LLM 调用的那些必须在 ``stop_llm_tts`` 之前收尾，
        否则会调向已关闭的 LLM 客户端。
        """
        from core.personality._utils import drain_scheduled

        leftover = await drain_scheduled(timeout=3.0)
        if leftover:
            logger.warning("personality 域后台任务未在 3s 内落定（%d 个仍在途）", leftover)
