"""CognitiveLoop — the core ReAct scheduler.

Orchestrates THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE.
Uses structured streaming for sub-100ms TTFT and simultaneous tool-call detection.

Sub-modules:
  text/block_filter.py         — streaming block tag filter + post-hoc block parsing
  adapter/tts_adapter.py       — TTS stream manager + emoji queuing
  adapter/tool_call_parser.py  — merge/normalize streaming tool_call deltas
  text/text_utils.py           — content text extraction + MessageObj
"""

import asyncio
import datetime
import logging
import time
from dataclasses import asdict
from enum import Enum
from typing import Any, Callable, TYPE_CHECKING

from ..utils import new_message_id

from .timing import TimingClock

from Module.output import OutputManager, ChannelCapability
from Module.tts import TTSBackend

from ..agent_fsm import AgentFSM, AgentState
from ..event_bus import EventBus, EventPriority, EventType
from ..utils import set_request_id, get_request_id, log_conversation
from Module.tool.protocol import ToolResult
from ..prompt_assembler import PromptAssembler
from ..memory.session import SessionMemory
from ..text.sentence_buffer import SentenceBuffer
from .stream_processor import StreamProcessor, StreamEvent, StreamEventType
from ..adapter.llm_adapter import LLMAdapter
from ..memory.consolidator import MemoryConsolidator
from Module.llm.registry import LLMFactory

if TYPE_CHECKING:
    from Module.task_system.delegation_preparer import DelegationPreparer

from ..text.block_filter import BlockFilter, build_parts_with_blocks
from ..adapter.tts_adapter import TTSStreamManager
from ..adapter.tool_call_parser import merge_stream_tool_calls, normalize_tool_calls
from ..text.text_utils import content_text_from_content, MessageObj
from core.schema.message import UserMessage, AssistantMessage, ToolResultMessage
from core.schema.token import TokenUsageRecord, TokenUsageSummary
from core.schema.tool import ToolCall, ToolCallId

from core.schema.part import TextPart
from core.schema.adapters.message_adapter import parts_from_dicts
from core.schema._utils import now_iso as _now_iso
from core.schema.event import StateChangeData

logger = logging.getLogger(__name__)


from .tools import CognitiveTools


class TurnPhase(str, Enum):
    """CognitiveLoop 单轮处理阶段 — 内部追踪，不暴露给 FSM 外部消费者。"""
    BUILD_PROMPT = "build_prompt"
    STREAMING = "streaming"
    EXECUTING_TOOLS = "executing_tools"
    FINALIZING = "finalizing"


def _apply_mood(raw: str, engine, tts_mgr) -> None:
    """解析 <mood> 标签内容，更新 CharacterState + push expression + TTS emotion."""
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
    engine._push_expression(mood_text, intensity)
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
        tool_executor=None,  # ToolExecutor Protocol
        character_engine=None,  # CharacterEngine | None
        consolidator: MemoryConsolidator | None = None,
        tts: TTSBackend | None = None,
        token_tracker=None,  # TokenTracker | None
        prompt_recorder=None,  # PromptRecorder | None
    ):
        self._llm = llm
        self._fsm = fsm
        self._bus = event_bus
        self._output = output
        self._prompt = prompt
        self._session = session
        self._tool_executor = tool_executor
        self._req_counter: int = 0
        self._character_engine = character_engine
        self._consolidator = consolidator
        self._tool_adapter: Any = None     # 由 configure() 注入
        self._memory_reflector: Any = None  # 由 configure() 注入
        self._tts = tts  # 保留供 agent.py stop() 读取
        self._token_tracker = token_tracker
        self._prompt_recorder = prompt_recorder
        self._response_recorder: Any = None  # 由 configure() 注入
        self._sentence_buf: SentenceBuffer | None = None
        self._block_filter: BlockFilter | None = None
        self._ttft_start: float = 0.0
        self._mood_applied: bool = False
        self._process_lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None  # 保留供 agent.py stop() 读取
        # TTS streaming manager — extracted sub-system
        self._tts_mgr = TTSStreamManager(
            tts, output,
            prompt._character.name if prompt and hasattr(prompt, '_character') else "unknown",
        )
        self._executing_tools: list[str] = []  # 当前正在执行中的工具名 (供 debug 面板)
        self._timing_history: list[dict] = []  # 最近 100 轮请求的计时数据 (供 debug 面板)
        self._task_system: Any = None          # 由 configure() 注入
        self._delegation_preparer: Any = None  # 由 configure() 注入
        self._on_background_task: Callable | None = None  # Agent 注入的回调
        self._user_profile: Any = None         # 由 configure() 注入
        self._phase: TurnPhase | None = None   # 当前轮次处理阶段（供 debug 面板）

    # ---- Public API ----

    def cancel_current(self) -> bool:
        """取消当前认知任务（如有）。返回 True 表示确实取消了任务。"""
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            self._current_task = None
            return True
        self._current_task = None
        return False

    def configure(self, **kwargs) -> None:
        """一次性注入所有延迟依赖（替代 10 个 setter 方法）。

        由 agent.py 在 __init__ / start() 中调用。有副作用的属性（如 tts）
        在此方法内联处理。
        """
        for name, value in kwargs.items():
            setattr(self, f"_{name}", value)
            if name == "tts":
                self._tts_mgr.set_backend(value)

    # ---- Main entry ----

    async def process(self, user_message: str = "", user_name: str = "妖梦",
                       stt_emotion: str = "", stt_events: list[str] | None = None,
                       _on_tool_calls=None,
                       content: list[dict] | None = None,
                       is_task_result: bool = False,
                       task_result_context: dict | None = None,
                       ) -> None:
        """处理用户消息。

        user_message: 纯文本（STT/键盘输入）— 兼容旧调用
        content: 多模态内容数组（ChatMessageV2 content）— 新接口
        """
        async with self._process_lock:
            if not self._fsm.can_transition(AgentState.THINKING):
                logger.warning("Cannot transition to THINKING from %s", self._fsm.state)
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

            self._fsm.transition(AgentState.THINKING)
            self._bus.post(StateChangeData(state="thinking", request_id=rid),
                          priority=EventPriority.STATE_CHANGE)

            self._current_task = asyncio.current_task()
            try:
                await self._think_loop(user_message, user_name,
                                       stt_emotion=stt_emotion, stt_events=stt_events,
                                       _on_tool_calls=_on_tool_calls, content=content,
                                       is_task_result=is_task_result,
                                       task_result_context=task_result_context)
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
                logger.exception("<<< LLM 调用异常，回退到 IDLE")
                # 通知前端错误信息
                err_msg = str(e)[:200]
                try:
                    await self._output._broadcast(
                        "on_text_delta",
                        f"\n❌ LLM 调用失败: {err_msg}",
                        capability=ChannelCapability.TEXT_DELTA,
                    )
                    await self._output.send("")
                except Exception:
                    pass
                await self._handle_interrupt()
            finally:
                self._clock = None  # type: ignore[assignment]

    # ---- Core loop: prompt construction ----

    async def _build_turn_prompt(
        self, user_message: str, user_name: str,
        content: list[dict] | None,
        stt_emotion: str, stt_events: list[str] | None,
        is_task_result: bool,
    ) -> tuple[list[dict], list[dict]]:
        """构建本轮 LLM 调用的 messages + tool definitions。"""
        assert self._clock is not None
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
                message_id=new_message_id(),
                timestamp=_now_iso(),
            ))
        tool_defs = self._tool_executor.definitions() if self._tool_executor else None
        # 多模态 LLM 可直接看图，排除 describe_image 避免 LLM 只调用一次
        if tool_defs and supports_mm:
            tool_defs = [t for t in tool_defs if t.get("function", {}).get("name") != "describe_image"]
        if tool_defs is None:
            tool_defs = []
        # 附加 submit_task（如果 TaskSystem 和 DelegationPreparer 都已注入）
        if self._task_system is not None and self._delegation_preparer is not None:
            tool_defs = list(tool_defs) + [self._submit_task_definition()]
        logger.debug("提示词构建: %d ms, %d 条消息, %d 个工具", t.elapsed_ms, len(messages), len(tool_defs))
        return messages, tool_defs

    # ---- Core loop: LLM streaming ----

    async def _stream_llm_response(
        self, messages: list[dict], tool_defs: list[dict], round_num: int,
    ) -> tuple[str, list[dict], bool, list[str], str]:
        """流式调用 LLM，分发 StreamProcessor 事件。

        Returns: (acc_text, raw_tool_calls, speaking_started, all_sentences, inner_voice)
        """
        acc_text = ""
        raw_tool_calls: list[dict] = []
        speaking_started = False
        all_sentences: list[str] = []
        iv_think_buf_holder: list[str] = [""]
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

        return acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think_buf_holder[0]

    # ---- Core loop: tool execution ----

    async def _execute_tool_round(
        self, messages: list[dict], acc_text: str,
        raw_tool_calls: list[dict], _on_tool_calls,
    ) -> None:
        """执行工具调用轮次：submit_task 拦截 → dispatch → 结果注入。

        副作用：修改 messages（in-place）、写 session、推 output 事件。
        """
        # 切断气泡 — TTS tasks 独立运行，不 cancel
        await self._output.send("")

        await self._output.typing(False)
        self._fsm.transition(AgentState.EXECUTING)
        self._bus.post(StateChangeData(state="executing", request_id=get_request_id()),
                       priority=EventPriority.STATE_CHANGE)

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

        # submit_task 工具调用——拦截处理
        if submit_tcs:
            for i, tc in enumerate(submit_tcs):
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
                    mailbox = self._task_system.get_task_mailbox(result.async_task_id)
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
            self._executing_tools.append(func_name)
            await self._output.action("tool_executing", params={
                "tool": func_name,
                "call_id": call_id,
                "args": {k: str(v) for k, v in args.items()},
            })

        assert self._clock is not None
        with self._clock.span("tools"):
            if _on_tool_calls is not None:
                results = await _on_tool_calls(dispatch_tcs)
            else:
                results = await self._execute_tools_directly(dispatch_tcs)

        for item in results:
            if isinstance(item, BaseException):
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

        self._fsm.transition(AgentState.THINKING)
        self._bus.post(StateChangeData(state="thinking", request_id=get_request_id()),
                       priority=EventPriority.STATE_CHANGE)
        await self._output.typing(True)

    # ---- Core loop: finalize ----

    async def _finalize_turn(
        self, acc_text: str, all_sentences: list[str],
        iv_think_buf: str, speaking_started: bool,
        user_message: str, round_num: int,
    ) -> None:
        """收尾：标签剥离 → 归档 session → 推送结构化块 → 触发 after_response。"""
        await self._tts_mgr.flush_emojis()

        # Fallback: LLM 未输出 <mood> → 关键词检测
        if self._character_engine and not self._mood_applied:
            logger.debug("心情: <mood> 未检测到，使用关键词 fallback")
            self._character_engine.analyze_user(user_message)
            self._tts_mgr.set_emotion(self._character_engine.state.mood)

        # 从 acc_text 中剥离 <mood> 和 <inner_voice> 标签
        public_text = acc_text
        mood_start = public_text.find("<mood>")
        if mood_start >= 0:
            mood_close = public_text.find("</mood>", mood_start)
            if mood_close >= 0:
                public_text = (public_text[:mood_start] + public_text[mood_close + len("</mood>"):]).strip()
        iv_start = public_text.find("<inner_voice>")
        if iv_start >= 0:
            iv_close = public_text.find("</inner_voice>", iv_start)
            if iv_close >= 0:
                public_text = (public_text[:iv_start] + public_text[iv_close + len("</inner_voice>"):]).strip()

        if public_text.strip():
            if self._character_engine:
                self._character_engine.analyze_assistant(public_text)
            if not speaking_started:
                await self._output.typing(False)
                msg = MessageObj(content=public_text)
                await self._output.send(msg)
        if public_text.strip():
            assistant_parts, content_type = build_parts_with_blocks(public_text, all_sentences)
            self._session.add_message(AssistantMessage(
                content=assistant_parts,
                message_id=new_message_id(),
                inner_voice=iv_think_buf.strip() or None,
                block_index=content_type,
                token_usage=TokenUsageSummary(
                    rounds=[TokenUsageRecord(
                        model=r.model,
                        prompt_tokens=r.prompt_tokens,
                        completion_tokens=r.completion_tokens,
                        total_tokens=r.total_tokens,
                        round_num=r.round_num,
                        timestamp=r.timestamp,
                    ) for r in self._token_tracker.current_turn_usages],
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
            asyncio.create_task(self._after_response(user_message, public_text),
                               name="after-response")

            # 推送结构化块到前端
            for idx in content_type.get("markdown", []):
                if idx < len(assistant_parts):
                    await self._output.command("block", params={
                        "block_type": "markdown",
                        "content": assistant_parts[idx].content,
                    })
            for idx in content_type.get("code", []):
                if idx < len(assistant_parts):
                    await self._output.command("block", params={
                        "block_type": "code",
                        "content": assistant_parts[idx].content,
                        "language": getattr(assistant_parts[idx], "language", ""),
                    })

        await self._transition_idle()

    # ---- Core loop: main orchestrator ----

    async def _think_loop(self, user_message: str, user_name: str,
                          stt_emotion: str = "", stt_events: list[str] | None = None,
                          _on_tool_calls=None,
                          content: list[dict] | None = None,
                          is_task_result: bool = False,
                          task_result_context: dict | None = None,
                          ) -> None:
        """ReAct 主循环：build → [stream → (execute | finalize)] * MAX_ROUNDS。"""
        # 前置：STT 情绪分析 + 渐进式唤醒
        if self._character_engine:
            if stt_emotion and stt_emotion != "NEUTRAL":
                self._character_engine.analyze_user_stt(stt_emotion, stt_events)
        if is_task_result and task_result_context:
            user_message = self._build_wake_up_message(task_result_context)

        # 1. 构建 prompt
        self._phase = TurnPhase.BUILD_PROMPT
        messages, tool_defs = await self._build_turn_prompt(
            user_message, user_name, content, stt_emotion, stt_events, is_task_result)

        # 记录 prompt + token turn start
        if self._prompt_recorder is not None:
            self._prompt_recorder.record(
                messages=list(messages), tools=tool_defs, round_num=0, model=self._llm.model)
        if self._token_tracker is not None:
            self._token_tracker.start_turn()

        await self._output.typing(True)

        # 2. ReAct 循环
        try:
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                self._phase = TurnPhase.STREAMING
                acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think = \
                    await self._stream_llm_response(messages, tool_defs, round_num)

                if raw_tool_calls and round_num < self.MAX_TOOL_ROUNDS:
                    self._phase = TurnPhase.EXECUTING_TOOLS
                    await self._execute_tool_round(
                        messages, acc_text, raw_tool_calls, _on_tool_calls)
                    continue

                self._phase = TurnPhase.FINALIZING
                await self._finalize_turn(
                    acc_text, all_sentences, iv_think, speaking_started, user_message, round_num)
                return

        except (Exception, asyncio.CancelledError):
            self._executing_tools.clear()
            self._tts_mgr.cancel_all()
            await self._output.typing(False)
            raise

    # ---- Post-response ----

    async def _after_response(self, user_message: str, assistant_response: str) -> None:
        """Post-response hooks: consolidate memory, character reflection, save session."""
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
                logger.debug("记忆沉淀失败", exc_info=True)
        if self._character_engine:
            try:
                self._character_engine.buffer_message(user_message, assistant_response)
            except Exception:
                logger.debug("性格反思缓冲失败", exc_info=True)
        # 更新 MemoryReflector 计数器
        if self._memory_reflector:
            self._memory_reflector.increment_round()
            if items:
                self._memory_reflector.increment_new_memories(len(items))
            # 检查是否需要触发去重（非阻塞，快速判断）
            try:
                await self._memory_reflector.check_and_reflect()
            except Exception:
                logger.debug("MemoryReflector check failed", exc_info=True)
        # Save session asynchronously (fire-and-forget)
        try:
            await self._session.save()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("会话保存失败", exc_info=True)


    # ---- StreamProcessor event dispatch ----

    async def _dispatch_stream_event(
        self, event: StreamEvent, *,
        speaking_started: bool, all_sentences: list[str],
        iv_think_buf_holder: list[str],
    ) -> tuple[bool, list[str]]:
        """分发 StreamEvent → 副作用。

        这是 StreamProcessor（纯状态机）和 CognitiveLoop（副作用世界）之间的桥。
        返回更新后的 (speaking_started, all_sentences)。
        """
        if event.type == StreamEventType.MOOD:
            if self._character_engine:
                _apply_mood(event.text, self._character_engine, self._tts_mgr)
            self._mood_applied = True

        elif event.type == StreamEventType.INNER_VOICE:
            if event.text:
                iv_think_buf_holder[0] = event.text
                try:
                    await self._output.command(
                        "inner_voice",
                        params={"content": event.text},
                    )
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
                await self._output.typing(False)
                self._fsm.transition(AgentState.SPEAKING)
                self._bus.post(
                    StateChangeData(state="speaking", request_id=get_request_id()),
                    priority=EventPriority.STATE_CHANGE,
                )
                speaking_started = True
            if self._tts_mgr.active:
                self._tts_mgr.stream_sentence(sentence)
            else:
                await self._output._broadcast(
                    "on_text_delta", sentence,
                    capability=ChannelCapability.TEXT_DELTA,
                )
                await self._output.send("")

        elif event.type == StreamEventType.TEXT_DELTA:
            if not self._tts_mgr.active and event.text:
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
        """等待所有 TTS 任务完成，然后转换到 IDLE。"""
        await self._tts_mgr.wait_all()

        # 如果有后台任务活跃 → 进入 IDLE_BACKGROUND_TASK 而非 IDLE
        if self._on_background_task is not None and self._task_system is not None:
            active = await self._task_system.list_active_tasks()
            if active:
                if self._fsm.state == AgentState.SPEAKING:
                    self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
                else:
                    # 防御性: 确保经过 SPEAKING
                    if self._fsm.can_transition(AgentState.SPEAKING):
                        self._fsm.transition(AgentState.SPEAKING)
                    self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
                self._bus.post(StateChangeData(state="idle_background_task", request_id=get_request_id()),
                               priority=EventPriority.STATE_CHANGE)
                return

        # 原有 IDLE 转换
        logger.info("→ IDLE")
        if self._fsm.state == AgentState.THINKING:
            # 仅 tool_calls 无文本时 FSM 仍在 THINKING，需经过 SPEAKING 才能到 IDLE
            self._fsm.transition(AgentState.SPEAKING)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(StateChangeData(state="idle", request_id=get_request_id()), priority=EventPriority.STATE_CHANGE)

    async def _handle_interrupt(self) -> None:
        """Clean up after interruption — cancel TTS tasks, return to IDLE."""
        if self._fsm.state == AgentState.IDLE:
            return  # 重启后旧 task 的 CancelledError 可能触发重复清理
        if self._sentence_buf is not None:
            self._sentence_buf.reset()
        if self._block_filter is not None:
            self._block_filter.reset()
        self._executing_tools.clear()
        self._tts_mgr.cancel_all()
        await self._output.typing(False)
        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(StateChangeData(state="interrupted", request_id=get_request_id()), priority=EventPriority.STATE_CHANGE)
        await asyncio.sleep(0.05)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(StateChangeData(state="idle", request_id=get_request_id()), priority=EventPriority.STATE_CHANGE)
        logger.debug("Interrupt complete — agent is IDLE")
