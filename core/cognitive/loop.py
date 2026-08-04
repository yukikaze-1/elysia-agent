"""CognitiveLoop — the core ReAct scheduler.

Orchestrates THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE.
Uses structured streaming for sub-100ms TTFT and simultaneous tool-call detection.

Sub-modules:
  text/stream_processor.py     — streaming parser coordinator (tokenizer + splitter + blocks)
  text/parts.py                — sentence/block interleave → TextPart + public_text rebuild
  adapter/tts_adapter.py       — TTS stream manager + emoji queuing
  adapter/tool_call_parser.py  — merge/normalize streaming tool_call deltas
  utils/text_utils.py          — content text extraction + MessageObj
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from dataclasses import asdict
from enum import Enum
from typing import Callable, TYPE_CHECKING

from core.utils import new_message_id, new_generation_id

from core.cognitive.generation import GenerationContext
from core.cognitive.timing import TimingClock

from Module.output import OutputManager, ChannelCapability
from Module.tts import TTSBackend

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from core.utils import set_request_id, get_request_id, log_conversation
from Module.tool.protocol import ToolResult
from core.adapter.prompt_assembler import PromptAssembler
from core.memory.session import SessionMemory
from core.text.stream_processor import StreamProcessor, StreamEvent, StreamEventType
from core.adapter.llm_adapter import LLMAdapter
from core.memory.consolidator import MemoryConsolidator
from Module.llm.registry import LLMFactory

if TYPE_CHECKING:
    from core.cognitive.delegation import DelegationPreparer
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.cognitive.user_profile import UserProfile
    from core.monitoring.token_tracker import TokenTracker
    from core.debug.prompt_recorder import PromptRecorder
    from core.debug.response_recorder import ResponseRecorder
    from core.personality.engine import CharacterEngine
    from core.adapter.tool_adapter import ToolAdapter
    from core.memory.memory_reflector import MemoryReflector

from core.text.parts import BlockRef, build_parts
from core.adapter.tts_adapter import TTSStreamManager
from core.adapter.tool_call_parser import merge_stream_tool_calls, normalize_tool_calls
from Module.workflow_contract import WorkflowId
from core.utils.text_utils import content_text_from_content, MessageObj
from core.schema.message import UserMessage, AssistantMessage, ToolResultMessage
from core.schema.token import TokenUsageSummary
from core.schema.tool import ToolCall, ToolCallId

from core.schema.part import TextPart
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


from core.cognitive.tools import CognitiveTools


class TurnPhase(str, Enum):
    """CognitiveLoop 单轮处理阶段 — 内部追踪，不暴露给 FSM 外部消费者。"""
    BUILD_PROMPT = "build_prompt"
    STREAMING = "streaming"
    EXECUTING_TOOLS = "executing_tools"
    FINALIZING = "finalizing"


async def _apply_mood(raw: str, engine: "CharacterEngine", tts_mgr: TTSStreamManager,
                      output) -> None:
    """解析 <mood> 标签内容，更新 CharacterState + 统一心情发射 + TTS emotion。

    支持格式: "情绪文本" 或 "情绪文本|强度"（强度 0.1–1.0）。
    同时更新 CharacterEngine state.mood / state.mood_intensity，
    经 output.mood() 统一发射（v1 发 expression、v2 捕获不发——mood 由 turn_completed 落位），
    设置 TTS 情绪参数。

    Args:
        raw: <mood> 标签内的原始文本
        engine: CharacterEngine 实例
        tts_mgr: TTSStreamManager 实例
        output: OutputManager（统一心情发射）
    """
    if "|" in raw:
        mood_text, intensity_str = raw.rsplit("|", 1)
        try:
            intensity = float(intensity_str.strip())
            intensity = min(1.0, max(0.1, intensity))
        except ValueError:
            intensity = 0.5
    else:
        mood_text = raw.strip()
        intensity = 0.5

    if not mood_text:
        logger.debug("心情: <mood> 标签为空，跳过")
        return

    prev = engine.state.mood
    engine.state.mood = mood_text
    engine.state.mood_intensity = intensity
    await output.mood(mood_text, intensity)
    tts_mgr.set_emotion(mood_text)
    if prev != mood_text:
        logger.info("心情: %s → %s (%.0f%%)", prev, mood_text, intensity * 100)
    else:
        logger.info("心情: %s (%.0f%%)", mood_text, intensity * 100)


class CognitiveLoop(CognitiveTools):
    """ReAct cognitive loop with streaming delivery and tool-call detection."""

    MAX_TOOL_ROUNDS = 5

    def __init__(
        self,
        llm: LLMAdapter,
        fsm: AgentFSM,
        event_bus: EventBus,
        output: OutputManager,
        prompt: PromptAssembler,
        session: SessionMemory,
        tool_adapter: ToolAdapter | None = None,
        character_engine: "CharacterEngine | None" = None,
        consolidator: MemoryConsolidator | None = None,
        tts: TTSBackend | None = None,
        token_tracker: "TokenTracker | None" = None,
        prompt_recorder: "PromptRecorder | None" = None,
    ):
        """初始化认知循环。

        Args:
            llm: LLM 适配器，负责流式/非流式调用
            fsm: Agent 状态机，驱动 THINKING/EXECUTING/SPEAKING/IDLE 转换
            event_bus: 事件总线，广播 StateChangeData
            output: 多通道输出管理器
            prompt: 系统提示词组装器
            session: 会话记忆
            tool_adapter: 工具适配器（ToolAdapter 实例）
            character_engine: 角色引擎（4 层性格模型）
            consolidator: 记忆沉淀器，提取对话记忆
            tts: TTS 后端，经 TTSStreamManager 代理
            token_tracker: LLM token 用量追踪器
            prompt_recorder: LLM prompt 记录器（供 debug 面板）
        """
        self._llm = llm
        self._fsm = fsm
        self._bus = event_bus
        self._output = output
        self._prompt = prompt
        self._session = session
        self._tool_adapter = tool_adapter
        self._req_counter: int = 0
        self._character_engine = character_engine
        self._consolidator = consolidator
        self._memory_reflector: MemoryReflector | None = None  # 由 configure() 注入
        self._tts = tts  # 保留供 agent.py stop() 读取
        self._token_tracker = token_tracker
        self._prompt_recorder = prompt_recorder
        # 批次 2：当前回合 GenerationContext（process() 创建，_dispatch_stream_event/
        # _finalize_turn/错误路径使用）；无活动回合为 None
        self._generation_ctx: GenerationContext | None = None
        # 批次 2：本回合是否已发 generation.failed（终态唯一守卫——错误路径已发则打断不重发 CANCELLED）
        self._turn_failed_emitted: bool = False
        self._response_recorder: ResponseRecorder | None = None  # 由 configure() 注入
        self._ttft_start: float = 0.0
        self._mood_applied: bool = False
        self._process_lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None  # 保留供 agent.py stop() 读取
        self._pending_after_response: list[asyncio.Task] = []  # shutdown 时等待
        self._shutting_down: bool = False  # shutdown 门禁——after_response 检查后提前退出
        # TTS streaming manager — extracted sub-system
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
        self._clock: TimingClock | None = None # 当前轮次的计时器（process() 设置，finally 清除）
        self._phase: TurnPhase | None = None   # 当前轮次处理阶段（供 debug 面板）
        self._phases = None  # CognitivePhaseController（由 agent.py 注入，或懒加载）
        self._status = None  # AgentStatus（由 agent.py 注入，或懒加载）
        self._empty_response_count = 0  # 连续空响应轮数（超阈值升 error）

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
    def phases(self, value):
        self._phases = value

    @property
    def status(self):
        """AgentStatus — 由 agent.py 注入（或直接构造时懒加载回退）。"""
        if self._status is None:
            from core.status import FSMBackedStatus
            self._status = FSMBackedStatus(self._fsm)
        return self._status

    @status.setter
    def status(self, value):
        self._status = value

    # ---- Public API ----

    def cancel_current(self) -> bool:
        """取消当前认知任务（如有）。返回 True 表示确实取消了任务。"""
        if self._current_task is not None and not self._current_task.done():
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

    def configure(self, **kwargs) -> None:
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

    # ---- Main entry ----

    async def process(self, user_message: str = "", user_name: str = "妖梦",
                       stt_emotion: str = "", stt_events: list[str] | None = None,
                       _on_tool_calls: Callable | None = None,
                       content: list[dict] | None = None,
                       is_task_result: bool = False,
                       task_result_context: dict | None = None,
                       message_id: str | None = None,
                       protocol_version: int = 1,
                       ) -> None:
        """处理用户消息，驱动一轮完整的 ReAct 认知循环。

        Args:
            user_message: 纯文本输入（STT 识别结果或键盘输入），兼容旧调用
            user_name: 用户名，注入系统提示词
            stt_emotion: STT 语音情绪标签（如 "HAPPY", "SAD", "NEUTRAL"）
            stt_events: STT 语音事件列表（如 ["laughter", "sigh"]）
            _on_tool_calls: 外部工具回调（测试/自定义 dispatch），None 时走 _execute_tools_directly
            content: 多模态内容数组（ChatMessageV2 content），优先级高于 user_message
            is_task_result: 是否为后台任务结果触发的唤醒
            task_result_context: 后台任务结果上下文，含 original_intent / result 等字段
            message_id: v2 预分配用户消息 ID（落库优先用，ACK 一致）；v1 无则 None
            protocol_version: 来源协议版本（1=v1，2=v2），默认 1
        """
        async with self._process_lock:
            if not self.status.is_accepting_input:
                logger.warning("Agent not accepting input — skipping turn")
                return

            rid = get_request_id()
            if rid == "-":
                # Fallback: idle monitor proactive messages have no request_id yet
                self._req_counter += 1
                rid = f"P{self._req_counter:03d}"
                set_request_id(rid)

            # 生成 trace_id（跨 Agent、跨工具串联）
            if self._tool_adapter is not None:
                self._tool_adapter.tool_context.trace_id = new_message_id()

            self._tts_mgr.reset_for_turn()
            clock = TimingClock()
            clock.start()
            self._clock = clock  # 供 _think_loop / _after_response 使用

            # 提取纯文本给日志
            log_text = user_message
            if content:
                log_text = content_text_from_content(content)
                if not log_text:
                    log_text = "[多模态消息]"

            # 用户输入到达 — 记录时间和内容
            emo_tag = f" [{stt_emotion}]" if stt_emotion and stt_emotion != "NEUTRAL" else ""
            logger.info(">>> 用户输入 (%s)%s: %s", user_name, emo_tag, log_text[:200])
            log_conversation("user", user_name, log_text)

            await self.phases.enter_thinking()

            # 批次 2：每回合创建 GenerationContext——generation_id / assistant_message_id 预分配，
            # 供统一回合事件与 _finalize_turn 落库（assistant 父消息 ID 贯通）。
            self._generation_ctx = GenerationContext(
                generation_id=new_generation_id(),
                user_message_id=message_id or "",
                assistant_message_id=new_message_id(),
            )
            self._turn_failed_emitted = False

            self._current_task = asyncio.current_task()
            try:
                await self._think_loop(user_message, user_name,
                                       stt_emotion=stt_emotion, stt_events=stt_events,
                                       _on_tool_calls=_on_tool_calls, content=content,
                                       is_task_result=is_task_result,
                                       task_result_context=task_result_context,
                                       message_id=message_id,
                                       protocol_version=protocol_version)
                clock.stop()
                logger.info("<<< 回复生成完成 — %s", clock.summary())
                # 存入 ring buffer 供 /debug 面板
                self._timing_history.append({
                    "total_ms": clock.total_ms,
                    "spans": dict(clock._spans),
                })
                if len(self._timing_history) > 100:
                    self._timing_history = self._timing_history[-100:]
            except asyncio.CancelledError:
                logger.info("<<< 用户打断")
                await self._handle_interrupt()
            except Exception as e:
                # 区分 LLM 层失败与代码 bug——代码 bug 不应误导用户为「LLM 调用失败」（O7）
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
                # 通知前端错误信息：统一回合事件（v1 复刻 ❌ delta + send("")；v2 发 generation.failed）
                err_msg = str(e)[:200]
                if self._generation_ctx is not None:
                    try:
                        await self._output.turn_failed(
                            code, err_msg, retryable, label, self._generation_ctx)
                        self._turn_failed_emitted = True  # 终态唯一：打断不重发 CANCELLED
                    except Exception:
                        logger.debug("turn_failed 发射失败", exc_info=True)
                await self._handle_interrupt()
            finally:
                self._clock = None

    # ---- Core loop: prompt construction ----

    async def _build_turn_prompt(
        self, user_message: str, user_name: str,
        content: list[dict] | None,
        stt_emotion: str, stt_events: list[str] | None,
        is_task_result: bool,
        user_message_id: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """构建本轮 LLM 调用的 messages + tool definitions。

        Args:
            user_message: 用户纯文本输入
            user_name: 用户名
            content: 多模态内容数组（优先于 user_message）
            stt_emotion: STT 情绪标签
            stt_events: STT 事件列表
            is_task_result: 是否为任务结果唤醒（决定是否写入 session）
            user_message_id: v2 预分配用户消息 ID（落库优先用，保证 ACK 一致）；无则 new_message_id()

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

    # ---- Core loop: LLM streaming ----

    async def _stream_llm_response(
        self, messages: list[dict], tool_defs: list[dict], round_num: int,
    ) -> tuple[str, list[dict], bool, list[str], str, list[BlockRef], str]:
        """流式调用 LLM，逐 chunk 分发 StreamProcessor 事件。

        驱动 StreamProcessor 状态机，将解析出的文本增量转为 TTS/UI 副作用。
        同时累积 tool_call delta 供后续 normalize。flush() 后在本方法内重建
        blocks 与 public_text（proc 是局部变量，`_finalize_turn` 只收返回值）。

        Args:
            messages: 已构建的 LLM 消息列表（含 system + history + user）
            tool_defs: 工具定义列表（OpenAI function calling 格式）
            round_num: 当前工具调用轮次（0 = 首轮）

        Returns:
            (acc_text, raw_tool_calls, speaking_started, all_sentences,
             inner_voice, blocks, public_text):
            - acc_text: LLM 原始输出全文（含标签）
            - raw_tool_calls: 合并后的 streaming tool_call delta 列表
            - speaking_started: 是否已开始语音/文本输出
            - all_sentences: 所有分句文本列表
            - inner_voice: 内心独白文本（多个 <inner_voice> 以换行合并）
            - blocks: BlockRef 列表 —— (kind, content, lang, after_sentence_idx)
            - public_text: 按 token 流重建的公开文本（去 META，保留 PAUSE/STAGE_CN/块内容）
        """
        acc_text = ""
        raw_tool_calls: list[dict] = []
        speaking_started = False
        all_sentences: list[str] = []
        iv_think_buf_holder: list[str] = []
        t_think_start = time.perf_counter()
        self._ttft_start = t_think_start

        # 工具循环的后续轮次也捕获 prompt
        if round_num > 0 and self._prompt_recorder is not None:
            self._prompt_recorder.record(
                messages=list(messages),
                tools=tool_defs,
                round_num=round_num,
                model=self._llm.model,
            )

        self._mood_applied = False
        proc = StreamProcessor()

        stream = self._llm.stream_think(messages, tools=tool_defs,
            extra_body={"thinking": {"type": "disabled"}})
        try:
            async for chunk in stream:
                delta: str = chunk.get("content", "")
                tc_deltas = chunk.get("tool_calls")

                if delta:
                    acc_text += delta
                    for event in proc.feed(delta):
                        speaking_started, all_sentences = (
                            await self._dispatch_stream_event(
                                event,
                                speaking_started=speaking_started,
                                all_sentences=all_sentences,
                                iv_think_buf_holder=iv_think_buf_holder,
                            )
                        )

                if tc_deltas:
                    merge_stream_tool_calls(raw_tool_calls, tc_deltas)

                if "usage" in chunk:
                    if self._token_tracker is not None:
                        await self._token_tracker.record(
                            usage=chunk["usage"],
                            model=self._llm.model,
                            round_num=round_num,
                        )
        except asyncio.CancelledError:
            raise

        # Flush StreamProcessor
        for event in proc.flush():
            speaking_started, all_sentences = (
                await self._dispatch_stream_event(
                    event,
                    speaking_started=speaking_started,
                    all_sentences=all_sentences,
                    iv_think_buf_holder=iv_think_buf_holder,
                )
            )

        # flush 后收集结构化块与公开文本（proc 是局部变量，只能在此重建）
        blocks = proc.get_blocks()
        public_text = proc.rebuild_public_text()
        # 多个 <inner_voice> 累积合并（见 _dispatch_stream_event）
        iv_think = "\n".join(iv_think_buf_holder)

        return acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think, blocks, public_text

    # ---- Core loop: tool execution ----

    async def _execute_tool_round(
        self, messages: list[dict], acc_text: str,
        raw_tool_calls: list[dict], _on_tool_calls: Callable | None,
    ) -> None:
        """执行一轮工具调用：submit_task 拦截 → Gate 校验 → dispatch → 结果注入。

        副作用（全部就地修改）：
        - messages 追加 assistant entry + tool result messages
        - session 写入 AssistantMessage + ToolResultMessage
        - output 推送 tool_executing / tool_done action
        - fsm 切换 EXECUTING → THINKING

        Args:
            messages: LLM 消息列表（in-place 修改，追加 assistant + tool results）
            acc_text: LLM 原始输出全文
            raw_tool_calls: streaming 累积的 tool_call delta（合并后未归一化）
            _on_tool_calls: 外部工具回调（测试/自定义 dispatch），None 时走 _execute_tools_directly
        """
        # 切断气泡 — TTS tasks 独立运行，不 cancel
        await self.phases.enter_executing()

        normalized = normalize_tool_calls(raw_tool_calls)

        # 分离 submit_task 和其他工具——submit_task 走专用路径
        if self._delegation_preparer is not None:
            submit_tcs = [tc for tc in normalized if tc.function.name == "submit_task"]
            other_tcs  = [tc for tc in normalized if tc.function.name != "submit_task"]
        else:
            submit_tcs = []
            other_tcs  = normalized

        # 构建 assistant_entry（先于 tool results，OpenAI 协议要求）
        assistant_entry: dict = {"role": "assistant", "content": acc_text or None}
        if normalized:
            assistant_entry["tool_calls"] = [tc.model_dump(mode="json") for tc in normalized]
        messages.append(assistant_entry)

        # Gate: 校验 tool_calls 合法性，拦截未知工具名
        if not self._gate_tool_calls(normalized, messages):
            await self.phases.return_to_thinking()  # 恢复 THINKING，避免 FSM 卡在 EXECUTING 触发非法转换
            return  # gate 已注入 failure messages，跳过 dispatch

        # submit_task 工具调用——拦截处理
        if submit_tcs:
            for tc in submit_tcs:
                result = await self._handle_submit_task(tc)
                tc_id = tc.id
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result.llm_content,
                })
                await self._output.action("tool_done", params={
                    "tool": "submit_task",
                    "call_id": tc_id,
                    "success": result.success,
                    "content": result.content or "",
                    "error": result.error,
                    "error_type": None,
                    "duration_ms": result.duration_ms,
                    "truncated": result.truncated,
                })
                self._session.add_message(ToolResultMessage(
                    content=[TextPart(content=result.content or "")],
                    tool_call_id=tc.id,
                    tool_name="submit_task",
                    success=result.success,
                    error=result.error,
                    error_type=None,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                ))
                # 注册后台任务
                if result.success and result.async_task_id and self._on_background_task and self._task_system:
                    mailbox = self._task_system.get_task_mailbox(WorkflowId(result.async_task_id))
                    if mailbox:
                        dispatched_now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        self._on_background_task(
                            result.async_task_id, mailbox, intent=(
                                tc.function.parsed.get("intent", "")
                            ), dispatched_at=dispatched_now,
                        )

            dispatch_tcs = other_tcs
        else:
            dispatch_tcs = normalized

        # 工具调用消息写入 Session
        self._session.add_message(AssistantMessage(
            content=[TextPart(content=acc_text)] if acc_text else [],
            message_id=new_message_id(),
            tool_calls=normalized if normalized else None,
        ))

        # Emit tool_executing actions for UI (before dispatch)
        for tc in dispatch_tcs:
            func_name = tc.function.name
            call_id = tc.id
            args = tc.function.parsed
            # arguments 可能是合法 JSON 但非 dict（list/str/int），跳过避免 AttributeError
            if not isinstance(args, dict):
                args = {}
            self._executing_tools.append(func_name)
            await self._output.action("tool_executing", params={
                "tool": func_name,
                "call_id": call_id,
                "args": {k: str(v) for k, v in args.items()},
            })

        if self._clock is None:
            raise RuntimeError("TimingClock 未初始化")
        with self._clock.span("tools"):
            if _on_tool_calls is not None:
                results = await _on_tool_calls(dispatch_tcs)
            else:
                results = await self._execute_tools_directly(dispatch_tcs)

        for item in results:
            if isinstance(item, BaseException):
                # gather(return_exceptions=True) 捕获的非 Exception 级异常
                #（KeyboardInterrupt/SystemExit 等）——工具结果被丢弃意味着
                # 该 tool_call 不会得到对应的 tool response，记录日志便于诊断。
                logger.warning("工具执行返回异常，跳过该工具结果: %r", item)
                continue
            tc, tool_result = item
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result.llm_content,
            })
            error_type_str = tool_result.error_type.value if tool_result.error_type else None
            await self._output.action("tool_done", params={
                "tool": tc.function.name,
                "call_id": tc.id,
                "success": tool_result.success,
                "content": tool_result.content or "",
                "error": tool_result.error,
                "error_type": error_type_str,
                "duration_ms": tool_result.duration_ms,
                "truncated": tool_result.truncated,
            })
            self._session.add_message(ToolResultMessage(
                content=[TextPart(content=tool_result.content or "")],
                tool_call_id=tc.id,
                tool_name=tc.function.name,
                success=tool_result.success,
                error=tool_result.error,
                error_type=tool_result.error_type,
                duration_ms=tool_result.duration_ms,
                truncated=tool_result.truncated,
            ))

        # Clean up executing tools tracking
        for tc in dispatch_tcs:
            func_name = tc.function.name
            try:
                self._executing_tools.remove(func_name)
            except ValueError:
                pass

        await self.phases.return_to_thinking()

    # ---- Core loop: finalize ----

    async def _finalize_turn(
        self, acc_text: str, all_sentences: list[str],
        iv_think_buf: str, speaking_started: bool,
        user_message: str, round_num: int,
        blocks: list[BlockRef], public_text: str,
    ) -> None:
        """收尾本轮：mood fallback → 归档 session → 推送结构化块 → after_response。

        public_text 已由 `_stream_llm_response` 重建（去 META，保留 PAUSE/STAGE_CN/
        块内容），不再在此 find-strip。构建 AssistantMessage 写入 session，
        推送 code/markdown 块到前端，最后触发 fire-and-forget _after_response。

        Args:
            acc_text: LLM 原始输出全文（含标签）
            all_sentences: 所有分句文本列表（用于 block 构建）
            iv_think_buf: 内心独白文本（多个 <inner_voice> 换行合并）
            speaking_started: 是否已开始语音/文本输出
            user_message: 用户原始输入（传给 _after_response）
            round_num: 当前轮次编号（传给 response_recorder）
            blocks: BlockRef 列表 —— (kind, content, lang, after_sentence_idx)
            public_text: 重建后的公开文本（供 analyze_assistant / log_conversation / after_response）
        """
        # 注意：不在此处 flush_emojis——最后一句 TTS 的 pop(sid-1) 在 wait_all 后才执行。
        # emoji 依附顺序由 finish_turn → wait_all → flush_emojis 保证（见 _transition_idle）。

        # Fallback: LLM 未输出 <mood> → 关键词检测
        if self._character_engine and not self._mood_applied:
            logger.debug("心情: <mood> 未检测到，使用关键词 fallback")
            self._character_engine.analyze_user(user_message)
            self._tts_mgr.set_emotion(self._character_engine.state.mood)

        if public_text.strip():
            if self._character_engine:
                self._character_engine.analyze_assistant(public_text)
            if not speaking_started:
                await self._output.typing(False)
                msg = MessageObj(content=public_text)
                await self._output.send(msg)
        if public_text.strip():
            assistant_parts, content_type = build_parts(all_sentences, blocks)
            # 批次 2：assistant 父消息 ID 用 generation 开始预分配的 ID（贯通 ACK/session 对齐）
            assistant_msg_id = (self._generation_ctx.assistant_message_id
                                if self._generation_ctx is not None else new_message_id())
            self._session.add_message(AssistantMessage(
                content=assistant_parts,
                message_id=assistant_msg_id,
                inner_voice=iv_think_buf.strip() or None,
                block_index=content_type,
                token_usage=TokenUsageSummary(
                    rounds=list(self._token_tracker.current_turn_usages),
                    turn_total=sum(r.total_tokens for r in self._token_tracker.current_turn_usages),
                ) if self._token_tracker and self._token_tracker.current_turn_usages else None,
            ))
            if self._response_recorder is not None:
                self._response_recorder.record(
                    raw_text=acc_text,
                    inner_voice=iv_think_buf.strip() or None,
                    round_num=round_num,
                )

            log_conversation("assistant", self._prompt._character.name, public_text)
            if iv_think_buf.strip():
                logger.info("💭 %s", iv_think_buf.strip())
                log_conversation("inner_voice", self._prompt._character.name,
                                 iv_think_buf.strip())
            task = asyncio.create_task(
                self._after_response(user_message, public_text),
                name="after-response")
            self._pending_after_response.append(task)
            # 清理已完成的 task，避免列表无限增长
            self._pending_after_response = [
                t for t in self._pending_after_response if not t.done()
            ]

            # 推送结构化块到前端（参数直接从 blocks 取——修复旧 getattr(...,"language","") 恒空）
            for kind, content, lang, _after in blocks:
                params: dict = {
                    "block_type": kind,
                    "content": content.strip(),
                }
                if kind == "code":
                    params["language"] = lang
                await self._output.command("block", params=params)

        # 批次 2：父完成（v2 generation.completed——usage/model/mood 落位；v1 NO-OP，token_usage 已实时广播）
        if self._generation_ctx is not None:
            try:
                from frontend_contract import TokenRound, TokenUsage
                rounds = [
                    TokenRound.model_validate(r.model_dump(mode="json"))
                    for r in (self._token_tracker.current_turn_usages
                              if self._token_tracker is not None else [])
                ]
                usage = TokenUsage(
                    rounds=rounds,
                    turn_total=sum(r.total_tokens for r in
                                   (self._token_tracker.current_turn_usages
                                    if self._token_tracker is not None else [])),
                )
                mood = (self._character_engine.state.mood
                        if self._character_engine is not None else None)
                await self._output.turn_completed(
                    "stop", usage, self._llm.model, mood, self._generation_ctx)
            except Exception:
                logger.debug("turn_completed 发射失败", exc_info=True)

        await self._transition_idle()

    # ---- Core loop: main orchestrator ----

    async def _think_loop(self, user_message: str, user_name: str,
                          stt_emotion: str = "", stt_events: list[str] | None = None,
                          _on_tool_calls: Callable | None = None,
                          content: list[dict] | None = None,
                          is_task_result: bool = False,
                          task_result_context: dict | None = None,
                          message_id: str | None = None,
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
            message_id: v2 预分配用户消息 ID（落库优先用）
            protocol_version: 来源协议版本（1=v1，2=v2）
        """
        # 前置：STT 情绪分析 + 渐进式唤醒
        if self._character_engine:
            if stt_emotion and stt_emotion != "NEUTRAL":
                self._character_engine.analyze_user_stt(stt_emotion, stt_events)
        if is_task_result and task_result_context:
            user_message = self._build_wake_up_message(task_result_context)

        # 1. 构建 prompt
        self._phase = TurnPhase.BUILD_PROMPT
        messages, tool_defs = await self._build_turn_prompt(
            user_message, user_name, content, stt_emotion, stt_events, is_task_result,
            user_message_id=message_id)

        # 记录 prompt + token turn start
        if self._prompt_recorder is not None:
            self._prompt_recorder.record(
                messages=list(messages), tools=tool_defs, round_num=0, model=self._llm.model)
        if self._token_tracker is not None:
            self._token_tracker.start_turn()

        await self._output.typing(True)

        # 批次 2：生成开始（v2 generation.started；v1 无新增）
        if self._generation_ctx is not None:
            await self._output.turn_started(self._generation_ctx)

        # 2. ReAct 循环
        empty_retried = False
        try:
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                self._phase = TurnPhase.STREAMING
                acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think, \
                    blocks, public_text = \
                    await self._stream_llm_response(messages, tool_defs, round_num)

                # 空 LLM 响应守卫：无文本、无工具调用 → 注入 nudge 重试一次；
                # 重试后仍空 → 兜底回复（同时注入 all_sentences，保证 session 归档
                # 与前端显示的文本一致——build_parts([]) 会产出空 parts）。
                if not raw_tool_calls and not public_text.strip():
                    if not empty_retried:
                        empty_retried = True
                        logger.warning(
                            "LLM 返回空响应（无文本、无工具调用），注入 nudge 重试一次")
                        messages.append({"role": "system", "content": _EMPTY_RESPONSE_NUDGE})
                        continue
                    # 连续空响应计数器——超阈值升 error，防系统性 bug 持续发
                    # 「走神」回复而不升级告警。
                    self._empty_response_count += 1
                    _log = (logger.error
                            if self._empty_response_count >= _EMPTY_RESPONSE_THRESHOLD
                            else logger.warning)
                    _log(
                        "LLM 重试后仍返回空响应（连续 %d 轮），发送兜底回复",
                        self._empty_response_count,
                    )
                    public_text = _EMPTY_RESPONSE_FALLBACK
                    acc_text = _EMPTY_RESPONSE_FALLBACK
                    all_sentences = [_EMPTY_RESPONSE_FALLBACK]
                else:
                    # 正常响应（有文本或工具调用）——重置空响应计数
                    self._empty_response_count = 0

                if raw_tool_calls and round_num < self.MAX_TOOL_ROUNDS:
                    self._phase = TurnPhase.EXECUTING_TOOLS
                    await self._execute_tool_round(
                        messages, acc_text, raw_tool_calls, _on_tool_calls)
                    # 显式模式切换信号：帮助 LLM 从"工具执行模式"切回"对话模式"
                    messages.append({"role": "system", "content": "[TOOL_EXECUTED]"})
                    continue

                self._phase = TurnPhase.FINALIZING
                await self._finalize_turn(
                    acc_text, all_sentences, iv_think, speaking_started, user_message,
                    round_num, blocks, public_text)
                return

        except (Exception, asyncio.CancelledError):
            self._executing_tools.clear()
            self._tts_mgr.cancel_all()
            await self._output.typing(False)
            raise

    # ---- Post-response ----

    async def _after_response(self, user_message: str, assistant_response: str) -> None:
        """Post-response hooks: consolidate memory, character reflection, save session.

        以 fire-and-forget 方式由 _finalize_turn 调用。
        执行顺序：Consolidator 提取记忆 → CharacterReflector 缓冲 → MemoryReflector 去重 → 保存 session。

        Args:
            user_message: 用户原始输入
            assistant_response: 剥离标签后的公开回复文本
        """
        # shutdown 门禁——stop() 开始后（cognitive task 已 cancel、LLM/session repo 将关闭），
        # 本轮 turn 收尾新创建的 after_response task 立即退出，避免与已关闭资源并发。
        # 否则最后轮记忆沉淀丢失、session 降级写 JSONL。
        if self._shutting_down:
            logger.debug("after_response 跳过——agent 正在关闭")
            return

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
        if self._character_engine:
            try:
                self._character_engine.buffer_message(user_message, assistant_response)
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
        # Save session asynchronously (fire-and-forget)
        try:
            await self._session.save()
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

    # ---- StreamProcessor event dispatch ----

    async def _dispatch_stream_event(
        self, event: StreamEvent, *,
        speaking_started: bool, all_sentences: list[str],
        iv_think_buf_holder: list[str],
    ) -> tuple[bool, list[str]]:
        """分发 StreamEvent → 副作用。

        这是 StreamProcessor（纯状态机）和 CognitiveLoop（副作用世界）之间的桥。
        按事件类型分发：mood → CharacterEngine, inner_voice → UI command,
        ttft → clock span, sentence → TTS/UI, text_delta → 纯文本模式输出。

        Args:
            event: StreamProcessor 产出的解析事件
            speaking_started: 当前轮是否已开始语音/文本输出
            all_sentences: 已累积的分句列表（in-place 追加）
            iv_think_buf_holder: 累积列表，逐条 append inner_voice 文本（可变容器绕过 Python 闭包限制）

        Returns:
            (speaking_started, all_sentences): 更新后的状态
        """
        if event.type == StreamEventType.MOOD:
            if self._character_engine:
                await _apply_mood(event.text, self._character_engine, self._tts_mgr,
                                  self._output)
            self._mood_applied = True

        elif event.type == StreamEventType.INNER_VOICE:
            if event.text:
                iv_think_buf_holder.append(event.text)
                try:
                    if self._generation_ctx is not None:
                        # 统一发射：v1 command inner_voice；v2 作为 inner_voice 段
                        await self._output.inner_voice(event.text, self._generation_ctx)
                    else:
                        # 防御（无 ctx，直接调用/旧路径）：v1 command 直发
                        await self._output.command(
                            "inner_voice", params={"content": event.text})
                except Exception:
                    logger.debug("inner_voice broadcast failed", exc_info=True)

        elif event.type == StreamEventType.TTFT:
            ttft_ms = int((time.perf_counter() - self._ttft_start) * 1000)
            if self._clock is not None:
                self._clock._spans["ttft"] = ttft_ms
            logger.debug("TTFT: %d ms", ttft_ms)

        elif event.type == StreamEventType.SENTENCE:
            sentence = event.text
            if not sentence:
                return speaking_started, all_sentences
            all_sentences.append(sentence)
            logger.debug("分句 (%d 字符)", len(sentence))
            if not speaking_started:
                await self.phases.enter_speaking()
                speaking_started = True
            tts_active = self._tts_mgr.active and self._tts_mgr.enabled
            if tts_active:
                self._tts_mgr.stream_sentence(sentence)
            ctx = self._generation_ctx
            if ctx is not None:
                if ctx.current_segment_id is not None:
                    # 活动段（由本句 deltas 打开）→ 完成本句段
                    await self._output.segment_completed(sentence, ctx, tts_active=tts_active)
                    ctx.close_segment()
                else:
                    # 无活动段（deltas 合并到前段 / 本句无独立 delta 流）→ 为本句新开段再完成，
                    # 保证每句独立 segmentId（PRD BE-022）
                    ctx.open_segment("public_reply", "text")
                    await self._output.segment_started(ctx)
                    await self._output.segment_completed(sentence, ctx, tts_active=tts_active)
                    ctx.close_segment()
            elif not tts_active:
                # text-only：TEXT_DELTA 已广播增量，此处只发句子边界信号结束气泡，避免整句重复显示
                await self._output.send("")

        elif event.type == StreamEventType.TEXT_DELTA:
            tts_active = self._tts_mgr.active and self._tts_mgr.enabled
            ctx = self._generation_ctx
            if event.text:
                if ctx is not None:
                    # 打开 public_reply 段（首个 delta 时）——TTS 模式下也开段（v2 segment.completed 恒发）
                    if ctx.current_segment_id is None:
                        ctx.open_segment("public_reply", "text")
                        await self._output.segment_started(ctx)
                    # loop 保留 TTS 门控：TTS 活跃时不发 delta（v1 现状；v2 段文本由 segment.completed 承载）
                    if not tts_active:
                        await self._output.text_delta(event.text, ctx)
                elif not tts_active:
                    await self._output._broadcast(
                        "on_text_delta", event.text,
                        capability=ChannelCapability.TEXT_DELTA,
                    )

        return speaking_started, all_sentences


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

    async def _handle_interrupt(self) -> None:
        """Clean up after interruption — cancel TTS tasks, return to IDLE."""
        if self.status.is_completely_idle:
            return
        # 批次 2：活动 generation 且未被错误路径覆盖 → v2 发 generation.failed{CANCELLED}
        # （终态唯一：错误路径已发 failed 则不重发；v1 不发，保持现状打断无 ❌）
        if self._generation_ctx is not None and not self._turn_failed_emitted:
            try:
                await self._output.turn_cancelled(self._generation_ctx)
                self._turn_failed_emitted = True
            except Exception:
                logger.debug("turn_cancelled 发射失败", exc_info=True)
        self._executing_tools.clear()
        self._tts_mgr.cancel_all()
        await self._output.typing(False)
        await self.phases.handle_interrupt()
        logger.debug("Interrupt complete — agent is IDLE")
