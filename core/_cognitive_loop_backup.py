"""CognitiveLoop — the core ReAct scheduler.

Orchestrates THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE.
Uses structured streaming for sub-100ms TTFT and simultaneous tool-call detection.

Sub-modules extracted for focus:
  block_filter.py   — streaming block tag filter + post-hoc block parsing
  tts_stream.py     — TTS stream manager + emoji queuing
  tool_call_parser.py — merge/normalize streaming tool_call deltas
  text_utils.py     — content text extraction + MessageObj
"""

import asyncio
import datetime
import logging
import time
from dataclasses import asdict
from typing import Callable, TYPE_CHECKING

from .message_id import new_message_id

from .timing import TimingClock

from Module.output import OutputManager, ChannelCapability
from Module.tts import TTSBackend

from .agent_fsm import AgentFSM, AgentState
from .event_bus import EventBus, EventPriority, EventType
from .log_setup import set_request_id, get_request_id, log_conversation
from Module.tool import ToolContext
from Module.tool.protocol import ToolResult
from .prompt_assembler import PromptAssembler
from .memory.session import SessionMemory
from .sentence_buffer import SentenceBuffer
from .llm_adapter import LLMAdapter
from .consolidator import MemoryConsolidator
from Module.llm.registry import LLMFactory

if TYPE_CHECKING:
    from Module.task_system.delegation_preparer import DelegationPreparer

from .block_filter import BlockFilter, build_parts_with_blocks
from .tts_stream import is_emoji_only, TTSStreamManager
from .tool_call_parser import merge_stream_tool_calls, normalize_tool_calls
from .text_utils import content_text_from_content, MessageObj
from core.schema.message import UserMessage, AssistantMessage, ToolResultMessage
from core.schema.token import TokenUsageRecord, TokenUsageSummary
from core.schema.tool import ToolCall, ToolCallId
from core.schema.mood import _MOOD_TO_TTS_EMOTION, Mood
from core.schema.part import TextPart
from core.schema.adapters.message_adapter import parts_from_dicts
from core.schema._utils import now_iso as _now_iso
from core.schema.event import StateChangeData

logger = logging.getLogger(__name__)


def _default_user_profile():
    """返回默认 UserProfile（v1: 空实例，后续由 CognitiveLoop 填充偏好/语言等）。"""
    from Module.task_system.schema import UserProfile
    return UserProfile()


class CognitiveLoop:
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
        ctx: ToolContext | None = None,
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
        self._memory_reflector = None
        self._tts = tts  # 保留供 agent.py stop() 读取
        self._ctx = ctx
        self._token_tracker = token_tracker
        self._prompt_recorder = prompt_recorder
        self._sentence_buf: SentenceBuffer | None = None
        self._block_filter: BlockFilter | None = None
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
        self._task_system = None            # TaskSystem | None（agent.py 在 start() 中注入）
        self._delegation_preparer = None    # DelegationPreparer | None（agent.py 在 start() 中注入）
        self._on_background_task: Callable | None = None  # Agent 注入的回调
        self._user_profile = None           # UserProfile | None

    # ---- Public API ----

    def cancel_current(self) -> bool:
        """取消当前认知任务（如有）。返回 True 表示确实取消了任务。"""
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            self._current_task = None
            return True
        self._current_task = None
        return False

    def set_tool_context(self, ctx) -> None:
        """设置当前 turn 的工具上下文。"""
        self._ctx = ctx

    def set_tts(self, tts: TTSBackend | None) -> None:
        """设置/热切换 TTS 后端。"""
        self._tts = tts
        self._tts_mgr.set_backend(tts)

    def set_token_tracker(self, tracker) -> None:
        """设置 token 追踪器（agent.py 在 start() 中延迟注入）。"""
        self._token_tracker = tracker

    def set_prompt_recorder(self, recorder) -> None:
        """设置 prompt 记录器（agent.py 在 start() 中延迟注入）。"""
        self._prompt_recorder = recorder

    def set_response_recorder(self, recorder) -> None:
        """设置 response 记录器（agent.py 在 start() 中延迟注入）。"""
        self._response_recorder = recorder

    def set_task_system(self, ts) -> None:
        """设置 TaskSystem 引用（agent.py 在 start() 中注入）。"""
        self._task_system = ts

    def set_delegation_preparer(self, dp) -> None:
        """设置 DelegationPreparer 引用（agent.py 在 start() 中注入）。"""
        self._delegation_preparer = dp

    def set_on_background_task(self, callback: Callable | None) -> None:
        """设置后台任务注册回调（agent.py 注入）。"""
        self._on_background_task = callback

    def set_user_profile(self, profile) -> None:
        """设置用户画像引用。"""
        self._user_profile = profile

    def set_memory_reflector(self, reflector) -> None:
        """设置 MemoryReflector 引用（agent.py 在 __init__ 中注入）。"""
        self._memory_reflector = reflector

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
            if self._ctx is not None:
                self._ctx.trace_id = new_message_id()

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

            self._sentence_buf = SentenceBuffer(min_chunk_length=5, max_buffer_length=2000)
            self._block_filter = BlockFilter()
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

    # ---- Core loop ----

    async def _think_loop(self, user_message: str, user_name: str,
                          stt_emotion: str = "", stt_events: list[str] | None = None,
                          _on_tool_calls=None,
                          content: list[dict] | None = None,
                          is_task_result: bool = False,
                          task_result_context: dict | None = None,
                          ) -> None:
        # process() 在调用 _think_loop 前已初始化这两个 buffer
        assert self._sentence_buf is not None
        sentence_buf = self._sentence_buf
        assert self._block_filter is not None

        if self._character_engine:
            if stt_emotion and stt_emotion != "NEUTRAL":
                self._character_engine.analyze_user_stt(stt_emotion, stt_events)
            else:
                self._character_engine.analyze_user(user_message)
            # 将当前 mood 映射为 TTS emotion（MiniMax 等后端支持情绪控制）
            mood = self._character_engine.state.mood
            self._tts_mgr.set_emotion(_MOOD_TO_TTS_EMOTION.get(mood))

        # 渐进式唤醒——后台任务完成后的通知
        if is_task_result and task_result_context:
            user_message = self._build_wake_up_message(task_result_context)

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
        # （只处理 image_index=0 而忽略其余图片）
        if tool_defs and supports_mm:
            tool_defs = [t for t in tool_defs if t.get("function", {}).get("name") != "describe_image"]
        # 合并 submit_task function definition（让 LLM 可以调用）
        if tool_defs is None:
            tool_defs = []
        # 附加 submit_task（如果 TaskSystem 和 DelegationPreparer 都已注入）
        if self._task_system is not None and self._delegation_preparer is not None:
            tool_defs = list(tool_defs) + [self._submit_task_definition()]
        logger.debug("提示词构建: %d ms, %d 条消息, %d 个工具", t.elapsed_ms, len(messages), len(tool_defs or []))

        # 捕获第一轮的完整 prompt（后续轮次在工具循环内捕获）
        if self._prompt_recorder is not None:
            self._prompt_recorder.record(
                messages=list(messages),  # 浅拷贝，防止后续工具循环修改
                tools=tool_defs,
                round_num=0,
                model=self._llm.model,
            )

        # 新一轮对话开始，清空 turn 缓存
        if self._token_tracker is not None:
            self._token_tracker.start_turn()

        await self._output.typing(True)

        try:
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                acc_text = ""
                raw_tool_calls: list[dict] = []
                speaking_started = False
                all_sentences: list[str] = []
                block_filter: BlockFilter = self._block_filter
                block_filter.reset()  # 防御性：_think_loop 可能因 tool round 被多次调用
                ttft_done = False
                t_think_start = time.perf_counter()

                # 工具循环的后续轮次也捕获 prompt
                if round_num > 0 and self._prompt_recorder is not None:
                    self._prompt_recorder.record(
                        messages=list(messages),
                        tools=tool_defs,
                        round_num=round_num,
                        model=self._llm.model,
                    )

                # inner_voice 标签检测状态机
                TAG_OPEN = "<inner_voice>"
                TAG_CLOSE = "</inner_voice>"
                iv_state = "PRE"      # PRE / THINK / REPLY
                iv_tail: str = ""          # 跨 chunk 检测缓冲
                iv_think_buf = ""     # 已确认的 inner_voice 内容

                # 句子处理 helper — PRE/THINK/REPLY 三状态共用
                async def _handle_sentence(clean_text: str) -> str | None:
                    nonlocal speaking_started
                    sentence = sentence_buf.feed(clean_text) if clean_text else None
                    if not sentence:
                        return None
                    all_sentences.append(sentence)
                    logger.debug("分句 (%d 字符)", len(sentence))
                    if not speaking_started:
                        await self._output.typing(False)
                        self._fsm.transition(AgentState.SPEAKING)
                        self._bus.post(StateChangeData(state="speaking", request_id=get_request_id()),
                                       priority=EventPriority.STATE_CHANGE)
                        speaking_started = True
                    if self._tts_mgr.active:
                        self._tts_mgr.stream_sentence(sentence)
                    else:
                        await self._output._broadcast("on_text_delta", sentence,
                            capability=ChannelCapability.TEXT_DELTA)
                        await self._output.send("")
                    return sentence

                stream = self._llm.stream_think(messages, tools=tool_defs,
                    extra_body={"thinking": {"type": "disabled"}})
                try:
                    async for chunk in stream:
                        delta: str = chunk.get("content", "")
                        tc_deltas = chunk.get("tool_calls")

                        if delta:
                            acc_text += delta

                            # --- inner_voice 标签检测 ---
                            if iv_state == "PRE":
                                combined = iv_tail + delta
                                idx = combined.find(TAG_OPEN)
                                if idx >= 0:
                                    before = combined[:idx].lstrip()
                                    if before:
                                        clean = block_filter.feed(before)
                                        if not self._tts_mgr.active and clean:
                                            await self._output._broadcast("on_text_delta", clean,
                                                capability=ChannelCapability.TEXT_DELTA)
                                        await _handle_sentence(clean)
                                    iv_state = "THINK"
                                    iv_tail = ""
                                    delta = combined[idx + len(TAG_OPEN):]
                                else:
                                    safe_len = max(0, len(combined) - len(TAG_OPEN) + 1)
                                    if safe_len > 0:
                                        clean = block_filter.feed(combined[:safe_len])
                                        if not self._tts_mgr.active and clean:
                                            await self._output._broadcast("on_text_delta", clean,
                                                capability=ChannelCapability.TEXT_DELTA)
                                        await _handle_sentence(clean)
                                    iv_tail = combined[safe_len:]
                                    continue

                            if iv_state == "THINK":
                                combined = iv_tail + delta
                                idx = combined.find(TAG_CLOSE)
                                if idx >= 0:
                                    iv_think_buf += combined[:idx]
                                    after = combined[idx + len(TAG_CLOSE):].lstrip()
                                    iv_state = "REPLY"
                                    iv_tail = ""
                                    if iv_think_buf.strip():
                                        try:
                                            await self._output.command("inner_voice",
                                                params={"content": iv_think_buf.strip()})
                                        except Exception:
                                            logger.debug("inner_voice broadcast failed", exc_info=True)
                                    delta = after
                                else:
                                    keep_len = min(len(TAG_CLOSE) - 1, len(combined))
                                    if len(combined) > keep_len:
                                        iv_think_buf += combined[:-keep_len]
                                    iv_tail = combined[-keep_len:]
                                    continue

                            if iv_state == "REPLY":
                                pass  # 正常流，继续下面的处理

                            if not delta:
                                continue

                            if not ttft_done:
                                ttft_done = True
                                ttft_ms = int((time.perf_counter() - t_think_start) * 1000)
                                assert self._clock is not None
                                self._clock._spans["ttft"] = ttft_ms
                                logger.debug("TTFT: %d ms", ttft_ms)

                            # 过滤结构化块标记 — 代码和 markdown 不走 TTS
                            clean = block_filter.feed(delta)

                            # TTS 关闭时流式逐字发送；TTS 开启时不发（由 TTSStreamManager 发送完整句）
                            if not self._tts_mgr.active and clean:
                                await self._output._broadcast("on_text_delta", clean,
                                    capability=ChannelCapability.TEXT_DELTA)

                            await _handle_sentence(clean)

                        if tc_deltas:
                            merge_stream_tool_calls(raw_tool_calls, tc_deltas)

                        # Token 消耗: stream 最后可能带 usage
                        if "usage" in chunk:
                            if self._token_tracker is not None:
                                await self._token_tracker.record(
                                    usage=chunk["usage"],
                                    model=self._llm.model,
                                    round_num=round_num,
                                )
                except asyncio.CancelledError:
                    raise

                # Flush state machine tail (流结束时的残留缓冲区)
                if iv_state == "PRE" and iv_tail:
                    # 整个流都没找到 <inner_voice>，全部当 public_reply
                    clean = block_filter.feed(iv_tail)
                    await _handle_sentence(clean)
                elif iv_state == "THINK":
                    # 有 <inner_voice> 但没有 </inner_voice>，剩余归入 think
                    if iv_tail:
                        iv_think_buf += iv_tail
                    # 广播 inner_voice（如果有的话）
                    if iv_think_buf.strip():
                        try:
                            await self._output.command("inner_voice",
                                params={"content": iv_think_buf.strip()})
                        except Exception:
                            logger.debug("inner_voice broadcast failed", exc_info=True)

                # Flush remaining text in sentence buffer + block filter
                trailing = block_filter.flush()
                if trailing:
                    await _handle_sentence(trailing)
                remaining = sentence_buf.flush()
                if remaining:
                    if is_emoji_only(remaining):
                        logger.info("flush emoji: %s", remaining)
                    else:
                        all_sentences.append(remaining)
                        logger.debug("flush 尾句 (%d 字符)", len(remaining))
                        logger.debug("尾句内容: %s", remaining[:60])
                    if not speaking_started:
                        await self._output.typing(False)
                        self._fsm.transition(AgentState.SPEAKING)
                        self._bus.post(StateChangeData(state="speaking", request_id=get_request_id()),
                                       priority=EventPriority.STATE_CHANGE)
                        speaking_started = True
                    if self._tts_mgr.active:
                        self._tts_mgr.stream_sentence(remaining)
                    else:
                        await self._output._broadcast("on_text_delta", remaining,
                            capability=ChannelCapability.TEXT_DELTA)
                        await self._output.send("")

                # If tool calls were detected, execute them and loop
                if raw_tool_calls and round_num < self.MAX_TOOL_ROUNDS:
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
                    # 使用完整的 normalized（含 submit_task）
                    assistant_entry: dict = {"role": "assistant", "content": acc_text or None}
                    if normalized:
                        assistant_entry["tool_calls"] = [tc.model_dump(mode="json") for tc in normalized]
                    messages.append(assistant_entry)

                    # submit_task 工具调用——拦截处理
                    # submit_task 的 tool result 在 assistant_entry 之后追加，
                    # 确保 OpenAI 协议: assistant(tool_calls) → tool(result)
                    if submit_tcs:
                        for i, tc in enumerate(submit_tcs):
                            result = await self._handle_submit_task(tc)
                            # 注入 tool result 到 messages（必须在 assistant 之后）
                            # 使用唯一 ID——避免多个 submit_task 共享 "unknown" 导致协议冲突
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
                            # 注册后台任务（result.success 时有 async_task_id）
                            if result.success and result.async_task_id and self._on_background_task and self._task_system:
                                mailbox = self._task_system.get_task_mailbox(result.async_task_id)
                                if mailbox:
                                    dispatched_now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                                    self._on_background_task(
                                        result.async_task_id, mailbox, intent=(
                                            tc.function.parsed.get("intent", "")
                                        ), dispatched_at=dispatched_now,
                                    )

                        # submit_task 已处理完毕，只 dispatch 其他工具
                        dispatch_tcs = other_tcs
                    else:
                        dispatch_tcs = normalized

                    # 工具调用消息写入 Session（刷新后前端可恢复工具卡片）
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
                            continue  # 工具被 cancel，跳过 — LLM 从缺失结果推断
                        tc, tool_result = item
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result.llm_content,
                        })
                        # Emit tool_done for each completed tool
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
                        # 工具结果写入 Session（刷新后前端可恢复工具卡片内容）
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
                    continue

                # Final answer — TTS tasks streaming in background, don't block
                # 发送还未排出的 emoji（最后一句 TTS 之后的）
                await self._tts_mgr.flush_emojis()

                # 从 acc_text 中剥离 <inner_voice> 标签——数据库/前端/情绪分析用清洗后的文本
                public_text = acc_text
                iv_start = public_text.find("<inner_voice>")
                if iv_start >= 0:
                    iv_close = public_text.find("</inner_voice>", iv_start)
                    if iv_close >= 0:
                        public_text = (public_text[:iv_start] + public_text[iv_close + len("</inner_voice>"):]).strip()

                if public_text.strip():
                    if self._character_engine:
                        self._character_engine.analyze_assistant(public_text)
                    if not speaking_started:
                        # 无标点的极短回复，直接发完整消息
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
                    # 记录原始 LLM 回复（供 debug 页面查看）
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

                    # 推送结构化块到前端（不走 TTS，单独渲染）
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

    # ---- Deprecated: legacy TTS methods (kept for test backward-compat) ----

    def _start_tts_stream(self, sentence: str) -> None:
        """[Deprecated] 代理到 TTSStreamManager.stream_sentence()。

        保留供外部旧代码/测试兼容，新代码应直接使用 self._tts_mgr.stream_sentence()。
        """
        self._tts_mgr.stream_sentence(sentence)
        # 更新 _tts_task 供 agent.py stop() 读取
        if self._tts_mgr._tasks:
            self._tts_task = self._tts_mgr._tasks[-1]

    async def _synthesize_sentence(self, sentence: str) -> None:
        """[Deprecated] Legacy non-streaming TTS — 直接使用 self._tts。

        保留供 test_phase3.py 兼容。不走 TTSStreamManager 以保持测试直接设置
        loop._tts = AsyncMock() 的行为不变。
        """
        if not self._tts:
            return
        try:
            char_name = self._prompt._character.name
            audio = await self._tts.synthesize(sentence, character=char_name)
            await self._output.audio_chunk(audio)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("TTS synthesis failed", exc_info=True)

    # ---- Tool execution ----

    async def _execute_tools_directly(
        self, normalized: list[ToolCall],
    ) -> list:
        """直接执行工具调用 — 向后兼容，不经过 Scheduler Worker Pool。

        每个工具在独立的 asyncio.Task 中并行执行，
        CancelledError 由 asyncio.gather 的 return_exceptions 捕获。
        """
        async def _execute_one(tc: ToolCall):
            func_name = tc.function.name
            t_tool = time.perf_counter()
            args = tc.function.parsed
            logger.info("工具开始: %s(%s)", func_name, {k: str(v)[:60] for k, v in args.items()})
            try:
                assert self._tool_executor is not None
                tr = await self._tool_executor.execute(func_name, ctx=self._ctx, **args)
            except asyncio.CancelledError:
                logger.info("工具取消: %s", func_name)
                tr = ToolResult.fail(name=func_name, error="用户取消")
            except Exception as e:
                logger.warning("工具失败: %s — %s", func_name, e)
                tr = ToolResult.fail(name=func_name, error=str(e))
            t_tool_elapsed = (time.perf_counter() - t_tool) * 1000
            logger.info("工具完成: %s (%s) — %d ms",
                        func_name, "OK" if tr.success else "FAIL", int(t_tool_elapsed))
            return tc, tr

        return await asyncio.gather(
            *[_execute_one(tc) for tc in normalized],
            return_exceptions=True,
        )

    # ---- submit_task 拦截框架 ----

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

    @staticmethod
    def _submit_task_definition() -> dict:
        """submit_task 工具的 function definition。

        不注册为 ToolPlugin——只在 tool_defs 中合并，LLM 可调用，
        但 ToolManager 永远不执行它（CognitiveLoop 拦截处理）。
        """
        return {
            "type": "function",
            "function": {
                "name": "submit_task",
                "description": (
                    "将复杂多步骤任务提交到后台异步执行。"
                    "后台 TaskSystem 有自己的 LLM，会自动规划、搜索、执行所有步骤。"
                    "你不需要在调用前做任何准备工作（如搜索、分析）——直接提交即可。"
                    "适用：涉及多步操作、状态变更、或长时间运行的任务。"
                    "简单单步操作（如单次搜索、单次文件读取）请直接用对应工具。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "完整任务意图描述，包含目标和约束",
                        },
                        "hints": {
                            "type": "string",
                            "description": (
                                "对任务执行有帮助的额外信息，"
                                "如用户情绪、紧急程度、偏好等"
                            ),
                        },
                    },
                    "required": ["intent"],
                },
            },
        }

    async def _handle_submit_task(self, tc: ToolCall) -> ToolResult:
        """处理 submit_task function call。

        校验 intent → DelegationPreparer.prepare() → TaskSystem.submit()。
        返回 ToolResult——LLM 在下一轮看到成功/失败。
        """
        from Module.task_system.schema import SubmitError

        args = tc.function.parsed
        intent = (args.get("intent") or "").strip()

        # 1. 校验
        if not intent:
            return ToolResult.fail(name="submit_task",
                error="intent 不能为空，请修正参数后重试。")
        if len(intent) > 10000:
            return ToolResult.fail(name="submit_task",
                error="intent 过长（超过 10000 字符），请缩短描述后重试。")

        hints = (args.get("hints") or "").strip() or None

        # 2. DelegationPreparer
        if self._delegation_preparer is None:
            return ToolResult.fail(name="submit_task",
                error="DelegationPreparer 未初始化，请告知用户后台任务系统暂不可用。")

        # 将 ToolManager.definitions() 的 dict 格式转换为 DelegationPreparer 期望的 Tool 列表
        from Module.task_system.schema import Tool
        tool_defs = self._tool_executor.definitions() if self._tool_executor else []
        tool_list = [
            Tool(
                name=d["function"]["name"],
                description=d["function"].get("description", ""),
                capabilities=[],
            )
            for d in tool_defs
        ]

        prep_result = await self._delegation_preparer.prepare(
            intent=intent,
            hints=hints,
            session_messages=self._session.recent_messages(),
            user_profile=self._user_profile or _default_user_profile(),
            tools=tool_list,
            global_constraints=(self._task_system._infra.constraints
                                if self._task_system else None),
            session_id=getattr(self._session, "_session_id", "default"),
        )

        if prep_result.status.value == "no_matching_tools":
            missing = getattr(prep_result, 'missing_tools', [])
            missing_str = ", ".join(missing) if missing else "未知"
            return ToolResult.fail(name="submit_task",
                error=f"缺少能力: {missing_str}。请直接告知用户当前不支持此功能，不要重复尝试。")
        if prep_result.status.value == "too_vague":
            suggestion = getattr(prep_result, 'suggestion', '') or '能再具体一点吗？'
            return ToolResult.fail(name="submit_task",
                error=f"意图不够明确: {suggestion}。请向用户澄清，不要重复尝试。")

        # 3. TaskSystem.submit
        if self._task_system is None:
            return ToolResult.fail(name="submit_task",
                error="TaskSystem 未初始化，请告知用户后台任务系统暂不可用。")

        mailbox_or_error = await self._task_system.submit(prep_result.context)
        if isinstance(mailbox_or_error, SubmitError):
            return ToolResult.fail(name="submit_task",
                error=f"任务提交失败: {mailbox_or_error.message}。请告知用户具体原因。")

        # 4. 成功
        return ToolResult.ok(
            name="submit_task",
            content="好的，已经在后台执行了。",
            duration_ms=0.0,
            async_task_id=str(mailbox_or_error.task_id),
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
