"""CognitiveLoop — the core ReAct scheduler.

Orchestrates THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE.
Uses structured streaming for sub-100ms TTFT and simultaneous tool-call detection.
"""

import asyncio
import json
import logging
from dataclasses import dataclass

from Module.output import OutputManager, ChannelCapability
from Module.tts import TTSBackend

from .agent_fsm import AgentFSM, AgentState
from .event_bus import EventBus, EventPriority, EventType
from .tool_registry import ToolRegistry
from .prompt_assembler import PromptAssembler
from .memory.session import SessionMemory
from .sentence_buffer import SentenceBuffer
from .llm_adapter import LLMAdapter
from .emotion_engine import EmotionEngine
from .consolidator import MemoryConsolidator
from .json_parser import TagParser

logger = logging.getLogger(__name__)


@dataclass
class _MessageObj:
    role: str = "assistant"
    content: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


class CognitiveLoop:
    """ReAct cognitive loop with streaming delivery and tool-call detection."""

    MAX_TOOL_ROUNDS = 3

    def __init__(
        self,
        llm: LLMAdapter,
        fsm: AgentFSM,
        event_bus: EventBus,
        output: OutputManager,
        prompt: PromptAssembler,
        session: SessionMemory,
        tools: ToolRegistry,
        emotion: EmotionEngine | None = None,
        consolidator: MemoryConsolidator | None = None,
        tts: TTSBackend | None = None,
    ):
        self._llm = llm
        self._fsm = fsm
        self._bus = event_bus
        self._output = output
        self._prompt = prompt
        self._session = session
        self._tools = tools
        self._emotion = emotion
        self._consolidator = consolidator
        self._tts = tts
        self._sentence_buf = SentenceBuffer()
        self._current_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None
        self._tts_tasks: list[asyncio.Task] = []
        self._tts_stream_id: int = 0
        self._sync_event = asyncio.Event()
        self._sync_buffer: list[str] = []
        self._audio_pending: list[tuple] = []  # (type, chunk|None, stream_id)
        self._audio_flow = False

    async def process(self, user_message: str, user_name: str = "妖梦") -> None:
        if not self._fsm.can_transition(AgentState.THINKING):
            logger.warning("Cannot transition to THINKING from %s", self._fsm.state)
            return

        self._fsm.transition(AgentState.THINKING)
        self._bus.post(EventType.STATE_CHANGE, {"state": "thinking"}, priority=EventPriority.STATE_CHANGE)

        self._sentence_buf.reset()
        self._sync_event.clear()
        self._sync_buffer.clear()
        self._audio_pending.clear()
        self._audio_flow = False
        self._current_task = asyncio.current_task()
        try:
            await self._think_loop(user_message, user_name)
        except asyncio.CancelledError:
            logger.info("Cognitive loop interrupted")
            await self._handle_interrupt()

    async def _think_loop(self, user_message: str, user_name: str) -> None:
        if self._emotion:
            self._emotion.analyze_user(user_message)

        messages = await self._prompt.build_messages(user_message, user_name=user_name)
        self._session.add_message("user", user_message)
        tool_defs = self._tools.tool_definitions() or None

        await self._output.typing(True)

        # 文本/语音同步：首句 TTS 音频到达前，暂存 text delta 不发送
        # 无 TTS 时立即释放。超时作为安全网防止永久持有。
        _sync_timeout = 5.0
        self._sync_event.clear()
        self._sync_buffer.clear()
        if not self._tts:
            self._sync_event.set()
            logger.debug("Sync: TTS 未启用，文本直接放行")

        async def _release_after_timeout():
            await asyncio.sleep(_sync_timeout)
            if not self._sync_event.is_set():
                logger.debug("Sync: %.1fs 超时，释放文本 (%d 字符缓冲)",
                             _sync_timeout, sum(len(s) for s in self._sync_buffer))
            self._sync_event.set()

        timeout_task = asyncio.create_task(_release_after_timeout(), name="sync-timeout")

        try:
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                # Stream the LLM response — text goes to output, tool_calls are accumulated
                acc_text = ""
                raw_tool_calls: list[dict] = []
                speaking_started = False
                dual = self._prompt.dual_track
                json_parser = TagParser() if dual else None

                stream = self._llm.stream_think(messages, tools=tool_defs)
                try:
                    async for chunk in stream:
                        content = chunk.get("content")
                        tc_deltas = chunk.get("tool_calls")

                        if content:
                            if dual:
                                # Route through JSON parser — only public_reply goes to output
                                parsed = json_parser.feed(content)
                                if parsed.get("field") and parsed["field"].name == "PUBLIC_REPLY":
                                    content = parsed["content"]
                                    acc_text += content
                                elif parsed.get("field") and parsed["field"].name == "INNER_VOICE":
                                    logger.debug("inner_voice: %s", parsed["content"])
                                    continue  # inner_voice → debug only
                                else:
                                    continue  # between fields or mood
                            else:
                                acc_text += content

                            if not self._sync_event.is_set():
                                self._sync_buffer.append(content)
                            else:
                                # First batch after sync: flush buffered deltas
                                if self._sync_buffer:
                                    n = len(self._sync_buffer)
                                    logger.info("Sync: 刷新 %d 个缓冲文本块", n)
                                    await self._output.typing(False)
                                    self._fsm.transition(AgentState.SPEAKING)
                                    self._bus.post(EventType.STATE_CHANGE, {"state": "speaking"}, priority=EventPriority.STATE_CHANGE)
                                    speaking_started = True
                                    for buf_content in self._sync_buffer:
                                        await self._output._broadcast("on_text_delta", buf_content, capability=ChannelCapability.TEXT_DELTA)
                                    self._sync_buffer.clear()
                                    # Release buffered audio now that text has been sent
                                    await self._drain_audio_pending()

                                if not speaking_started:
                                    await self._output.typing(False)
                                    self._fsm.transition(AgentState.SPEAKING)
                                    self._bus.post(EventType.STATE_CHANGE, {"state": "speaking"}, priority=EventPriority.STATE_CHANGE)
                                    speaking_started = True

                                await self._output._broadcast("on_text_delta", content, capability=ChannelCapability.TEXT_DELTA)

                            sentence = self._sentence_buf.feed(content)
                            if sentence and self._tts:
                                self._start_tts_stream(sentence)

                        if tc_deltas:
                            self._merge_stream_tool_calls(raw_tool_calls, tc_deltas)
                except asyncio.CancelledError:
                    # Propagate cancellation — cleanup happens in process()
                    raise

                # Flush remaining text in sentence buffer
                if self._tts:
                    remaining = self._sentence_buf.flush()
                    if remaining:
                        self._start_tts_stream(remaining)

                # If tool calls were detected, execute them and loop
                if raw_tool_calls and round_num < self.MAX_TOOL_ROUNDS:
                    await self._output.typing(False)
                    self._fsm.transition(AgentState.EXECUTING)
                    self._bus.post(EventType.STATE_CHANGE, {"state": "executing"}, priority=EventPriority.STATE_CHANGE)

                    normalized = self._normalize_tool_calls(raw_tool_calls)
                    assistant_entry: dict = {"role": "assistant", "content": acc_text or None}
                    if normalized:
                        assistant_entry["tool_calls"] = normalized
                    messages.append(assistant_entry)

                    for tc in normalized:
                        func_name = tc["function"]["name"]
                        try:
                            args = json.loads(tc["function"]["arguments"])
                        except (json.JSONDecodeError, KeyError):
                            args = {}
                        logger.info("Executing tool: %s(%s)", func_name, args)
                        tool_result = await self._tools.execute(func_name, args)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", "unknown"),
                            "content": tool_result,
                        })

                    self._fsm.transition(AgentState.THINKING)
                    self._bus.post(EventType.STATE_CHANGE, {"state": "thinking"}, priority=EventPriority.STATE_CHANGE)
                    await self._output.typing(True)
                    continue

                # Final answer — finalize the message
                if dual:
                    # Parse finished JSON to extract public_reply + inner_voice + mood
                    result = json_parser.finish()
                    reply = result["public_reply"].strip()
                    inner = result["inner_voice"].strip()
                    mood_val = result.get("mood", "").strip()
                    if inner:
                        logger.info("inner_voice: %s", inner[:200])
                    if mood_val:
                        logger.info("dual_track mood: %s", mood_val)
                        self._session.set_param("mood", mood_val)
                    if reply:
                        if self._emotion:
                            self._emotion.analyze_assistant(reply)
                        if not speaking_started:
                            await self._output.typing(False)
                        msg = _MessageObj(content=reply)
                        await self._output.send(msg)
                        self._session.add_message("assistant", reply)
                        asyncio.create_task(self._after_response(user_message, reply), name="after-response")
                elif speaking_started:
                    if acc_text.strip():
                        if self._emotion:
                            self._emotion.analyze_assistant(acc_text)
                        msg = _MessageObj(content=acc_text)
                        await self._output.send(msg)
                        self._session.add_message("assistant", acc_text)
                        asyncio.create_task(self._after_response(user_message, acc_text), name="after-response")
                else:
                    await self._output.typing(False)
                    if acc_text.strip():
                        # Flush buffered deltas so browser DOM is built before finalize
                        if self._sync_buffer:
                            self._fsm.transition(AgentState.SPEAKING)
                            self._bus.post(EventType.STATE_CHANGE, {"state": "speaking"}, priority=EventPriority.STATE_CHANGE)
                            for buf_content in self._sync_buffer:
                                await self._output._broadcast("on_text_delta", buf_content, capability=ChannelCapability.TEXT_DELTA)
                            self._sync_buffer.clear()
                            await self._drain_audio_pending()
                        if self._emotion:
                            self._emotion.analyze_assistant(acc_text)
                        msg = _MessageObj(content=acc_text)
                        await self._output.send(msg)
                        self._session.add_message("assistant", acc_text)
                        asyncio.create_task(self._after_response(user_message, acc_text), name="after-response")
                await self._transition_idle()
                return

        finally:
            timeout_task.cancel()
            # Flush any remaining buffered text (e.g. response too short for TTS)
            if self._sync_buffer:
                await self._output.typing(False)
                for buf_content in self._sync_buffer:
                    await self._output._broadcast("on_text_delta", buf_content, capability=ChannelCapability.TEXT_DELTA)
                self._sync_buffer.clear()
            # Always drain buffered audio — bg task may have accumulated chunks
            # that arrived after the else-branch cleared sync_buffer
            await self._drain_audio_pending()

    async def _after_response(self, user_message: str, assistant_response: str) -> None:
        """Post-response hooks: consolidate memory, save session."""
        if self._consolidator:
            try:
                items = await self._consolidator.consolidate(user_message, assistant_response)
                if items:
                    logger.debug("Consolidated %d items", len(items))
            except Exception:
                logger.debug("Consolidation failed", exc_info=True)
        # Save session asynchronously (fire-and-forget)
        try:
            await self._session.save()
        except Exception:
            logger.debug("Session save failed", exc_info=True)

    def _start_tts_stream(self, sentence: str) -> None:
        """Start streaming TTS for a sentence in a background task.

        Synthesis starts immediately (pipeline with previous sentence).
        Audio chunks are sent as they arrive; the browser queues playback.
        Only interrupt cancels the current TTS stream.
        Does NOT block — text streaming continues in parallel.
        """
        self._tts_stream_id += 1
        stream_id = self._tts_stream_id

        async def _stream(_sid: int):
            if not self._tts:
                return
            try:
                char_name = self._prompt._character.name
                first_chunk = True
                async for chunk in self._tts.synthesize_stream(sentence, character=char_name):
                    if first_chunk:
                        first_chunk = False
                        if not self._sync_event.is_set():
                            logger.debug("Sync: 首块 TTS 音频到达，释放文本")
                        self._sync_event.set()
                    if not self._audio_flow:
                        self._audio_pending.append(('chunk', chunk, _sid))
                    else:
                        await self._output.audio_chunk(chunk, stream_id=_sid)
                if not self._audio_flow:
                    self._audio_pending.append(('end', None, _sid))
                else:
                    await self._output.audio_end(_sid)
            except asyncio.CancelledError:
                pass  # Interrupted by newer speech
            except Exception:
                logger.debug("TTS stream failed", exc_info=True)

        task = asyncio.create_task(_stream(stream_id), name=f"tts-{stream_id}")
        self._tts_tasks.append(task)
        self._tts_task = task

        # Clean up completed tasks
        self._tts_tasks = [t for t in self._tts_tasks if not t.done()]

    async def _drain_audio_pending(self) -> None:
        """发送所有缓冲的音频块 + 结束标记，然后启用直通模式。

        无论当前缓冲是否为空，始终将 _audio_flow 设为 True，
        确保后续 TTS 块不再进入缓冲（TTS 可能尚未开始）。
        """
        self._audio_flow = True
        if not self._audio_pending:
            logger.debug("Sync: 音频排空 — 缓冲为空，已启用直通")
            return
        chunks = sum(1 for kind, _, _ in self._audio_pending if kind == 'chunk')
        ends = sum(1 for kind, _, _ in self._audio_pending if kind == 'end')
        logger.info("Sync: 排空 %d 音频块 + %d 结束标记", chunks, ends)
        for item in self._audio_pending:
            kind, data, sid = item
            if kind == 'chunk':
                await self._output.audio_chunk(data, stream_id=sid)
            elif kind == 'end':
                await self._output.audio_end(sid)
        self._audio_pending.clear()

    async def _synthesize_sentence(self, sentence: str) -> None:
        """Legacy non-streaming TTS — kept for backward compat with tests."""
        if not self._tts:
            return
        try:
            char_name = self._prompt._character.name
            audio = await self._tts.synthesize(sentence, character=char_name)
            await self._output.audio_chunk(audio)
        except Exception:
            logger.debug("TTS synthesis failed", exc_info=True)

    async def _transition_idle(self) -> None:
        await asyncio.sleep(0.1)
        # If sync held text and never fired (no TTS / no timeout yet),
        # FSM may still be in THINKING. Step through SPEAKING first.
        if self._fsm.state == AgentState.THINKING:
            self._fsm.transition(AgentState.SPEAKING)
            self._bus.post(EventType.STATE_CHANGE, {"state": "speaking"}, priority=EventPriority.STATE_CHANGE)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(EventType.STATE_CHANGE, {"state": "idle"}, priority=EventPriority.STATE_CHANGE)

    async def _handle_interrupt(self) -> None:
        """Clean up after interruption — stop output, clear buffers, return to IDLE."""
        self._sentence_buf.reset()
        self._sync_buffer.clear()
        self._sync_event.clear()
        self._audio_pending.clear()
        self._audio_flow = False
        for t in self._tts_tasks:
            if not t.done():
                t.cancel()
        self._tts_tasks.clear()
        await self._output.typing(False)
        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(EventType.STATE_CHANGE, {"state": "interrupted"}, priority=EventPriority.STATE_CHANGE)
        await asyncio.sleep(0.05)  # Brief pause for UI to register INTERRUPTED
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(EventType.STATE_CHANGE, {"state": "idle"}, priority=EventPriority.STATE_CHANGE)
        logger.debug("Interrupt complete — agent is IDLE")

    # ---- Tool call helpers ----

    @staticmethod
    def _merge_stream_tool_calls(accumulated: list[dict], deltas: list[dict]) -> None:
        """Merge streaming tool_call deltas into the accumulated list (mutates in place)."""
        for delta in deltas:
            idx = delta.get("index", 0)
            while len(accumulated) <= idx:
                accumulated.append({
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
            if delta.get("id"):
                accumulated[idx]["id"] = delta["id"]
            if "function" in delta:
                func = delta["function"]
                if func.get("name"):
                    accumulated[idx]["function"]["name"] += func["name"]
                if func.get("arguments"):
                    accumulated[idx]["function"]["arguments"] += func["arguments"]

    @staticmethod
    def _normalize_tool_calls(raw: list[dict]) -> list[dict]:
        """Return only tool calls that have an id and name (complete)."""
        result = []
        for tc in raw:
            if tc.get("id") and tc["function"].get("name"):
                result.append(tc)
        return result
