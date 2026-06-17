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
import json
import logging
import time
from dataclasses import asdict

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
from .emotion_engine import EmotionEngine
from .consolidator import MemoryConsolidator
from Module.llm.registry import LLMFactory

from .block_filter import BlockFilter, build_parts_with_blocks
from .tts_stream import is_emoji_only, TTSStreamManager
from .tool_call_parser import merge_stream_tool_calls, normalize_tool_calls
from .text_utils import content_text_from_content, MessageObj

logger = logging.getLogger(__name__)


class CognitiveLoop:
    """ReAct cognitive loop with streaming delivery and tool-call detection."""

    MAX_TOOL_ROUNDS = 5
    _req_counter: int = 0

    def __init__(
        self,
        llm: LLMAdapter,
        fsm: AgentFSM,
        event_bus: EventBus,
        output: OutputManager,
        prompt: PromptAssembler,
        session: SessionMemory,
        tool_executor=None,  # ToolExecutor Protocol
        emotion: EmotionEngine | None = None,
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
        self._emotion = emotion
        self._consolidator = consolidator
        self._tts = tts  # 保留供 agent.py stop() 读取
        self._ctx = ctx
        self._token_tracker = token_tracker
        self._prompt_recorder = prompt_recorder
        self._sentence_buf = SentenceBuffer()
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

    # ---- Public API ----

    def cancel_current(self) -> bool:
        """取消当前认知任务（如有）。返回 True 表示确实取消了任务。"""
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            return True
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

    # ---- Main entry ----

    async def process(self, user_message: str = "", user_name: str = "妖梦",
                       stt_emotion: str = "", stt_events: list[str] | None = None,
                       _on_tool_calls=None,
                       content: list[dict] | None = None) -> None:
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
            self._bus.post(EventType.STATE_CHANGE, {"state": "thinking", "request_id": rid},
                          priority=EventPriority.STATE_CHANGE)

            self._sentence_buf.reset()
            self._current_task = asyncio.current_task()
            try:
                await self._think_loop(user_message, user_name,
                                       stt_emotion=stt_emotion, stt_events=stt_events,
                                       _on_tool_calls=_on_tool_calls, content=content)
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
                          content: list[dict] | None = None) -> None:
        if self._emotion:
            if stt_emotion and stt_emotion != "NEUTRAL":
                self._emotion.analyze_user_stt(stt_emotion, stt_events)
            else:
                self._emotion.analyze_user(user_message)

        assert self._clock is not None
        with self._clock.span("prompt") as t:
            supports_mm = LLMFactory.supports_multimodal(self._llm.model)
            messages = await self._prompt.build_messages(
                user_message=user_message, user_name=user_name,
                stt_emotion=stt_emotion, stt_events=stt_events,
                content=content,
                supports_multimodal=supports_mm,
            )
        user_content = content if content else [{"type": "text", "content": user_message}]
        self._session.add_message({
            "id": new_message_id(),
            "role": "user",
            "content": user_content,
            "timestamp": datetime.datetime.now().astimezone().isoformat(),
        })
        tool_defs = self._tool_executor.definitions() if self._tool_executor else None
        # 多模态 LLM 可直接看图，排除 describe_image 避免 LLM 只调用一次
        # （只处理 image_index=0 而忽略其余图片）
        if tool_defs and supports_mm:
            tool_defs = [t for t in tool_defs if t.get("function", {}).get("name") != "describe_image"]
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
                block_filter = BlockFilter()
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

                stream = self._llm.stream_think(messages, tools=tool_defs,
                    extra_body={"thinking": {"type": "disabled"}})
                try:
                    async for chunk in stream:
                        content = chunk.get("content")
                        tc_deltas = chunk.get("tool_calls")

                        if content:
                            if not ttft_done:
                                ttft_done = True
                                ttft_ms = int((time.perf_counter() - t_think_start) * 1000)
                                assert self._clock is not None
                                self._clock._spans["ttft"] = ttft_ms
                                logger.debug("TTFT: %d ms", ttft_ms)
                            acc_text += content

                            # 过滤结构化块标记 — 代码和 markdown 不走 TTS
                            clean = block_filter.feed(content)

                            # TTS 关闭时流式逐字发送；TTS 开启时不发（由 TTSStreamManager 发送完整句）
                            if not self._tts_mgr.active and clean:
                                await self._output._broadcast("on_text_delta", clean,
                                    capability=ChannelCapability.TEXT_DELTA)

                            sentence = self._sentence_buf.feed(clean) if clean else None
                            if sentence:
                                all_sentences.append(sentence)
                                logger.info("分句 (%d 字符)", len(sentence))
                                logger.debug("分句内容: %s", sentence[:60])

                                if not speaking_started:
                                    await self._output.typing(False)
                                    self._fsm.transition(AgentState.SPEAKING)
                                    self._bus.post(EventType.STATE_CHANGE, {"state": "speaking", "request_id": get_request_id()},
                                                   priority=EventPriority.STATE_CHANGE)
                                    speaking_started = True

                                # TTS 句: 后台 task 绑在一起发送；emoji: 排队跟在上一句后面
                                if self._tts_mgr.active:
                                    self._tts_mgr.stream_sentence(sentence)
                                else:
                                    await self._output._broadcast("on_text_delta", sentence,
                                        capability=ChannelCapability.TEXT_DELTA)
                                    await self._output.send("")

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

                # Flush remaining text in sentence buffer + block filter
                trailing = block_filter.flush()
                if trailing:
                    self._sentence_buf.feed(trailing)
                remaining = self._sentence_buf.flush()
                if remaining:
                    if is_emoji_only(remaining):
                        logger.info("flush emoji: %s", remaining)
                    else:
                        all_sentences.append(remaining)
                        logger.info("flush 尾句 (%d 字符)", len(remaining))
                        logger.debug("尾句内容: %s", remaining[:60])
                    if not speaking_started:
                        await self._output.typing(False)
                        self._fsm.transition(AgentState.SPEAKING)
                        self._bus.post(EventType.STATE_CHANGE, {"state": "speaking", "request_id": get_request_id()},
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
                    self._bus.post(EventType.STATE_CHANGE, {"state": "executing", "request_id": get_request_id()},
                                   priority=EventPriority.STATE_CHANGE)

                    normalized = normalize_tool_calls(raw_tool_calls)
                    assistant_entry: dict = {"role": "assistant", "content": acc_text or None}
                    if normalized:
                        assistant_entry["tool_calls"] = normalized
                    messages.append(assistant_entry)

                    # 工具调用消息写入 Session（刷新后前端可恢复工具卡片）
                    self._session.add_message({
                        "id": new_message_id(),
                        **assistant_entry,
                        "timestamp": datetime.datetime.now().astimezone().isoformat(),
                    })

                    # Emit tool_executing actions for UI (before dispatch)
                    for tc in normalized:
                        func_name = tc["function"]["name"]
                        call_id = tc.get("id", "unknown")
                        try:
                            args = json.loads(tc["function"]["arguments"])
                        except (json.JSONDecodeError, KeyError):
                            args = {}
                        self._executing_tools.append(func_name)
                        await self._output.action("tool_executing", params={
                            "tool": func_name,
                            "call_id": call_id,
                            "args": {k: str(v) for k, v in args.items()},
                        })
                    
                    assert self._clock is not None
                    with self._clock.span("tools"):
                        if _on_tool_calls is not None:
                            results = await _on_tool_calls(normalized)
                        else:
                            results = await self._execute_tools_directly(normalized)

                    for item in results:
                        if isinstance(item, BaseException):
                            continue  # 工具被 cancel，跳过 — LLM 从缺失结果推断
                        tc, tool_result = item
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", "unknown"),
                            "content": tool_result.llm_content,
                        })
                        # Emit tool_done for each completed tool
                        error_type_str = tool_result.error_type.value if tool_result.error_type else None
                        await self._output.action("tool_done", params={
                            "tool": tc["function"]["name"],
                            "call_id": tc.get("id", "unknown"),
                            "success": tool_result.success,
                            "content": tool_result.content or "",
                            "error": tool_result.error,
                            "error_type": error_type_str,
                            "duration_ms": tool_result.duration_ms,
                            "truncated": tool_result.truncated,
                        })
                        # 工具结果写入 Session（刷新后前端可恢复工具卡片内容）
                        self._session.add_message({
                            "id": new_message_id(),
                            "role": "tool",
                            "tool_call_id": tc.get("id", "unknown"),
                            "name": tc["function"]["name"],
                            "content": tool_result.content or "",
                            "success": tool_result.success,
                            "error": tool_result.error,
                            "error_type": error_type_str,
                            "duration_ms": tool_result.duration_ms,
                            "truncated": tool_result.truncated,
                            "timestamp": datetime.datetime.now().astimezone().isoformat(),
                        })

                    # Clean up executing tools tracking
                    for tc in normalized:
                        func_name = tc["function"]["name"]
                        try:
                            self._executing_tools.remove(func_name)
                        except ValueError:
                            pass

                    self._fsm.transition(AgentState.THINKING)
                    self._bus.post(EventType.STATE_CHANGE, {"state": "thinking", "request_id": get_request_id()},
                                   priority=EventPriority.STATE_CHANGE)
                    await self._output.typing(True)
                    continue

                # Final answer — TTS tasks streaming in background, don't block
                # 发送还未排出的 emoji（最后一句 TTS 之后的）
                await self._tts_mgr.flush_emojis()

                if acc_text.strip():
                    if self._emotion:
                        self._emotion.analyze_assistant(acc_text)
                    if not speaking_started:
                        # 无标点的极短回复，直接发完整消息
                        await self._output.typing(False)
                        msg = MessageObj(content=acc_text)
                        await self._output.send(msg)
                if acc_text.strip():
                    parts, content_type = build_parts_with_blocks(acc_text, all_sentences)
                    # 构建 assistant 消息的 content 数组（text parts + 保留 content_type 信息）
                    assistant_content: list[dict] = []
                    for p in parts:
                        if isinstance(p, dict) and "content" in p:
                            assistant_content.append({"type": "text", "content": p["content"]})
                    self._session.add_message({
                        "id": new_message_id(),
                        "role": "assistant",
                        "content": assistant_content,
                        "content_type": content_type,
                        "timestamp": datetime.datetime.now().astimezone().isoformat(),
                        "token_usage": {
                            "rounds": [asdict(r) for r in self._token_tracker.current_turn_usages],
                            "turn_total": sum(r.total_tokens for r in self._token_tracker.current_turn_usages),
                        } if self._token_tracker and self._token_tracker.current_turn_usages else None,
                    })
                    log_conversation("assistant", self._prompt._character.name, acc_text)
                    asyncio.create_task(self._after_response(user_message, acc_text),
                                       name="after-response")

                    # 推送结构化块到前端（不走 TTS，单独渲染）
                    for idx in content_type.get("markdown", []):
                        if idx < len(parts):
                            await self._output.command("block", params={
                                "block_type": "markdown",
                                "content": parts[idx]["content"],
                            })
                    for idx in content_type.get("code", []):
                        if idx < len(parts):
                            await self._output.command("block", params={
                                "block_type": "code",
                                "content": parts[idx]["content"],
                                "language": parts[idx].get("language", ""),
                            })

                await self._transition_idle()
                return

        except Exception:
            self._executing_tools.clear()
            self._tts_mgr.cancel_all()
            raise

    # ---- Post-response ----

    async def _after_response(self, user_message: str, assistant_response: str) -> None:
        """Post-response hooks: consolidate memory, save session."""
        if self._consolidator:
            try:
                t0 = time.perf_counter()
                items = await self._consolidator.consolidate(user_message, assistant_response)
                if items:
                    logger.debug("记忆沉淀: %d 条 (%d ms)", len(items),
                                 int((time.perf_counter() - t0) * 1000))
            except Exception:
                logger.debug("记忆沉淀失败", exc_info=True)
        # Save session asynchronously (fire-and-forget)
        try:
            await self._session.save()
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
        except Exception:
            logger.debug("TTS synthesis failed", exc_info=True)

    # ---- Tool execution ----

    async def _execute_tools_directly(
        self, normalized: list[dict],
    ) -> list:
        """直接执行工具调用 — 向后兼容，不经过 Scheduler Worker Pool。

        每个工具在独立的 asyncio.Task 中并行执行，
        CancelledError 由 asyncio.gather 的 return_exceptions 捕获。
        """
        async def _execute_one(tc: dict):
            func_name = tc["function"]["name"]
            t_tool = time.perf_counter()
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
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

    # ---- State transitions ----

    async def _transition_idle(self) -> None:
        """等待所有 TTS 任务完成，然后转换到 IDLE。"""
        await self._tts_mgr.wait_all()

        logger.info("→ IDLE")
        if self._fsm.state == AgentState.THINKING:
            # 仅 tool_calls 无文本时 FSM 仍在 THINKING，需经过 SPEAKING 才能到 IDLE
            self._fsm.transition(AgentState.SPEAKING)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(EventType.STATE_CHANGE, {"state": "idle", "request_id": get_request_id()}, priority=EventPriority.STATE_CHANGE)

    async def _handle_interrupt(self) -> None:
        """Clean up after interruption — cancel TTS tasks, return to IDLE."""
        self._sentence_buf.reset()
        self._executing_tools.clear()
        self._tts_mgr.cancel_all()
        await self._output.typing(False)
        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(EventType.STATE_CHANGE, {"state": "interrupted", "request_id": get_request_id()}, priority=EventPriority.STATE_CHANGE)
        await asyncio.sleep(0.05)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(EventType.STATE_CHANGE, {"state": "idle", "request_id": get_request_id()}, priority=EventPriority.STATE_CHANGE)
        logger.debug("Interrupt complete — agent is IDLE")
