"""CognitiveLoop —— 核心 ReAct 调度器。

编排 THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE 流程。
使用结构化流式实现亚 100ms TTFT，并同时进行工具调用检测。

子模块：
  text/stream_processor.py     — 流式解析协调器（tokenizer + splitter + blocks）
  text/parts.py                — 句子/块交错 → TextPart + public_text 重建
  adapter/tts_adapter.py       — TTS 流管理器 + emoji 排队
  adapter/tool_call_parser.py  — 合并/规整流式工具调用增量
  utils/text_utils.py          — 内容文本提取 + MessageObj
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Any, Callable, cast, TYPE_CHECKING

from core.utils import new_message_id, new_generation_id
from core.schema.types import MessageId, RequestId, SessionId

from core.cognitive.generation import GenerationContext
from core.cognitive.generation_registry import GenerationRegistry
from core.cognitive.timing import TimingClock
from core.cognitive.turn_context import TurnCharacterContext

from Module.output import OutputManager
from Module.tts import TTSBackend

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from core.memory.session_manager import SessionNotFoundError
from core.utils import set_request_id, get_request_id, log_conversation
from core.storage.desktop_call_repo import STATUS_OFFLINE
from core.prompt.prompt_assembler import PromptAssembler
from core.memory.session import SessionMemory
from core.adapter.llm_adapter import LLMAdapter
from core.memory.consolidator import MemoryConsolidator
from Module.llm.registry import LLMFactory

if TYPE_CHECKING:
    from core.status.fsm import FSMBackedPhaseController, FSMBackedStatus
    from core.cognitive.delegation import DelegationPreparer
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.cognitive.user_profile import UserProfile
    from core.monitoring.token_tracker import TokenTracker
    from core.debug.prompt_recorder import PromptRecorder
    from core.debug.response_recorder import ResponseRecorder
    from core.personality.engine import CharacterEngine
    from core.character.registry import CharacterRuntimeRegistry
    from core.character.runtime import CharacterRuntime
    from core.profile.profile_store import UserIdentityStore
    from core.adapter.tool_adapter import ToolAdapter
    from core.memory.memory_reflector import MemoryReflector

from core.adapter.tts_adapter import TTSStreamManager
from core.utils.text_utils import content_text_from_content
from core.schema.message import UserMessage

from core.schema.adapters.message_adapter import parts_from_dicts
from core.schema.utils import now_iso as _now_iso

logger = logging.getLogger(__name__)

# 空 LLM 响应守卫：LLM 返回 200 成功但无任何对用户可见的文本（含只输出
# <mood>/<inner_voice> 标签、无 speech 的情况）且无工具调用时，整轮会被静默吞掉。
# 对策：注入 nudge 系统消息重试一次；仍空则发兜底回复，保证用户始终看到回应。
_EMPTY_RESPONSE_NUDGE = (
    "你上一轮的回复没有输出任何对用户可见的内容（不能只输出标签）。"
    "请直接以对话形式回复用户当前的话题。"
)
_EMPTY_RESPONSE_FALLBACK = "抱歉，我刚才走神了，你能再说一遍吗？"

# 空响应兜底累计计数器——连续多轮兜底说明系统性异常（LLM 持续空响应），
# 超阈值升 error，避免「走神」回复静默刷屏不升级告警。
_EMPTY_RESPONSE_THRESHOLD = 5

# Task 8（B1 闭环）：session.character_binding 缺 instance_id/character_id 时的回退。
# 与内置 Elysia 角色快照对齐（core/character 默认角色），见 core/character 批。
DEFAULT_INSTANCE_ID = "chi_builtin_elysia"
DEFAULT_CHARACTER_ID = "chr_builtin_elysia"


def _is_llm_layer_error(exc: BaseException) -> bool:
    """判断异常是否属于 LLM/网络层失败（区别于代码 bug）。

    CognitiveLoop.process() 顶层 except 需要区分归因（O7）：代码 bug 不应
    误导用户为「LLM 调用失败」。LLM/网络失败的特征：
    - 超时/OSError（网络层）
    - httpx / openai SDK 异常（模块路径前缀）
    - LLM 输出 JSON 解析失败
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, OSError, json.JSONDecodeError)):
        return True
    mod = type(exc).__module__
    return mod.startswith("httpx") or mod.startswith("openai")


from core.cognitive.tool_phase import CognitiveToolPhase
from core.cognitive.finalize import CognitiveFinalize
from core.cognitive.streaming import CognitiveStreaming, _apply_mood  # noqa: F401


class TurnPhase(str, Enum):
    """CognitiveLoop 单轮处理阶段 — 内部追踪，不暴露给 FSM 外部消费者。"""
    BUILD_PROMPT = "build_prompt"
    STREAMING = "streaming"
    EXECUTING_TOOLS = "executing_tools"
    FINALIZING = "finalizing"


@dataclass
class _TurnSetup:
    """process() 准备阶段产物：目标会话 / generation 会话 / request / clock / 日志文本。"""

    target: SessionMemory
    gen_conversation_id: SessionId
    rid: str
    clock: TimingClock
    log_text: str


class CognitiveLoop(CognitiveStreaming, CognitiveToolPhase, CognitiveFinalize):
    """ReAct 认知循环：流式投递 + 工具调用检测。"""

    MAX_TOOL_ROUNDS = 5

    def __init__(
        self,
        llm: LLMAdapter,
        fsm: AgentFSM,
        event_bus: EventBus,
        output: OutputManager,
        prompt: PromptAssembler,
        session_resolver: Callable[[SessionId | None, int], Awaitable[SessionMemory]],
        tool_adapter: ToolAdapter | None = None,
        character_engine: "CharacterEngine | None" = None,
        consolidator: MemoryConsolidator | None = None,
        tts: TTSBackend | None = None,
        token_tracker: "TokenTracker | None" = None,
        prompt_recorder: "PromptRecorder | None" = None,
        generation_registry: "GenerationRegistry | None" = None,
        runtime_registry: "CharacterRuntimeRegistry | None" = None,
        runtime_factory: "Callable[[str], CharacterRuntime] | None" = None,
        engine_factory: "Callable[[CharacterRuntime], CharacterEngine] | None" = None,
    ):
        """初始化认知循环。

        Args:
            llm: LLM 适配器，负责流式/非流式调用
            fsm: Agent 状态机，驱动 THINKING/EXECUTING/SPEAKING/IDLE 转换
            event_bus: 事件总线，广播 StateChangeData
            output: 多通道输出管理器
            prompt: 系统提示词组装器
            session_resolver: 会话解析器，接收 (conversation_id, protocol_version)
                返回目标 SessionMemory（批次 3 multi-session：每轮 process 开头解析）。
            tool_adapter: 工具适配器（ToolAdapter 实例）
            character_engine: 角色引擎（4 层性格模型）
            consolidator: 记忆沉淀器，提取对话记忆
            tts: TTS 后端，经 TTSStreamManager 代理
            token_tracker: LLM token 用量追踪器
            prompt_recorder: LLM prompt 记录器（供 debug 面板）
            generation_registry: generation 生命周期持久化注册表（批次 3 BE-024）；
                None 时自建（Mongo 离线自动降级 no-op，不阻塞 generation）
        """
        self._llm = llm
        self._fsm = fsm
        self._bus = event_bus
        self._output = output
        self._prompt = prompt
        self._session_resolver = session_resolver
        # 批次 3：self._session 不再在 __init__ 初始化——process() 开头按 resolver
        # 解析本轮目标会话后动态赋值（CognitiveTools mixin 经 self._session 自动跟随）。
        self._tool_adapter = tool_adapter
        self._req_counter: int = 0
        self._engine = character_engine
        self._consolidator = consolidator
        self._memory_reflector: MemoryReflector | None = None  # 由 configure() 注入
        self._tts = tts  # 保留供 agent.py stop() 读取
        self._token_tracker = token_tracker
        self._prompt_recorder = prompt_recorder
        # 批次 3（BE-024）：generation 生命周期持久化注册表（register/mark 挂点）。
        # 默认自建——Mongo 离线时内部 try/except 降级 no-op，绝不阻塞 generation。
        self._generation_registry = (generation_registry
                                     if generation_registry is not None
                                     else GenerationRegistry())
        # Task 7（B1）：character runtime 绑定层注入——registry 按 instance_id 解析
        # runtime，runtime_factory 构造 CharacterRuntime，engine_factory 由 runtime
        # 构造 CharacterEngine（本批：单例 engine + 标记，inert，可接受）。
        self._registry = runtime_registry
        self._runtime_factory = runtime_factory
        self._engine_factory = engine_factory
        # 批次 2：当前回合 GenerationContext（process() 创建，_dispatch_stream_event/
        # _finalize_turn/错误路径使用）；无活动回合为 None
        self._generation_ctx: GenerationContext | None = None
        # 批次 2：本回合是否已发 generation.failed（终态唯一守卫——错误路径已发则打断不重发 CANCELLED）
        self._turn_failed_emitted: bool = False
        # 批次 3 review 修复（F9）：本回合是否已 mark(completed)（成功路径终态）。
        # TTS 播放窗口（turn_completed 已发、_transition_idle 等 wait_all）内打断时，
        # _handle_interrupt 检查此标志不再把已 completed 的 generation 重标 cancelled。
        self._generation_completed: bool = False
        self._response_recorder: ResponseRecorder | None = None  # 由 configure() 注入
        self._ttft_start: float = 0.0
        self._mood_applied: bool = False
        self._process_lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None  # 保留供 agent.py stop() 读取
        self._pending_after_response: list[asyncio.Task] = []  # shutdown 时等待
        # Agent 可选注入：after_response 会异步保存目标 SessionMemory，必须独立持有
        # accepted-work 引用，不能随主 cognitive task 返回就允许删除。
        self._session_work_reserver: (
            Callable[[SessionId], Awaitable[SessionMemory]] | None) = None
        self._session_work_releaser: Callable[[SessionId], None] | None = None
        self._shutting_down: bool = False  # shutdown 门禁——after_response 检查后提前退出
        # TTS 流管理器——抽取出来的子系统
        self._tts_mgr = TTSStreamManager(
            tts, output,
            prompt._character.name if prompt and hasattr(prompt, '_character') else "unknown",
        )
        self._executing_tools: list[str] = []  # 当前正在执行中的工具名 (供 debug 面板)
        self._timing_history: list[dict] = []  # 最近 100 轮请求的计时数据 (供 debug 面板)
        self._task_system: WorkflowEngine | None = None              # 由 configure() 注入
        self._delegation_preparer: DelegationPreparer | None = None  # 由 configure() 注入
        self._on_background_task: Callable | None = None  # Agent 注入的回调
        self._user_profile: UserProfile | None = None         # 由 configure() 注入
        self._user_identity_store: "UserIdentityStore | None" = None  # 由 configure() 注入（Task 7 B1）
        self._turn_context: TurnCharacterContext | None = None  # Task 7：本回合冻结的角色/用户上下文
        self._clock: TimingClock | None = None # 当前轮次的计时器（process() 设置，finally 清除）
        self._phase: TurnPhase | None = None   # 当前轮次处理阶段（供 debug 面板）
        self._phases = None  # CognitivePhaseController（由 agent.py 注入，或懒加载）
        self._status = None  # AgentStatus（由 agent.py 注入，或懒加载）
        self._empty_response_count = 0  # 连续空响应轮数（超阈值升 error）
        self._interrupt_source: str | None = None  # 批次 3：cancel_current(source) 贯通到中断路径（Task 9 mark 拆分消费）

    @property
    def phases(self):
        """CognitivePhaseController — 由 agent.py 注入（或直接构造时懒加载回退）。"""
        if self._phases is None:
            from core.status import FSMBackedPhaseController
            self._phases = FSMBackedPhaseController(
                fsm=self._fsm, bus=self._bus, output=self._output,
                tts_mgr=self._tts_mgr, status=self.status,
            )
        return self._phases

    @phases.setter
    def phases(self, value: "FSMBackedPhaseController"):
        self._phases = value

    @property
    def status(self):
        """AgentStatus — 由 agent.py 注入（或直接构造时懒加载回退）。"""
        if self._status is None:
            from core.status import FSMBackedStatus
            self._status = FSMBackedStatus(self._fsm)
        return self._status

    @status.setter
    def status(self, value: "FSMBackedStatus"):
        self._status = value

    @property
    def current_generation_user_message_id(self) -> str | None:
        """当前在途 generation 的用户消息 ID（D1 断连归属判定）。无在途/未建档 → None。"""
        if self._generation_ctx is None:
            return None
        return self._generation_ctx.user_message_id or None

    # ---- Public API ----

    def cancel_current(self, source: str | None = None) -> bool:
        """取消当前认知任务（如有）。返回 True 表示确实取消了任务。

        Args:
            source: 中断来源（vad / disconnect / button 等），批次 3 起贯通到
                取消路径——存到 _interrupt_source 供 _handle_interrupt 消费，
                Task 9 mark 拆分据此区分「语音打断」与「断线」。

        批次 3 review 修复（F6）的「只增不减」语义保留，但记录条件收窄到
        「实际取消了任务」（D4）：task 已 done（IDLE_BG 窗口断线）不取消任何
        东西，也就没有中断来源可消费——不留 sticky source，否则下一回合用户
        取消被 disconnect 规则锁死成 interrupted。
        """
        if self._current_task is not None and not self._current_task.done():
            if source is not None:
                if self._interrupt_source != "disconnect" or source == "disconnect":
                    self._interrupt_source = source
            self._current_task.cancel()
            self._current_task = None
            return True
        self._current_task = None
        return False

    def mark_shutting_down(self) -> None:
        """标记 CognitiveLoop 进入 shutdown——turn 收尾新创建的 after_response task
        检查此标志后提前退出，避免与已关闭的 LLM/session repo 并发。
        """
        self._shutting_down = True

    def reset_lifecycle_state(self) -> None:
        """C4/P1-07：同一 Agent 实例 restart 前复位生命周期状态。

        修复前 stop() 后 `_shutting_down` 恒 True——再次 start 的 turn 收尾
        after_response 永久跳过（记忆沉淀/性格反思不再执行）。这里复位：
        - `_shutting_down` 恢复 False；
        - 清空 after-response tail registry（stop 已 drain，此处兜底）；
        - 断开 `_current_task` 引用（stop 已取消）。
        """
        self._shutting_down = False
        self._pending_after_response = []
        self._current_task = None

    def configure(self, **kwargs: Any) -> None:
        """一次性注入所有延迟依赖（替代 10 个 setter 方法）。

        由 agent.py 在 __init__ / start() 中调用。有副作用的属性（如 tts）
        在此方法内联处理。

        Args:
            **kwargs: 键值对，键为属性名（不含 _ 前缀），值为注入对象。
                常用键: tool_adapter, memory_reflector, response_recorder,
                task_system, delegation_preparer, on_background_task, user_profile, tts
        """
        for name, value in kwargs.items():
            setattr(self, f"_{name}", value)
            if name == "tts":
                self._tts_mgr.set_backend(value)

    def _resolve_pronouns(self) -> str:
        """读 UserIdentity.pronouns（B1 全局唯一「她」）。无 store 时兜底「她」。"""
        store = getattr(self, "_user_identity_store", None)
        if store is not None:
            identity = store.get()
            if identity is not None and identity.pronouns:
                return identity.pronouns
        return "她"

    # ---- Main entry ----

    async def _prepare_turn(
        self,
        user_message: str,
        user_name: str,
        stt_emotion: str,
        content: list[dict] | None,
        message_id: MessageId | None,
        conversation_id: SessionId | None,
        protocol_version: int,
    ) -> _TurnSetup | None:
        """process 的同步准备阶段：会话解析、context 换绑、日志、generation 建档。"""
        self._generation_ctx = None
        self._generation_completed = False
        self._turn_failed_emitted = False

        try:
            target = await self._session_resolver(conversation_id, protocol_version)
        except SessionNotFoundError:
            logger.warning("会话已删除，拒绝在途消息 conversation_id=%s", conversation_id)
            return None

        gen_conversation_id = conversation_id or target.session_id
        # Task 8（B1 闭环）：从 session.character_binding 解析 instance_id，
        # 经 registry.get（同步，不 await）→ runtime.ensure_engine → self._engine。
        # binding 缺 instance_id/character_id 时回退内置 Elysia。
        binding = target.character_binding or {}
        instance_id = binding.get("instance_id") or DEFAULT_INSTANCE_ID
        registry = cast("CharacterRuntimeRegistry", self._registry)
        runtime = registry.get(instance_id, self._runtime_factory)  # type: ignore[arg-type]
        engine = runtime.ensure_engine(lambda: self._engine_factory(runtime))  # type: ignore[call-optional]
        self._session = target
        self._prompt.set_session(target)
        self._engine = engine
        # Task 7（B1 闭环）：冻结本回合角色/用户上下文，并传给 prompt。
        # characterInstanceId 来自 binding 的 instance_id，characterId 来自 runtime.definition。
        self._turn_context = TurnCharacterContext(
            conversationId=gen_conversation_id,
            characterInstanceId=instance_id,
            characterId=runtime.definition.characterId,
            characterRevision=runtime.revision.revision,
            userPronoun=self._resolve_pronouns(),
            memoryScope=instance_id,
            voice=cast(dict, runtime.revision.runtimeDefaults.get("voice", {})),
            displayName=runtime.revision.displayName,
            stateSnapshot=runtime.snapshot_state(),
        )
        self._prompt._turn_context = self._turn_context
        self._prompt.set_personality_engine(engine)
        self._prompt.set_character(runtime.definition.characterId, runtime.revision.displayName)
        if self._tool_adapter is not None:
            svc = self._tool_adapter.tool_context.services
            svc["session"] = target
            svc["_session_msg_count"] = len(target.messages)

        rid = get_request_id()
        if rid == "-":
            self._req_counter += 1
            rid = f"P{self._req_counter:03d}"
            set_request_id(rid)

        if self._tool_adapter is not None:
            self._tool_adapter.tool_context.trace_id = new_message_id()

        self._tts_mgr.reset_for_turn()
        _svc_state = getattr(target, "service_state", None)
        self._tts_mgr.set_voice(
            getattr(_svc_state, "tts_voice", None) if _svc_state is not None else None)
        clock = TimingClock()
        clock.start()
        self._clock = clock

        log_text = user_message
        if content:
            log_text = content_text_from_content(content)
            if not log_text:
                log_text = "[多模态消息]"

        emo_tag = f" [{stt_emotion}]" if stt_emotion and stt_emotion != "NEUTRAL" else ""
        logger.info(">>> 用户输入 (%s)%s: len=%d message_id=%s",
                    user_name, emo_tag, len(log_text), message_id or "")
        log_conversation("user", user_name, log_text)

        await self.phases.enter_thinking()

        self._generation_ctx = GenerationContext(
            generation_id=new_generation_id(),
            user_message_id=MessageId(message_id or ""),
            assistant_message_id=new_message_id(),
            conversation_id=gen_conversation_id,
            model_id=self._llm.model,
            request_id=RequestId(rid),
            protocol_version=protocol_version,
        )
        self._turn_failed_emitted = False
        if self._generation_registry is not None:
            try:
                await self._generation_registry.register(
                    generation_id=self._generation_ctx.generation_id,
                    session_id=target.session_id,
                    user_message_id=self._generation_ctx.user_message_id,
                    message_id=self._generation_ctx.assistant_message_id,
                    model=self._llm.model,
                    request_id=self._generation_ctx.request_id,
                )
            except Exception:
                logger.debug("generation register 失败（Mongo 离线）", exc_info=True)

        return _TurnSetup(
            target=target,
            gen_conversation_id=gen_conversation_id,
            rid=rid,
            clock=clock,
            log_text=log_text,
        )

    async def _handle_turn_exception(self, e: BaseException) -> None:
        """process 异常收口：区分 LLM 层错误 / 内部错误并发射 generation.failed。"""
        if _is_llm_layer_error(e):
            logger.exception("<<< LLM 调用异常，回退到 IDLE")
            label = "LLM 调用失败"
            code = "GENERATION_TIMEOUT" if isinstance(e, asyncio.TimeoutError) \
                else "GENERATION_PROVIDER_UNAVAILABLE"
            retryable = True
        else:
            logger.exception("<<< 处理回复时内部错误，回退到 IDLE")
            label = "内部错误"
            code = "INTERNAL"
            retryable = False
        err_msg = str(e)[:200]
        if self._generation_ctx is not None:
            try:
                await self._output.turn_failed(
                    code, err_msg, retryable, label, self._generation_ctx)
            except Exception:
                logger.debug("turn_failed 发射失败", exc_info=True)
            self._turn_failed_emitted = True
            if self._generation_registry is not None:
                try:
                    await self._generation_registry.mark(
                        self._generation_ctx.generation_id,
                        status="failed", finish_reason="error",
                        error={"code": code, "message": err_msg, "retryable": retryable})
                except Exception:
                    logger.debug("generation mark failed 失败", exc_info=True)
        await self._handle_interrupt()

    async def process(self, user_message: str = "", user_name: str = "妖梦",
                       stt_emotion: str = "", stt_events: list[str] | None = None,
                       _on_tool_calls: Callable | None = None,
                       content: list[dict] | None = None,
                       is_task_result: bool = False,
                       task_result_context: dict | None = None,
                       message_id: MessageId | None = None,
                       conversation_id: SessionId | None = None,
                       protocol_version: int = 1,
                       ) -> None:
        """处理用户消息，驱动一轮完整的 ReAct 认知循环。"""
        async with self._process_lock:
            if not self.status.is_accepting_input:
                logger.warning("Agent not accepting input — skipping turn")
                return

            setup = await self._prepare_turn(
                user_message, user_name, stt_emotion, content,
                message_id, conversation_id, protocol_version)
            if setup is None:
                return

            self._current_task = asyncio.current_task()
            try:
                await self._think_loop(user_message, user_name,
                                       stt_emotion=stt_emotion, stt_events=stt_events,
                                       _on_tool_calls=_on_tool_calls, content=content,
                                       is_task_result=is_task_result,
                                       task_result_context=task_result_context,
                                       message_id=message_id,
                                       conversation_id=conversation_id,
                                       session=setup.target,
                                       protocol_version=protocol_version)
                setup.clock.stop()
                logger.info("<<< 回复生成完成 — %s", setup.clock.summary())
                self._timing_history.append({
                    "total_ms": setup.clock.total_ms,
                    "spans": dict(setup.clock._spans),
                })
                if len(self._timing_history) > 100:
                    self._timing_history = self._timing_history[-100:]
            except asyncio.CancelledError:
                logger.info("<<< 用户打断")
                await self._handle_interrupt()
            except Exception as e:
                await self._handle_turn_exception(e)
            finally:
                self._clock = None

    # ---- Core loop: prompt construction ----

    async def _build_turn_prompt(
        self, user_message: str, user_name: str,
        content: list[dict] | None,
        stt_emotion: str, stt_events: list[str] | None,
        is_task_result: bool,
        user_message_id: MessageId | None = None,
        conversation_id: SessionId | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """构建本轮 LLM 调用的 messages + tool definitions。

        Args:
            user_message: 用户纯文本输入
            user_name: 用户名
            content: 多模态内容数组（优先于 user_message）
            stt_emotion: STT 情绪标签
            stt_events: STT 事件列表
            is_task_result: 是否为任务结果唤醒（决定是否写入 session）
            user_message_id: v2 预分配用户消息 ID（MessageId NewType，落库优先用，
                保证 ACK 一致）；无则 new_message_id()
            conversation_id: 批次 3 目标会话 ID（SessionId NewType，贯通，供日志/归属追踪）

        Returns:
            (messages, tool_defs): LLM 消息列表 + 工具定义列表
        """
        if self._clock is None:
            raise RuntimeError("TimingClock 未初始化")
        with self._clock.span("prompt") as t:
            supports_mm = LLMFactory.supports_multimodal(self._llm.model)
            tts_provider = self._tts.provider_name if self._tts else ""
            messages = await self._prompt.build_messages(
                user_message=user_message, user_name=user_name,
                stt_emotion=stt_emotion, stt_events=stt_events,
                content=content,
                supports_multimodal=supports_mm,
                tts_provider=tts_provider,
            )
        user_content = content if content else [{"type": "text", "content": user_message}]
        # 系统唤醒消息不写入 session（Step 4: 唤醒模板不需要存放在 session 中）
        if not is_task_result:
            self._session.add_message(UserMessage(
                content=parts_from_dicts(user_content),
                message_id=user_message_id or new_message_id(),
                timestamp=_now_iso(),
            ))
        tool_defs = self._tool_adapter.definitions() if self._tool_adapter else None
        # 多模态 LLM 可直接看图，排除 describe_image 避免 LLM 只调用一次
        if tool_defs and supports_mm:
            tool_defs = [t for t in tool_defs if t.get("function", {}).get("name") != "describe_image"]
        if tool_defs is None:
            tool_defs = []
        # 附加 submit_task（如果 WorkflowEngine 和 DelegationPreparer 都已注入）
        if self._task_system is not None and self._delegation_preparer is not None:
            tool_defs = list(tool_defs) + [self._submit_task_definition()]
        logger.debug("提示词构建: %d ms, %d 条消息, %d 个工具", t.elapsed_ms, len(messages), len(tool_defs))
        return messages, tool_defs

    # 流式消费/事件分发已迁至 CognitiveStreaming mixin（CORE-MOD-P1-04）。

    # ---- Core loop: tool execution ----

    # 工具轮执行已迁至 CognitiveToolPhase mixin（CORE-MOD-P1-04）。

    # 轮次收尾已迁至 CognitiveFinalize mixin（CORE-MOD-P1-04）。

    # ---- Core loop: main orchestrator ----

    def _prepare_think_input(
        self,
        user_message: str,
        stt_emotion: str,
        stt_events: list[str] | None,
        is_task_result: bool,
        task_result_context: dict | None,
    ) -> str:
        """think_loop 前置：STT 情绪分析 + 后台任务结果唤醒消息。"""
        if self._engine:
            if stt_emotion and stt_emotion != "NEUTRAL":
                self._engine.analyze_user_stt(stt_emotion, stt_events)
        if is_task_result and task_result_context:
            return self._build_wake_up_message(task_result_context)
        return user_message

    def _resolve_empty_response(
        self,
        empty_retried: bool,
        messages: list[dict],
    ) -> tuple[str, str, list[str], bool]:
        """空响应守卫：首空注入 nudge 重试；仍空发兜底回复。

        Returns:
            (public_text, acc_text, all_sentences, retry)：retry=True 时调用方
            continue；否则返回兜底文本。
        """
        if not empty_retried:
            logger.warning("LLM 返回空响应（无文本、无工具调用），注入 nudge 重试一次")
            messages.append({"role": "system", "content": _EMPTY_RESPONSE_NUDGE})
            return "", "", [], True
        self._empty_response_count += 1
        _log = (logger.error
                if self._empty_response_count >= _EMPTY_RESPONSE_THRESHOLD
                else logger.warning)
        _log("LLM 重试后仍返回空响应（连续 %d 轮），发送兜底回复",
             self._empty_response_count)
        return (_EMPTY_RESPONSE_FALLBACK,
                _EMPTY_RESPONSE_FALLBACK,
                [_EMPTY_RESPONSE_FALLBACK],
                False)

    async def _start_turn_tracking(self, messages: list[dict], tool_defs: list[dict]) -> None:
        """prompt 记录 + token turn 启动 + typing + generation.started。"""
        if self._prompt_recorder is not None:
            self._prompt_recorder.record(
                messages=list(messages), tools=tool_defs, round_num=0, model=self._llm.model)
        if self._token_tracker is not None:
            self._token_tracker.start_turn()
        await self._output.typing(True)
        if self._generation_ctx is not None:
            await self._output.turn_started(self._generation_ctx)

    async def _think_loop(self, user_message: str, user_name: str,
                          stt_emotion: str = "", stt_events: list[str] | None = None,
                          _on_tool_calls: Callable | None = None,
                          content: list[dict] | None = None,
                          is_task_result: bool = False,
                          task_result_context: dict | None = None,
                          message_id: MessageId | None = None,
                          conversation_id: SessionId | None = None,
                          session: SessionMemory | None = None,
                          protocol_version: int = 1,
                          ) -> None:
        """ReAct 主循环：build → [stream → (execute | finalize)] × MAX_TOOL_ROUNDS。

        1. 前置：STT 情绪分析 + 渐进式唤醒消息构建
        2. 构建 prompt（_build_turn_prompt）→ 记录 prompt + 启动 token turn
        3. ReAct 循环（最多 MAX_TOOL_ROUNDS 轮）：
           - 流式调用 LLM（_stream_llm_response）
           - 有 tool_calls → 执行工具轮（_execute_tool_round）→ 继续循环
           - 无 tool_calls → 收尾（_finalize_turn）→ 退出
        4. 异常时清理 executing_tools + TTS

        Args:
            user_message: 用户纯文本输入
            user_name: 用户名
            stt_emotion: STT 语音情绪标签
            stt_events: STT 语音事件列表
            _on_tool_calls: 外部工具回调
            content: 多模态内容数组
            is_task_result: 是否为后台任务结果唤醒
            task_result_context: 任务结果上下文（含 original_intent / result）
            message_id: v2 预分配用户消息 ID（MessageId NewType，落库优先用）
            conversation_id: 批次 3 目标会话 ID（SessionId NewType，贯通到 build/execute/finalize）
            session: 批次 3 本轮目标会话（局部捕获，贯通到 stream/finalize）
            protocol_version: 来源协议版本（1=v1，2=v2）
        """
        user_message = self._prepare_think_input(
            user_message, stt_emotion, stt_events, is_task_result, task_result_context)

        # 1. 构建 prompt
        self._phase = TurnPhase.BUILD_PROMPT
        messages, tool_defs = await self._build_turn_prompt(
            user_message, user_name, content, stt_emotion, stt_events, is_task_result,
            user_message_id=message_id, conversation_id=conversation_id)

        await self._start_turn_tracking(messages, tool_defs)

        # 2. ReAct 循环
        empty_retried = False
        try:
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                self._phase = TurnPhase.STREAMING
                acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think, \
                    blocks, public_text = \
                    await self._stream_llm_response(
                        messages, tool_defs, round_num,
                        session_id=(session.session_id if session is not None else None),
                        session=session)

                # 空 LLM 响应守卫：无文本、无工具调用 → 注入 nudge 重试一次；
                # 重试后仍空 → 兜底回复（同时注入 all_sentences，保证 session 归档
                # 与前端显示的文本一致——build_parts([]) 会产出空 parts）。
                if not raw_tool_calls and not public_text.strip():
                    public_text, acc_text, all_sentences, retry = \
                        self._resolve_empty_response(empty_retried, messages)
                    if retry:
                        empty_retried = True
                        continue
                else:
                    # 正常响应（有文本或工具调用）——重置空响应计数
                    self._empty_response_count = 0

                if raw_tool_calls and round_num < self.MAX_TOOL_ROUNDS:
                    self._phase = TurnPhase.EXECUTING_TOOLS
                    await self._execute_tool_round(
                        messages, acc_text, raw_tool_calls, _on_tool_calls,
                        conversation_id=conversation_id)
                    # 显式模式切换信号：帮助 LLM 从"工具执行模式"切回"对话模式"
                    messages.append({"role": "system", "content": "[TOOL_EXECUTED]"})
                    continue

                self._phase = TurnPhase.FINALIZING
                await self._finalize_turn(
                    acc_text, all_sentences, iv_think, speaking_started, user_message,
                    round_num, blocks, public_text,
                    conversation_id=conversation_id, session=session)
                return

        except (Exception, asyncio.CancelledError):
            self._executing_tools.clear()
            self._tts_mgr.cancel_all()
            await self._output.typing(False)
            raise

    # ---- Post-response ----

    async def _after_response(self, user_message: str, assistant_response: str, *,
                              session: SessionMemory | None = None,
                              engine: "CharacterEngine | None" = None) -> None:
        """Post-response 钩子：沉淀记忆、性格反思、保存会话。

        以 fire-and-forget 方式由 _finalize_turn 调用。
        执行顺序：Consolidator 提取记忆 → CharacterReflector 缓冲 → MemoryReflector 去重 → 保存 session。

        Args:
            user_message: 用户原始输入
            assistant_response: 剥离标签后的公开回复文本
            session: 批次 3 本轮目标会话（局部捕获——save 归属，防 active 切换后
                in-flight 异步延续落错会话；None 时回退 self._session）
            engine: 批次 3 本轮 CharacterEngine（局部捕获——buffer_message 归属；
                None 时回退 self._engine）
        """
        # shutdown 门禁——stop() 开始后（cognitive task 已 cancel、LLM/session repo 将关闭），
        # 本轮 turn 收尾新创建的 after_response task 立即退出，避免与已关闭资源并发。
        # 否则最后轮记忆沉淀丢失、session 降级写 JSONL。
        if self._shutting_down:
            logger.debug("after_response 跳过——agent 正在关闭")
            return

        # 局部捕获：不读 self._session / self._engine——resolver 切走后
        # 本轮 in-flight 异步延续仍写发起会话 / 发起引擎。
        target_session = session if session is not None else self._session
        target_engine = engine if engine is not None else self._engine
        # C3/P1-10：在首个 await 前固化发起会话的 reflector——consolidate await 期间
        # session switch 会 rebind engine._reflector，晚读会写到新会话的 reflector。
        target_reflector = getattr(target_engine, "_reflector", None) if target_engine else None

        items = []  # 初始化，避免 NameError
        if self._consolidator:
            try:
                t0 = time.perf_counter()
                items = await self._consolidator.consolidate(user_message, assistant_response)
                if items:
                    logger.debug("记忆沉淀: %d 条 (%d ms)", len(items),
                                 int((time.perf_counter() - t0) * 1000))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("记忆沉淀失败", exc_info=True)
        if target_engine:
            try:
                # C3/P1-10：用固化 reflector 缓冲——A 会话内容只进 A reflector；
                # task 登记统一 tail registry（shutdown/delete 可 drain）
                task = target_engine.buffer_message(
                    user_message, assistant_response, reflector=target_reflector)
                if task is not None:
                    self._pending_after_response.append(task)
            except Exception:
                logger.warning("性格反思缓冲失败", exc_info=True)
        # 更新 MemoryReflector 计数器
        if self._memory_reflector:
            self._memory_reflector.increment_round()
            if items:
                self._memory_reflector.increment_new_memories(len(items))
            # 检查是否需要触发去重（非阻塞，快速判断）
            try:
                await self._memory_reflector.check_and_reflect()
            except Exception:
                logger.warning("MemoryReflector check failed", exc_info=True)
        # 异步保存会话（fire-and-forget，不阻塞本轮）
        try:
            await target_session.save()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("会话保存失败", exc_info=True)

    async def flush_after_response(self, timeout: float = 5.0) -> None:
        """等待所有 pending _after_response task 完成。

        shutdown 时由 lifecycle 调用，防止内存沉淀和 session 保存丢失。

        Args:
            timeout: 每个 task 的最长等待时间（秒）。
        """
        pending = [t for t in self._pending_after_response if not t.done()]
        if not pending:
            return
        logger.info("等待 %d 个 after-response task 完成...", len(pending))
        for t in pending:
            try:
                await asyncio.wait_for(t, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("after-response task 超时，取消")
                t.cancel()
            except Exception:
                logger.warning("after-response task 异常", exc_info=True)
        self._pending_after_response.clear()

    @staticmethod
    def _build_wake_up_message(ctx: dict) -> str:
        """构建渐进式唤醒消息。

        注入当前时间避免 LLM 产生时间幻觉（如凌晨说"今早"）。
        v1: 统一礼貌询问。后续 TODO(v2): 检查对话上下文动态调整。

        Args:
            ctx: 任务结果上下文字典，含 original_intent（原始意图）和 result（结果摘要）

        Returns:
            格式化的系统唤醒消息字符串
        """
        now = datetime.datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        return (
            f"[系统通知] 当前时间: {time_str}（{weekday}）。"
            f"你此前委派的后台任务"
            f"（{ctx.get('original_intent', '')}）"
            f"已执行完毕。结果摘要: {ctx.get('result', '')}。"
            f"请自然地告知用户任务已完成，询问是否现在汇报详细结果，还是稍后再说。"
        )



    # ---- State transitions ----

    async def _transition_idle(self) -> None:
        """等待 TTS 完成 → 检查后台任务 → 转换到 IDLE/IDLE_BG。"""
        active_tasks: list = []
        if self._on_background_task is not None and self._task_system is not None:
            active_tasks = await self._task_system.list_active_tasks()
        await self.phases.finish_turn(has_background_tasks=bool(active_tasks))

    async def _cancel_desktop_calls(self) -> None:
        """中断/取消回合时取消在途 desktop 调用（interrupt 语义，spec §8）。

        经 ToolAdapter.tool_context.services 取桥（Task 6/8：start() 时 extra_services
        注入 desktop_bridge）。无桥 / 无在途调用 → no-op。遍历 pending_calls() 对
        在途（pending，未接手）调用逐 cancel_call——桥内 future 立即 resolve(CANCELLED)
        + repo mark cancelled，回合取消后不再挂起到 120s 超时。

        offline 调用跳过（D-o）：断线标 offline 属断开规则，与 generation cancelled
        不混用——已接手的 offline 调用靠 Electron 重连后重传 result（offline → 终态
        迁移）闭环，取消会破坏该对账。
        """
        if self._tool_adapter is None:
            return
        bridge = self._tool_adapter.tool_context.services.get("desktop_bridge")
        if bridge is None:
            return
        try:
            for doc in await bridge.pending_calls():
                if doc.get("status") == STATUS_OFFLINE:
                    continue  # D-o：断线标 offline，保重传结果闭环
                call_id = doc.get("call_id")
                if call_id:
                    await bridge.cancel_call(call_id)
        except Exception:
            logger.debug("取消在途 desktop 调用失败（桥不可用/断连竞态）", exc_info=True)

    async def _handle_interrupt(self, source: str | None = None) -> None:
        """中断后的清理：取消 TTS 任务、回到 IDLE 状态。

        Args:
            source: 中断来源（cancel_current 存入 _interrupt_source，此处消费；
                显式传入优先）。Task 9 mark 拆分据此区分「语音打断」与「断线」。
        """
        if source is None:
            source = self._interrupt_source
        self._interrupt_source = None
        if self.status.is_completely_idle:
            return
        # 批次 3 review 修复（F9）：本回合已 mark(completed)（turn_completed 已发、
        # TTS 窗口内打断）→ 不再发 CANCELLED/重标 cancelled——客户端已收 completed，
        # 打断仅做 TTS 清理回 IDLE。
        if self._generation_completed:
            self._executing_tools.clear()
            self._tts_mgr.cancel_all()
            await self._output.typing(False)
            await self.phases.handle_interrupt()
            logger.debug("Interrupt after generation.completed — 保持 completed，仅清理")
            return
        # 批次 2：活动 generation 且未被错误路径覆盖 → v2 发 generation.failed{CANCELLED}
        # （终态唯一：错误路径已发 failed 则不重发；v1 不发，保持现状打断无 ❌）
        if self._generation_ctx is not None and not self._turn_failed_emitted:
            try:
                await self._output.turn_cancelled(self._generation_ctx)
                self._turn_failed_emitted = True
            except Exception:
                logger.debug("turn_cancelled 发射失败", exc_info=True)
            # 批次 3（BE-024）：中断来源分派——disconnect → interrupted；其余
            # （vad/button/新输入等）→ cancelled。终态唯一：错误路径已标 failed 不覆盖。
            if self._generation_registry is not None:
                try:
                    status = "interrupted" if source == "disconnect" else "cancelled"
                    await self._generation_registry.mark(
                        self._generation_ctx.generation_id, status=status)
                except Exception:
                    logger.debug("generation mark %s 失败", status, exc_info=True)
        # 批次 4（spec §8）：取消在途 desktop 调用（interrupt 语义，无桥/无在途则 no-op）
        await self._cancel_desktop_calls()
        self._executing_tools.clear()
        self._tts_mgr.cancel_all()
        await self._output.typing(False)
        await self.phases.handle_interrupt()
        logger.debug("Interrupt complete (source=%s) — agent is IDLE", source or "unknown")
