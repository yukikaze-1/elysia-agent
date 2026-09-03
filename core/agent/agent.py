"""Agent —— 将 L0/L1/L2 各组件装配成一个可运行的 Agent。

用法:
    agent = Agent(config=config)
    await agent.start()
    await agent.process_message("你好")       # 单轮对话
    await agent.run_repl()                   # 交互式 REPL
    await agent.stop()

组件依赖图（自顶向下）:
    AppConfig ──┬── CharacterConfig ──┐
                ├── LLMAdapter ──────┼── CharacterEngine ──┐
                ├── MemoryConfig ────┤                     │
                └── ServerConfig     │                     │
                                     │                     │
    Repos ───────┬── SessionManager  │                     │   (批次 3: multi-session 池, active → SessionMemory)
                 ├── LongTermMemory  │                     │
                 └── VersionRepo ────┘                     │
                                                           │
    EventBus ──────────────────┐                           │
    AgentFSM ──────────────────┤                           │
    ToolAdapter ───────────────┤                           │
    PromptAssembler ───────────┤                           │
    MemoryConsolidator ────────┤                           │
    MemoryReflector ───────────┤                           │
                               ├── CognitiveLoop ──────────┤
    TokenTracker ──────────────┤                           │
    PromptRecorder ────────────┤                           │
    ResponseRecorder ──────────┤                           │
    TTSBackend ────────────────┤                           │
    WorkflowEngine ────────────┤                           │
    DelegationPreparer ────────┤                           │
                               │                           │
                               └── IdleMonitor ────────────┘
                               │
                               └── TaskScheduler

    两阶段初始化：__init__ 创建 22 个组件；start() 在拿到 session_id 后再创建 8 个
    (token_tracker, tts, task_system 等)。交叉装配经 start() 中的
    CognitiveLoop.configure() 完成。
"""

from __future__ import annotations

import asyncio
import time
import os
from dataclasses import asdict
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.monitoring.token_tracker import TokenTracker
    from core.debug.prompt_recorder import PromptRecorder
    from core.debug.response_recorder import ResponseRecorder
    from Module.workflow_engine.mailbox import WorkflowMailbox
    from Module.stt.protocol import STTBackend
    from core.status import AgentStatus, CognitivePhaseController

from Module.output import OutputManager, ConsoleChannel
from Module.workflow_contract.schema import WorkflowId

from core.agent._types import RunState, _PendingMessage, _BackgroundTaskEntry
from core.agent.lifecycle import AgentLifecycle
from core.agent.server import AgentServer

from core.config import AppConfig
from core.monitoring.event_bus import EventBus, EventPriority, EventType
from core.status.agent_fsm import AgentFSM, AgentState
from core.utils import set_request_id, get_request_id
from core.config import CharacterConfig
from core.memory.session_manager import SessionManager
from core.memory.long_term import LongTermMemory
from core.prompt.prompt_assembler import PromptAssembler
from core.adapter import ToolAdapter
from core.tools import ALL_PLUGINS
from core.monitoring.idle_monitor import IdleMonitor
from core.adapter.llm_adapter import LLMAdapter
from core.memory.consolidator import MemoryConsolidator
from core.personality import CharacterEngine
from core.personality.reflector import CharacterReflector
from core.memory.memory_reflector import MemoryReflector
from core.storage.character_version_repo import CharacterVersionRepository
from core.schema.types import IsoTimestamp, MessageId, SessionId
from core.schema.character import CharacterState
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
from Module.embedding import EmbeddingFactory
from core.task import TaskScheduler
from core.storage.session_repo import SessionRepository
from core.storage.message_repo import MessageRepository
from core.storage.token_repo import TokenRepository
from core.storage.memory_repo import MemoryRepository
from core.storage.desktop_call_repo import DesktopCallRepository
from core.storage.attachment_repo import AttachmentRepository
from core.storage.idempotency_repo import IdempotencyRepository
from core.storage.attachment_release import release_session_attachments
from core.desktop.bridge import DesktopToolBridge

logger = logging.getLogger(__name__)


class Agent(AgentLifecycle, AgentServer):
    """由 L0/L1/L2 各组件装配而成的完整 Agent。"""

    def __init__(self, *, config: AppConfig, config_path: str | None = None):
        """初始化 Agent 并创建所有 L0/L1/L2 组件。

        Args:
            config: 应用配置（AppConfig 实例）。
            config_path: 配置文件路径，用于 /restart 时重新加载配置。
        """
        self._config = config
        self._config_path = config_path
        self._user_name = config.agent.user_name

        # ---- L0: Infrastructure (from Module/) ----
        self._output = OutputManager()
        self._output.add_channel(ConsoleChannel())

        # ---- Character ----
        self._character = CharacterConfig.from_yaml(config.agent.character)

        # ---- Storage Repositories ----
        db_cfg = config.database
        self._session_repo = SessionRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
        )
        self._message_repo = MessageRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
        )
        self._token_repo = TokenRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
        )
        self._memory_repo = MemoryRepository(
            base_url=db_cfg.milvus_uri,
            token=db_cfg.milvus_token or None,
        )
        self._version_repo = CharacterVersionRepository(
            base_url=db_cfg.mongodb_uri,
        )
        # 批次 4 Desktop 调用注册表（desktop_calls 集合；Mongo 离线降级为进程内缓存）
        self._desktop_call_repo = DesktopCallRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
        )
        # 批次 7：附件仓库——lazy 初始化（attachment_repo property 首访时构造，
        # start() 建连；Mongo 离线附件操作 fail-closed）
        self._attachment_repo: AttachmentRepository | None = None
        # D6c：幂等 claim 仓库——lazy 初始化（idempotency_repo property 首访构造，
        # _start_persistence 建连；产品模式 Mongo 离线幂等 fail-closed）
        self._idempotency_repo: IdempotencyRepository | None = None

        # ---- L2: Memory ----
        # 批次 3 (multi-session)：SessionManager 取代单一 self._session——
        # 缓存池 + 活动指针，eager 构造 default 空实例。session property 直通 active。
        self._session_manager = SessionManager(
            repo=self._session_repo,
            msg_repo=self._message_repo,
            max_messages=config.memory.session_max_messages,
        )
        # review 复核 #5 D2：会话删除提交后释放该会话归档消息的附件 refs（失败由
        # manager journal 重试）。lazy 闭包——delete/retry 时才解引用 self 的 repo。
        self._session_manager.set_attachment_cleanup(
            lambda sid: release_session_attachments(self, sid))
        self._long_term = LongTermMemory(
            max_entries=config.memory.ltm_max_entries,
            max_age_days=config.memory.ltm_max_age_days,
            embed_timeout=config.memory.ltm_embed_timeout,
            enable_eviction=config.memory.ltm_enable_eviction,
            repo=self._memory_repo,
        )

        # ---- L2: Tools ----
        # 批次 4（review 修正，构造顺序 critical）：DesktopToolBridge 必须先于
        # ToolAdapter 实例化——① 构造绑定注入（definitions() D-j 过滤）；② _start_persistence
        # 早于 _STARTUP_STEPS/_start_tool_context，bridge 必须已存在供 connect_db()。
        self._desktop_bridge = DesktopToolBridge(repo=self._desktop_call_repo)
        self._tool_adapter = ToolAdapter(
            ALL_PLUGINS,
            session_id="",
            user_name=self._user_name,
            config=self._config,
            hitl_broadcast=self._output,
            desktop_bridge=self._desktop_bridge,
        )

        # ---- L1: Cognitive ----
        self._bus = EventBus()
        self._fsm = AgentFSM()
        self._llm = LLMAdapter(model=config.llm.model)

        # ---- 性格 + 情绪引擎（合并）----
        # 与 active session 的 character_state 共用同一对象 — engine 写入 mood 等，
        # session 持久化 + debug 读取。构造顺序（D-l）：SessionManager(eager default)
        # → CharacterEngine(state=default.character_state) → bind_character_engine。
        self._character_engine = CharacterEngine(
            llm=self._llm,
            state=self._session_manager.active.character_state,
            character_name=self._character.name,
            user_name=self._user_name,
            base_personality=self._character.base_personality,
            character_config=self._character,
            output=self._output,
            version_repo=self._version_repo,
        )
        # bind 时对 active(default) 调 attach_session：绑定 state 视图 + 设 pre_save_hook
        self._session_manager.bind_character_engine(self._character_engine)

        # 保存 reflector 引用供 debug_info 使用
        self._character_reflector = self._character_engine._reflector

        self._prompt = PromptAssembler(
            self._character, self._session_manager.active, self._long_term,
            personality_engine=self._character_engine,
        )

        self._tts = None  # 在 _start_tts() 中创建
        self._tts_enabled: bool | None = None  # TTS 有效开关（None → 回退 config.tts.enabled）
        self._stt: STTBackend | None = None  # STT 后端 — stt_transcribe() 读取，支持热切换
        self._stt_enabled: bool | None = None  # STT 有效开关（None → 回退 config.stt.enabled）
        self._stt_emotion_state: dict = {"emotion": "NEUTRAL", "events": []}  # STT 情绪/事件状态
        self._stt_ok = False  # STT 连通性标志 — 在 run_server() 中设置

        self._consolidator = MemoryConsolidator(
            llm=self._llm,
            memory=self._long_term,
            character_name=self._character.name,
            user_name=self._user_name,
        )

        # ---- 记忆反射器 ----
        self._memory_reflector = MemoryReflector(
            llm=self._llm,
            memory=self._long_term,
            session=self._session_manager.active,
            character_name=self._character.name,  # 新增：占位符替换，避免默认值泄漏给 Cyrene
            user_name=self._user_name,            # 新增
        )
        # B6/P2-01：把 reflector 挂到所有会话——反思状态经 session save/load 持久化
        self._session_manager.bind_memory_reflector(self._memory_reflector)

        # ---- Token 统计 ----
        self._token_tracker: "TokenTracker | None" = None  # 在 start() 中创建（需要 session_id）
        self._prompt_recorder: "PromptRecorder | None" = None   # 在 start() 中创建
        self._response_recorder: "ResponseRecorder | None" = None # 在 start() 中创建

        # 批次 3（review 修复，兼容#2）：generation 生命周期持久化注册表由 Agent 持有，
        # 经构造注入 loop（loop 不再自建）——agent 与 loop 是同一实例；生命周期启动时
        # 对 registry 建连（connect），此后 register/mark 真正落库（不再全量 no-op）。
        from core.cognitive.generation_registry import GenerationRegistry
        self._generation_registry = GenerationRegistry()

        from core.cognitive import CognitiveLoop  # 函数级导入：打破循环导入
        self._cognitive = CognitiveLoop(
            llm=self._llm,
            fsm=self._fsm,
            event_bus=self._bus,
            output=self._output,
            prompt=self._prompt,
            session_resolver=lambda c, p: self.resolve_session(c, p),  # 批次 3：按 conversation_id + protocol_version 解析目标会话
            tool_adapter=self._tool_adapter,
            character_engine=self._character_engine,
            consolidator=self._consolidator,
            tts=self._tts,
            token_tracker=self._token_tracker,
            generation_registry=self._generation_registry,
        )
        self._cognitive.configure(
            memory_reflector=self._memory_reflector,
            # _after_response 是独立 task，主 process 返回后仍会保存 SessionMemory；
            # 给它单独一份引用，避免删除后晚到 save upsert 复活幽灵会话。
            session_work_reserver=lambda sid: self._session_manager.reserve_work(sid, 2),
            session_work_releaser=self._session_manager.release_work,
        )

        self._idle = IdleMonitor(
            character=self._character,
            fsm=self._fsm,
            event_bus=self._bus,
        )

        # ---- Status & Phase Controller ----
        from core.status import FSMBackedStatus, FSMBackedPhaseController

        self.status: AgentStatus = FSMBackedStatus(self._fsm)
        self.phases: CognitivePhaseController = FSMBackedPhaseController(
            fsm=self._fsm,
            bus=self._bus,
            output=self._output,
            tts_mgr=self._cognitive._tts_mgr,
            status=self.status,
        )
        self._cognitive.phases = self.phases
        self._cognitive.status = self.status
        # 批次 3 review 修复（F1-a）：SessionManager.switch 忙时拒绝（生成/TTS 中
        # 不换绑 engine，防 state 竞态）——activate 入口 FSM 门禁的双保险。
        self._session_manager.set_busy_check(lambda: not self.status.is_accepting_input)

        # Worker Pool — 执行 AI 规划的工具子任务 + 后台任务
        self._scheduler = TaskScheduler(
            tool_executor=self._tool_adapter,
            tool_ctx=None,  # 在 start() 中通过 configure() 更新
            num_workers=5,
            checkpoint_path="data/scheduler_checkpoint.json",
        )

        self._state = RunState.STOPPED
        self._event_task: asyncio.Task | None = None
        self._req_counter: int = 0
        # FIFO 消息排队 — FSM != IDLE 时暂存消息
        self._pending_messages: list[_PendingMessage] = []
        # C1/P1-06：已真正开始执行的 cognitive task 集合——pre-start 任务不被后一
        # handler 取消（第二条消息排队），只在任务真正运行时才允许打断。
        self._started_cognitive_tasks: set[asyncio.Task] = set()

        # ---- WorkflowEngine ----
        self._task_system = None   # WorkflowEngine | None
        self._delegation_preparer = None  # DelegationPreparer | None
        self._background_tasks: dict[WorkflowId, _BackgroundTaskEntry] = {}
        self._mailbox_task: asyncio.Task | None = None
        self._db_health_task: asyncio.Task | None = None
        self._embed_task: asyncio.Task | None = None
        self._attachment_gc_task: asyncio.Task | None = None

    # ---- process_message ----

    async def process_message(self, text: str = "", user_name: str = "",
                               content: list[dict] | None = None,
                               message_id: str | None = None,
                               protocol_version: int = 1,
                               conversation_id: str | None = None,
                               emotion: str = "",
                               events: list[str] | None = None) -> bool:
        """处理单条用户消息，通过管道送入 CognitiveLoop。

        Agent 忙碌时消息入队（FIFO），IDLE 时出队处理。
        显式中断（停止按钮、VAD）仍通过 EventBus INTERRUPT 机制生效。

        Args:
            text: 纯文本消息（兼容旧调用）。
            user_name: 发送者名称，为空时使用配置中的默认用户名。
            content: 多模态内容数组（ChatMessageV2 content），新接口。
            message_id: v2 预分配用户消息 ID（ACK 一致，落库优先用）；v1 无则 None。
            protocol_version: 来源协议版本（1=v1，2=v2），默认 1。
            conversation_id: 批次 3（multi-session）目标会话 ID；v2 的 None 会在
                accepted 前解析并固化为当时活动会话，v1 仍由 loop 解析到 default。
            emotion: 语音 STT 情绪标签；文本消息为空。
            events: 语音 STT 事件标签；文本消息为空。

        Returns:
            bool: True = 消息已接受（已入队或直发）；False = 消息为空或队列满被丢弃。
                调用方（v2 附件/sticker 路由）据此决定 ACK accepted / QUEUE_FULL rejected。
        """
        message_id_nt = MessageId(message_id) if message_id else None
        conversation_id_nt = SessionId(conversation_id) if conversation_id else None
        user_name = user_name or self._user_name
        if not text.strip() and not content:
            return False

        # v2 在 ACK accepted 前原子固定实际目标（None 也解析成当时 active）并登记
        # accepted work。删除会话会在同一 SessionManager 生命周期锁下检查引用计数，
        # 因而 EventBus/pending/生成中的消息不会被删除后静默丢弃或改投新 active。
        session_work_reserved = False
        if int(protocol_version) >= 2:
            target = await self._session_manager.reserve_work(conversation_id, protocol_version)
            conversation_id_nt = SessionId(target.session_id)
            session_work_reserved = True

        rid = self.next_request_id()

        self._idle.touch()

        if not self.status.is_accepting_input or self._bus.pending_user_inputs > 0:
            # 排队消息而不是打断当前流程。review 复核 #4：bus 中已有未派发的
            # USER_INPUT（同 tick 突发，FSM 仍 accepting）同样视为 busy——否则多次
            # process_message 都走直接 post 返回 True，事件派发时才做 pre-start 队列
            # 容量检查并静默丢弃（先 ACK 后丢弃）。接受时即做容量检查。
            max_queue = self._config.agent.max_pending_messages
            if len(self._pending_messages) >= max_queue:
                logger.warning("消息队列已满 (%d) — 丢弃消息", max_queue)
                if session_work_reserved and conversation_id_nt is not None:
                    self._session_manager.release_work(conversation_id_nt)
                return False
            logger.debug("Agent busy — 消息排队 (state=%s, queue=%d)",
                       self.status.current_name(), len(self._pending_messages) + 1)
            self._pending_messages.append(_PendingMessage(
                text=text, user_name=user_name, request_id=rid,
                content=content, emotion=emotion, events=list(events or []),
                message_id=message_id_nt,
                conversation_id=conversation_id_nt,
                protocol_version=protocol_version,
                session_work_reserved=session_work_reserved,
            ))
            return True

        try:
            self._bus.post(UserInputData(
                text=text, user_name=user_name, request_id=rid, content=content,
                emotion=emotion, events=list(events or []), message_id=message_id_nt,
                protocol_version=protocol_version, conversation_id=conversation_id_nt,
                session_work_reserved=session_work_reserved))
        except Exception:
            if session_work_reserved and conversation_id_nt is not None:
                self._session_manager.release_work(conversation_id_nt)
            raise
        return True

    # ---- Background task lifecycle ----

    def _register_background_task(self, task_id: WorkflowId, mailbox: WorkflowMailbox, intent: str, dispatched_at: IsoTimestamp) -> None:
        """CognitiveLoop 提交后台任务后调用——注册到轮询列表。

        Args:
            task_id: 后台任务的唯一标识符。
            mailbox: asyncio.Queue，用于接收任务完成/失败事件。
            intent: 任务意图描述。
            dispatched_at: 任务派发时间（ISO 8601 格式）。
        """
        self._background_tasks[task_id] = _BackgroundTaskEntry(
            intent=intent,
            dispatched_at=dispatched_at,
            mailbox=mailbox,
        )

    def purge_message_from_session(self, msg_id: str) -> int:
        """从当前 session 中移除匹配 message_id 的消息。

        Archive 和 session 是独立存储，只删 archive 不改 session 会导致
        LLM prompt 仍然包含已删消息。此方法在 archive 删除时同步清理 session。

        Args:
            msg_id: 要移除的消息 ID。

        Returns:
            实际移除的消息数量（0 或 1）。
        """
        before = len(self.session.messages)
        kept = [m for m in self.session.messages if m.message_id != msg_id]
        purged = before - len(kept)
        if purged:
            self.session.messages.clear()
            for m in kept:
                self.session.messages.append(m)
        return purged

    async def _poll_mailboxes(self):
        """轮询所有活跃后台任务的 mailbox。每 500ms 一次。"""
        while self._state == RunState.RUNNING:
            await asyncio.sleep(0.5)
            for task_id, info in list(self._background_tasks.items()):
                mailbox = info.mailbox
                if mailbox is None:
                    continue
                try:
                    while True:
                        try:
                            event = mailbox.get_nowait()
                        except asyncio.QueueEmpty:
                            break  # 队列已空，继续下一个 task
                        if event is None:
                            break  # 队列为空（get_nowait 空时返回 None），处理下一个 task
                        data = event.data or {}
                        if event.type.value == "completed":
                            self._bus.post(TaskResultData(
                                task_id=task_id,
                                result=data.get("result", ""),
                                original_intent=info.intent,
                                dispatched_at=info.dispatched_at,
                            ))
                            self._background_tasks.pop(task_id, None)
                            break  # task 已终态，跳出 while 处理下一个 task
                        elif event.type.value == "failed":
                            self._bus.post(TaskResultData(
                                task_id=task_id,
                                result=f"任务执行失败: {data.get('error', '未知错误')}",
                                original_intent=info.intent,
                                dispatched_at=info.dispatched_at,
                                is_error=True,
                            ))
                            self._background_tasks.pop(task_id, None)
                            logger.warning(
                                "后台任务失败: %s — %s",
                                info.intent, data.get('error', '未知错误'),
                            )
                            break
                        elif event.type.value == "cancelled":
                            # E3：取消事件改推 CANCELLED——消费侧须 pop 条目 + 呈现结果，
                            # 否则 _background_tasks 条目永久驻留（直到下次 start 清理）。
                            self._bus.post(TaskResultData(
                                task_id=task_id,
                                result=f"任务已取消: {data.get('reason', '用户取消')}",
                                original_intent=info.intent,
                                dispatched_at=info.dispatched_at,
                                is_error=True,
                            ))
                            self._background_tasks.pop(task_id, None)
                            logger.info(
                                "后台任务已取消: %s — %s",
                                info.intent, data.get('reason', '用户取消'),
                            )
                            break
                except Exception:
                    # 处理单个事件时的意外错误（event.data 访问 / TaskResultData
                    # 构造 / bus.post 等）——不中断整体轮询，但必须留日志诊断。
                    # 注意：队列空（asyncio.QueueEmpty）已在上方 get_nowait 分支
                    # 显式处理，走到这里的是真实错误。
                    logger.warning(
                        "mailbox 轮询处理事件异常（task_id=%s）", task_id, exc_info=True,
                    )

    # ---- Event dispatching ----

    async def _process_events(self) -> None:
        """主事件循环：从 EventBus 消费事件并分发。

        认知处理在独立任务中运行，保证事件循环在 LLM 流式输出期间
        仍能响应 INTERRUPT 事件。
        """
        while self._state == RunState.RUNNING:
            try:
                event = await self._bus.next()
            except RuntimeError:
                break

            data = event.data
            if data is None:
                continue

            # 从事件数据提取 request_id 并设置 ContextVar（修复跨 task 传播）
            rid = getattr(data, "request_id", "")
            if rid:
                set_request_id(rid)

            if isinstance(data, InterruptData):
                self._handle_interrupt(data)
            elif isinstance(data, UserInputData):
                self._handle_user_input(data)
            elif isinstance(data, SystemTickData):
                self._handle_system_tick(data)
            elif isinstance(data, TaskResultData):
                self._handle_task_result(data)
            elif isinstance(data, StateChangeData):
                await self._handle_state_change(data)

    def _handle_interrupt(self, data: InterruptData) -> None:
        """处理 INTERRUPT 事件：cancel cognitive task，清空队列。

        保留 STATE_CHANGE 事件（前端 UI 需要），丢弃其余排队事件。

        Args:
            data: 中断事件数据，包含中断来源等信息。
        """
        set_request_id("-")
        logger.info("EventBus: INTERRUPT (%s)", data.source)
        # 批次 3：source 贯通到 loop 的 cancel 路径（mark 拆分由 Task 9 接入）
        self._cognitive.cancel_current(source=data.source)
        for drained in self._bus.drain():
            if isinstance(drained.data, StateChangeData):
                self._bus.post(drained.data, priority=drained.priority,
                               type=EventType.STATE_CHANGE)
            elif isinstance(drained.data, UserInputData):
                self._release_session_work(
                    drained.data.conversation_id,
                    drained.data.session_work_reserved,
                )
        for pending in self._pending_messages:
            self._release_pending_session_work(pending)
        self._pending_messages.clear()

    def _release_session_work(self, conversation_id: SessionId | None,
                              reserved: bool) -> None:
        """释放一份 v2 accepted work；供完成、排队丢弃、中断和 shutdown 共用。"""
        if reserved and conversation_id is not None:
            self._session_manager.release_work(conversation_id)

    def _release_pending_session_work(self, pending: _PendingMessage | dict) -> None:
        """释放 pending 引用；兼容少量旧测试/迁移代码使用的 dict 队列项。"""
        if isinstance(pending, dict):
            conversation_id = pending.get("conversation_id")
            reserved = bool(pending.get("session_work_reserved", False))
        else:
            conversation_id = pending.conversation_id
            reserved = pending.session_work_reserved
        self._release_session_work(conversation_id, reserved)

    def _handle_user_input(self, data: UserInputData) -> None:
        """处理 USER_INPUT 事件：创建 cognitive task 异步运行。

        Args:
            data: 用户输入事件数据，包含文本、用户名、情感、多模态内容等。
        """
        text = data.text
        user_name = data.user_name
        stt_emotion = data.emotion
        stt_events = data.events
        content = data.content
        rid = data.request_id
        message_id = data.message_id
        protocol_version = data.protocol_version
        logger.debug("EventBus: USER_INPUT 到达 (来源=%s, STT=%s)",
                   "websocket" if stt_emotion else "text", stt_emotion or "无")
        # C1/P1-06：前一条 cognitive task 若尚未真正开始（同 tick 第二条已 accepted 的
        # 消息，pre-start）——不得隐式取消它（否则已 ACK 消息静默丢失且 lease 泄漏），
        # 改为入队等待；只有任务真正在跑时才允许「新输入打断当前生成」。
        prev = self._cognitive._current_task
        if prev is not None and not prev.done() and prev not in self._started_cognitive_tasks:
            max_queue = self._config.agent.max_pending_messages
            if len(self._pending_messages) >= max_queue:
                self._release_session_work(data.conversation_id, data.session_work_reserved)
                return
            logger.debug("Agent pre-start——新消息排队 (queue=%d)",
                       len(self._pending_messages) + 1)
            self._pending_messages.append(_PendingMessage(
                text=data.text, user_name=data.user_name, request_id=data.request_id,
                content=data.content, emotion=data.emotion, events=list(data.events or []),
                message_id=data.message_id,
                conversation_id=data.conversation_id,
                protocol_version=data.protocol_version,
                session_work_reserved=data.session_work_reserved,
            ))
            return
        # 取消上一个未完成的 task（仅真正在跑时），防止快速连续事件造成孤儿
        self._cognitive.cancel_current()
        _on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
            parent_id=rid, calls=calls,
        )

        async def _run_with_session_lease() -> None:
            # 标记任务已真正开始（进入 coroutine）——pre-start 判定依据
            task = asyncio.current_task()
            if task is not None:
                self._started_cognitive_tasks.add(task)
            await self._cognitive.process(
                text, user_name=user_name,
                stt_emotion=stt_emotion,
                stt_events=stt_events,
                _on_tool_calls=_on_tool_calls,
                content=content,
                message_id=message_id,
                protocol_version=protocol_version,
                conversation_id=data.conversation_id,
            )

        def _lease_release_done(task: asyncio.Task) -> None:
            """done callback：唯一 lease 释放点（正常/异常/取消都会触发，恰一次）。

            C1/P1-06：pre-start cancel 不进入 coroutine finally——必须经 done
            callback 释放，否则 work lease 永久泄漏、删除持续 409 busy。释放恰好
            一次（不在 finally 里重复释放，否则会误扣其他在途消息的 lease）。
            """
            self._started_cognitive_tasks.discard(task)
            self._release_session_work(
                data.conversation_id, data.session_work_reserved)

        try:
            task = asyncio.create_task(
                _run_with_session_lease(),
                name=f"cognitive-{text[:20] if text else 'multimodal'}",
            )
        except Exception:
            self._release_session_work(
                data.conversation_id, data.session_work_reserved)
            raise
        task.add_done_callback(_lease_release_done)
        self._cognitive._current_task = task

    def _handle_system_tick(self, data: SystemTickData) -> None:
        """处理 SYSTEM_TICK 事件——silence_timeout 时主动开启话题。

        Args:
            data: 系统定时事件数据，包含触发原因和已过秒数。
        """
        # TODO: time_of_day 事件未处理——producer 在 idle_monitor._check_proactive 中，
        #       此处只消费 silence_timeout。C5/P2-09：未知 proactive kind 不再静默吞——
        #       记 warning 便于后续实现/观测（明确消费行为）。
        if data.reason == "silence_timeout":
            elapsed = data.elapsed or 0
            rid = self.next_request_id()
            set_request_id(rid)
            _on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
                parent_id=rid, calls=calls,
            )
            self._cognitive.cancel_current()
            task = asyncio.create_task(
                self._cognitive.process(
                    f"（系统提示：用户已经{int(elapsed)}秒没有说话了，请自然地开启一个话题）",
                    _on_tool_calls=_on_tool_calls,
                ),
                name="cognitive-proactive"
            )
            self._cognitive._current_task = task
        else:
            logger.warning("SYSTEM_TICK 未知 proactive reason=%s（暂未消费）", data.reason)

    def _handle_task_result(self, data: TaskResultData) -> None:
        """处理 TASK_RESULT 事件——后台任务完成，唤醒 CognitiveLoop。

        Args:
            data: 任务结果事件数据，包含 task_id、结果文本和原始意图。
        """
        rid = self.next_request_id()
        set_request_id(rid)
        self._cognitive.cancel_current()
        task = asyncio.create_task(
            self._cognitive.process(
                user_message="",
                is_task_result=True,
                task_result_context=data.model_dump(),
            ),
            name=f"task-result-{data.task_id}",
        )
        self._cognitive._current_task = task

    async def _handle_state_change(self, data: StateChangeData) -> None:
        """处理 STATE_CHANGE 事件——FSM 状态变更。

        IDLE/IDLE_BACKGROUND_TASK 时检查排队消息（FIFO 出队），
        通知所有输出通道状态变更（前端据此切换动画）。

        Args:
            data: 状态变更事件数据，包含新状态名称。
        """
        state = data.state
        logger.info("FSM → %s", state)
        if state in (AgentState.IDLE, AgentState.IDLE_BACKGROUND_TASK):
            set_request_id("-")
            if self._pending_messages:
                pending = self._pending_messages.pop(0)
                logger.debug("排队消息出队 (%d 条剩余)", len(self._pending_messages))
                try:
                    self._bus.post(UserInputData(
                        text=pending.text,
                        user_name=pending.user_name,
                        request_id=pending.request_id,
                        content=pending.content,
                        emotion=pending.emotion,
                        events=list(pending.events or []),
                        message_id=pending.message_id,  # 批次 2：忙时排队不丢预分配 ID
                        # review 修复（F3/链路#1）：透传 conversation_id + protocol_version——
                        # 否则 v2 排队消息出队后降级为 v1 语义（恒 default），丢失会话归属。
                        conversation_id=pending.conversation_id,
                        protocol_version=pending.protocol_version,
                        session_work_reserved=pending.session_work_reserved,
                    ))
                except Exception:
                    self._release_session_work(
                        pending.conversation_id, pending.session_work_reserved)
                    raise
        await self._output.notify_state(state)

    # 工具注册在 start() 中经 ToolAdapter.start() 完成

    # ---- 公开只读接口（薄 getter 直通）----
    # 39 个 @property：通往 Agent 组合根持有的子系统，可写性由子系统 API 自管。
    # 批次 3 新增 session_manager / active_session_id / pending_messages。

    @property
    def session(self):
        """当前会话内存（SessionManager 活动会话，switch 后指向新会话）。"""
        return self._session_manager.active

    @property
    def session_manager(self):
        """会话管理器（多会话缓存池 + 活动指针）。"""
        return self._session_manager

    @property
    def active_session_id(self):
        """当前活动会话 ID（SessionId/str）。"""
        return self._session_manager.active_id

    @property
    def pending_messages(self):
        """FIFO 队列中暂存的消息（只读快照视图，busy 时排队未出队）。"""
        return list(self._pending_messages)

    async def resolve_session(self, conversation_id: str | None,
                              protocol_version: int):
        """按协议版本 + conversation_id 解析目标会话并确保加载（直通 SessionManager）。"""
        return await self._session_manager.resolve(conversation_id, protocol_version)

    async def switch_session(self, sid: SessionId):
        """切换到目标会话（直通 SessionManager——engine attach + 活动指针更新）。"""
        return await self._session_manager.switch(sid)

    @property
    def long_term(self):
        """长期记忆（Milvus 向量库）。"""
        return self._long_term

    @property
    def message_repo(self):
        """消息归档仓库。"""
        return self._message_repo

    @property
    def session_repo(self):
        """会话仓库。"""
        return self._session_repo

    @property
    def memory_repo(self):
        """长期记忆仓库。"""
        return self._memory_repo

    @property
    def token_repo(self):
        """token 用量仓库。"""
        return self._token_repo

    @property
    def attachment_repo(self):
        """附件仓库（fail-closed 存储，懒初始化；首访时构造真实 repo）。"""
        if self._attachment_repo is None:
            db_cfg = self._config.database
            self._attachment_repo = AttachmentRepository(
                database_name=db_cfg.db_name,
                base_url=db_cfg.mongodb_uri,
            )
        return self._attachment_repo

    @property
    def idempotency_repo(self):
        """幂等 claim 仓库（D6c，懒初始化；首访构造真实 repo）。"""
        if self._idempotency_repo is None:
            db_cfg = self._config.database
            self._idempotency_repo = IdempotencyRepository(
                database_name=db_cfg.db_name,
                base_url=db_cfg.mongodb_uri,
            )
        return self._idempotency_repo

    @property
    def tool_adapter(self):
        """工具子系统适配器。"""
        return self._tool_adapter

    @property
    def output(self):
        """多通道输出管理器。"""
        return self._output

    @property
    def llm(self):
        """LLM 适配器。"""
        return self._llm

    @property
    def tts(self):
        """TTS 流管理器（启动后才有，可能为 None）。"""
        return self._tts

    @property
    def tts_manager(self):
        """TTS 流管理器（播放控制，BE-063 audio.cancel 入口）。"""
        return self._cognitive._tts_mgr

    @property
    def stt(self):
        """STT 后端（只读，可能为 None）。"""
        return self._stt

    @property
    def stt_enabled(self):
        """STT 有效开关（三态回退：None → config.stt.enabled）。"""
        return self._stt_enabled if self._stt_enabled is not None else self._config.stt.enabled

    @property
    def tts_enabled(self):
        """TTS 有效开关（三态回退：None → config.tts.enabled）。"""
        return self._tts_enabled if self._tts_enabled is not None else self._config.tts.enabled

    @property
    def stt_emotion_state(self):
        """STT 语音情绪/事件状态（transcribe 后更新）。"""
        return self._stt_emotion_state

    @property
    def config(self):
        """应用配置。"""
        return self._config

    @property
    def bus(self):
        """事件总线。"""
        return self._bus

    @property
    def cognitive(self):
        """认知循环。"""
        return self._cognitive

    @property
    def generation_registry(self):
        """generation 生命周期注册表（Agent 持有并注入 loop，启动时 connect）。"""
        return self._generation_registry

    @property
    def desktop_bridge(self):
        """Desktop 工具桥（只读——批次 4 BE-041/043）。

        Task 7/8 经此访问：on_client_connect/on_client_disconnect（ws_handler 握手）、
        get_call/pending_calls（REST 状态查询）、request_and_wait（插件 execute 委托）。
        连接/断开后调 `agent.sync_desktop_available()` 同步 ToolManager 的
        desktop 可用性（plugin_list/registry_metadata available 数据源）。
        """
        return self._desktop_bridge

    def sync_desktop_available(self) -> None:
        """同步 desktop 工具可用性到 ToolManager（bridge 连接/断开时调用）。

        公开入口（2026-08-14 模块化修复——原私有 `_sync_desktop_available` 被输入层
        跨模块直调，违反「走公开接口」纪律，公开化后 ws_handler 正常调用）。
        ws_handler 在 `bridge.on_client_connect/disconnect` 后调用本入口，
        把 `bridge.is_available()` 经 `tool_adapter._manager.set_desktop_available`
        写入 `_desktop_available`——plugin_list/registry_metadata 中 desktop 工具
        available 字段的数据源（BE-040 §3.2）。
        """
        self._tool_adapter._manager.set_desktop_available(  # noqa: SLF001 — 组合根内深度同步
            self._desktop_bridge.is_available())

    @property
    def idle(self):
        """空闲监控。"""
        return self._idle

    @property
    def scheduler(self):
        """任务调度器。"""
        return self._scheduler

    @property
    def consolidator(self):
        """记忆整合器。"""
        return self._consolidator

    @property
    def memory_reflector(self):
        """长期记忆反思。"""
        return self._memory_reflector

    @property
    def character_engine(self):
        """角色引擎。"""
        return self._character_engine

    @property
    def character_reflector(self):
        """角色反思器。"""
        return self._character_reflector

    @property
    def character(self):
        """角色配置（L1 Core + config）。"""
        return self._character

    @property
    def token_tracker(self):
        """token 统计（启动时创建）。"""
        return self._token_tracker

    @property
    def prompt_recorder(self):
        """LLM prompt 记录器（启动时创建）。"""
        return self._prompt_recorder

    @property
    def response_recorder(self):
        """LLM 响应记录器（启动时创建）。"""
        return self._response_recorder

    @property
    def user_name(self):
        """注入的用户名。"""
        return self._user_name

    @property
    def state(self):
        """Agent 运行状态（只读）。"""
        return self._state

    @property
    def started_at(self):
        """启动时间戳（启动时创建）。"""
        return self._started_at

    @property
    def stt_ok(self):
        """STT 可用标志。"""
        return self._stt_ok

    async def stt_transcribe(self, audio: bytes) -> str:
        """STT 转写（公开接口）。读取当前 agent.stt 后端，支持热切换。"""
        backend = self.stt
        if backend is None:
            return ""  # shutdown 窗口 _stop_* 置 None 后 _stop_output 才关 channel
        text = await backend.transcribe(audio)
        self.stt_emotion_state["emotion"] = getattr(backend, "last_emotion", "NEUTRAL")
        self.stt_emotion_state["events"] = list(getattr(backend, "last_events", None) or [])
        return text

    # ---- 状态写方法（引导主循环退出，不做清理）----

    def request_shutdown(self) -> None:
        """请求关机——设 _state = STOPPING，引导主循环退出。"""
        self._state = RunState.STOPPING

    def request_restart(self) -> None:
        """请求重启——设 _state = RESTARTING，引导主循环退出。"""
        self._state = RunState.RESTARTING
