"""Agent — wires all L0/L1/L2 components into a runnable Agent.

Usage:
    agent = Agent(config=config)
    await agent.start()
    await agent.process_message("你好")       # single turn
    await agent.run_repl()                   # interactive REPL
    await agent.stop()

Component dependency graph (top-down):
    AppConfig ──┬── CharacterConfig ──┐
                ├── LLMAdapter ──────┼── CharacterEngine ──┐
                ├── MemoryConfig ────┤                     │
                └── ServerConfig     │                     │
                                     │                     │
    Repos ───────┬── SessionMemory   │                     │
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
    WorkflowEngine ────────────────┤                           │
    DelegationPreparer ────────┤                           │
                               │                           │
                               └── IdleMonitor ────────────┘
                               │
                               └── TaskScheduler

    Two-phase init: __init__ creates 22 components; start() creates 8 more
    (token_tracker, tts, task_system, etc.) once session_id is available.
    Cross-wiring via CognitiveLoop.configure() in start().
"""

from __future__ import annotations

import asyncio
import time
import os
from dataclasses import asdict
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..monitoring.token_tracker import TokenTracker
    from ..debug.prompt_recorder import PromptRecorder
    from ..debug.response_recorder import ResponseRecorder
    from Module.workflow_engine.mailbox import WorkflowMailbox
    from core.status import AgentStatus, CognitivePhaseController

from Module.output import OutputManager, ConsoleChannel

from ._types import RunState, _PendingMessage, _BackgroundTaskEntry
from .lifecycle import AgentLifecycle
from .server import AgentServer

from ..config import AppConfig
from ..monitoring.event_bus import EventBus, EventPriority, EventType
from core.status.agent_fsm import AgentFSM, AgentState
from ..utils import set_request_id, get_request_id
from ..config import CharacterConfig
from ..memory.session import SessionMemory
from ..memory.long_term import LongTermMemory
from ..adapter.prompt_assembler import PromptAssembler
from core.adapter import ToolAdapter
from core.tools import ALL_PLUGINS
from ..monitoring.idle_monitor import IdleMonitor
from ..adapter.llm_adapter import LLMAdapter
from ..memory.consolidator import MemoryConsolidator
from ..personality import CharacterEngine
from core.personality.reflector import CharacterReflector
from ..memory.memory_reflector import MemoryReflector
from core.storage.character_version_repo import CharacterVersionRepository
from core.schema.types import IsoTimestamp, SessionId
from core.schema.character import CharacterState
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
from Module.embedding import EmbeddingFactory
from ..task import TaskScheduler
from core.storage.session_repo import SessionRepository
from core.storage.message_repo import MessageRepository
from core.storage.token_repo import TokenRepository
from core.storage.memory_repo import MemoryRepository

logger = logging.getLogger(__name__)


class Agent(AgentLifecycle, AgentServer):
    """Complete Agent wired from L0/L1/L2 components."""

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

        # ---- L2: Memory ----
        self._session = SessionMemory(
            max_messages=config.memory.session_max_messages,
            repo=self._session_repo,
            msg_repo=self._message_repo,
        )
        self._long_term = LongTermMemory(
            max_entries=config.memory.ltm_max_entries,
            max_age_days=config.memory.ltm_max_age_days,
            embed_timeout=config.memory.ltm_embed_timeout,
            enable_eviction=config.memory.ltm_enable_eviction,
            repo=self._memory_repo,
        )

        # ---- L2: Tools ----
        self._tool_adapter = ToolAdapter(
            ALL_PLUGINS,
            session_id="",
            user_name=self._user_name,
            config=self._config,
            hitl_broadcast=self._output,
        )

        # ---- L1: Cognitive ----
        self._bus = EventBus()
        self._fsm = AgentFSM()
        self._llm = LLMAdapter(model=config.llm.model)

        # ---- 性格 + 情绪引擎（合并）----
        # 与 session.character_state 共用同一对象 — engine 写入 mood 等，session 持久化 + debug 读取
        self._character_engine = CharacterEngine(
            llm=self._llm,
            state=self._session.character_state,
            character_name=self._character.name,
            user_name=self._user_name,
            base_personality=self._character.base_personality,
            character_config=self._character,
            output=self._output,
            version_repo=self._version_repo,
        )

        # 保存 reflector 引用供 debug_info 使用
        self._character_reflector = self._character_engine._reflector

        # 注册 pre-save 钩子：session.save() 前同步 reflector buffer → CharacterState
        self._session._pre_save_hook = self._character_engine.sync_reflector_to_state

        self._prompt = PromptAssembler(
            self._character, self._session, self._long_term,
            personality_engine=self._character_engine,
        )

        self._tts = None  # 在 _start_tts() 中创建
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
            session=self._session,
        )

        # ---- Token 统计 ----
        self._token_tracker: "TokenTracker | None" = None  # 在 start() 中创建（需要 session_id）
        self._prompt_recorder: "PromptRecorder | None" = None   # 在 start() 中创建
        self._response_recorder: "ResponseRecorder | None" = None # 在 start() 中创建

        from ..cognitive import CognitiveLoop  # function-level: break circular import
        self._cognitive = CognitiveLoop(
            llm=self._llm,
            fsm=self._fsm,
            event_bus=self._bus,
            output=self._output,
            prompt=self._prompt,
            session=self._session,
            tool_adapter=self._tool_adapter,
            character_engine=self._character_engine,
            consolidator=self._consolidator,
            tts=self._tts,
            token_tracker=self._token_tracker,
        )
        self._cognitive.configure(memory_reflector=self._memory_reflector)

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

        # ---- WorkflowEngine ----
        self._task_system = None   # WorkflowEngine | None
        self._delegation_preparer = None  # DelegationPreparer | None
        self._background_tasks: dict[str, _BackgroundTaskEntry] = {}
        self._mailbox_task: asyncio.Task | None = None
        self._db_health_task: asyncio.Task | None = None
        self._embed_task: asyncio.Task | None = None

    # ---- process_message ----

    async def process_message(self, text: str = "", user_name: str = "",
                               content: list[dict] | None = None) -> None:
        """处理单条用户消息，通过管道送入 CognitiveLoop。

        Agent 忙碌时消息入队（FIFO），IDLE 时出队处理。
        显式中断（停止按钮、VAD）仍通过 EventBus INTERRUPT 机制生效。

        Args:
            text: 纯文本消息（兼容旧调用）。
            user_name: 发送者名称，为空时使用配置中的默认用户名。
            content: 多模态内容数组（ChatMessageV2 content），新接口。
        """
        user_name = user_name or self._user_name
        if not text.strip() and not content:
            return

        rid = self.next_request_id()

        self._idle.touch()

        if not self.status.is_accepting_input:
            # Queue message instead of interrupting
            max_queue = self._config.agent.max_pending_messages
            if len(self._pending_messages) >= max_queue:
                logger.warning("消息队列已满 (%d) — 丢弃消息", max_queue)
                return
            logger.debug("Agent busy — 消息排队 (state=%s, queue=%d)",
                       self.status.current_name(), len(self._pending_messages) + 1)
            self._pending_messages.append(_PendingMessage(
                text=text, user_name=user_name, request_id=rid,
                content=content,
            ))
            return

        self._bus.post(UserInputData(text=text, user_name=user_name,
                                     request_id=rid, content=content))

    # ---- Background task lifecycle ----

    def _register_background_task(self, task_id: str, mailbox: WorkflowMailbox, intent: str, dispatched_at: IsoTimestamp) -> None:
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
        before = len(self._session.messages)
        kept = [m for m in self._session.messages if m.message_id != msg_id]
        purged = before - len(kept)
        if purged:
            self._session.messages.clear()
            for m in kept:
                self._session.messages.append(m)
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
                except Exception:
                    # get_nowait() 抛异常 = 队列空
                    pass

    # ---- Event dispatching ----

    async def _process_events(self) -> None:
        """Main event loop: consume events from EventBus and dispatch.

        Cognitive processing runs in a separate task so the event loop stays
        responsive to INTERRUPT events during LLM streaming.
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
        self._cognitive.cancel_current()
        for drained in self._bus.drain():
            if isinstance(drained.data, StateChangeData):
                self._bus.post(drained.data, priority=drained.priority,
                               type=EventType.STATE_CHANGE)
        self._pending_messages.clear()

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
        logger.debug("EventBus: USER_INPUT 到达 (来源=%s, STT=%s)",
                   "websocket" if stt_emotion else "text", stt_emotion or "无")
        # 取消上一个未完成的 task，防止快速连续事件造成孤儿
        self._cognitive.cancel_current()
        _on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
            parent_id=rid, calls=calls,
        )
        task = asyncio.create_task(
            self._cognitive.process(text, user_name=user_name,
                                    stt_emotion=stt_emotion,
                                    stt_events=stt_events,
                                    _on_tool_calls=_on_tool_calls,
                                    content=content),
            name=f"cognitive-{text[:20] if text else 'multimodal'}"
        )
        self._cognitive._current_task = task

    def _handle_system_tick(self, data: SystemTickData) -> None:
        """处理 SYSTEM_TICK 事件——silence_timeout 时主动开启话题。

        Args:
            data: 系统定时事件数据，包含触发原因和已过秒数。
        """
        # TODO: time_of_day 事件未处理——producer 在 idle_monitor._check_proactive 中，
        #       此处只消费 silence_timeout。time_of_day 事件静默丢弃，等待后续实现。
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
        if state in ("idle", "idle_background_task"):
            set_request_id("-")
            if self._pending_messages:
                pending = self._pending_messages.pop(0)
                logger.debug("排队消息出队 (%d 条剩余)", len(self._pending_messages))
                self._bus.post(UserInputData(
                    text=pending.text,
                    user_name=pending.user_name,
                    request_id=pending.request_id,
                    content=pending.content,
                ))
        await self._output.notify_state(state)

    # Tool registration is done via ToolAdapter.start() in start()

    # ---- 公开只读接口（薄 getter 直通）----
    # 27 个 @property：通往 Agent 组合根持有的子系统，可写性由子系统 API 自管。

    @property
    def session(self):
        """当前会话内存。"""
        return self._session

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

    # ---- 状态写方法（引导主循环退出，不做清理）----

    def request_shutdown(self) -> None:
        """请求关机——设 _state = STOPPING，引导主循环退出。"""
        self._state = RunState.STOPPING

    def request_restart(self) -> None:
        """请求重启——设 _state = RESTARTING，引导主循环退出。"""
        self._state = RunState.RESTARTING
