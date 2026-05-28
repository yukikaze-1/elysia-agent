"""CognitiveLoop — the core ReAct scheduler.

Orchestrates THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE.
Uses structured streaming for sub-100ms TTFT and simultaneous tool-call detection.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from Module.output import OutputManager, ChannelCapability
from Module.tts import TTSBackend

from .agent_fsm import AgentFSM, AgentState
from .event_bus import EventBus, EventPriority, EventType
from Module.tool import ToolRegistry, ToolContext
from .prompt_assembler import PromptAssembler
from .memory.session import SessionMemory
from .sentence_buffer import SentenceBuffer
from .llm_adapter import LLMAdapter
from .emotion_engine import EmotionEngine
from .consolidator import MemoryConsolidator
logger = logging.getLogger(__name__)

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF"
    r"\U0001F600-\U0001F64F"
    r"\U00002600-\U000027BF"
    r"\U00002300-\U000023FF]+",
    flags=re.UNICODE,
)


def _is_emoji_only(text: str) -> bool:
    """句子去掉 emoji 后是否只剩空白 — 用于跳过 TTS 合成。"""
    return not _EMOJI_RE.sub("", text).strip()


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
        ctx: ToolContext | None = None,
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
        self._ctx = ctx
        self._sentence_buf = SentenceBuffer()
        self._process_lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None
        self._tts_tasks: list[asyncio.Task] = []
        self._tts_stream_id: int = 0

    async def process(self, user_message: str, user_name: str = "妖梦",
                       stt_emotion: str = "", stt_events: list[str] | None = None) -> None:
        async with self._process_lock:
            if not self._fsm.can_transition(AgentState.THINKING):
                logger.warning("Cannot transition to THINKING from %s", self._fsm.state)
                return

            self._fsm.transition(AgentState.THINKING)
            self._bus.post(EventType.STATE_CHANGE, {"state": "thinking"}, priority=EventPriority.STATE_CHANGE)

            self._sentence_buf.reset()
            self._current_task = asyncio.current_task()
            try:
                await self._think_loop(user_message, user_name,
                                       stt_emotion=stt_emotion, stt_events=stt_events)
            except asyncio.CancelledError:
                logger.info("Cognitive loop interrupted")
                await self._handle_interrupt()

    async def _think_loop(self, user_message: str, user_name: str,
                          stt_emotion: str = "", stt_events: list[str] | None = None) -> None:
        if self._emotion:
            if stt_emotion and stt_emotion != "NEUTRAL":
                self._emotion.analyze_user_stt(stt_emotion, stt_events)
            else:
                self._emotion.analyze_user(user_message)

        messages = await self._prompt.build_messages(
            user_message, user_name=user_name,
            stt_emotion=stt_emotion, stt_events=stt_events,
        )
        self._session.add_message("user", user_message)
        tool_defs = self._tools.definitions() or None

        await self._output.typing(True)

        try:
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                acc_text = ""
                raw_tool_calls: list[dict] = []
                speaking_started = False
                ttft_logged = False

                stream = self._llm.stream_think(messages, tools=tool_defs,
                    extra_body={"thinking": {"type": "disabled"}})
                try:
                    async for chunk in stream:
                        content = chunk.get("content")
                        tc_deltas = chunk.get("tool_calls")

                        if content:
                            if not ttft_logged:
                                ttft_logged = True
                                logger.info("Pipeline: 首 token 到达 (TTFT)")
                            acc_text += content

                            # TTS 关闭时流式逐字发送；TTS 开启时不发（由 _start_tts_stream 发送完整句）
                            if not self._tts:
                                await self._output._broadcast("on_text_delta", content,
                                    capability=ChannelCapability.TEXT_DELTA)

                            sentence = self._sentence_buf.feed(content)
                            if sentence:
                                logger.info("Pipeline: SentenceBuffer 分句 (%d 字符): %s",
                                             len(sentence), sentence[:40])

                                if not speaking_started:
                                    await self._output.typing(False)
                                    self._fsm.transition(AgentState.SPEAKING)
                                    self._bus.post(EventType.STATE_CHANGE, {"state": "speaking"},
                                                   priority=EventPriority.STATE_CHANGE)
                                    speaking_started = True

                                # 纯 emoji 跳过 TTS，直接发文字
                                if self._tts and not _is_emoji_only(sentence):
                                    self._start_tts_stream(sentence)
                                else:
                                    await self._output._broadcast("on_text_delta", sentence,
                                        capability=ChannelCapability.TEXT_DELTA)
                                    await self._output.send("")

                        if tc_deltas:
                            self._merge_stream_tool_calls(raw_tool_calls, tc_deltas)
                except asyncio.CancelledError:
                    raise

                # Flush remaining text in sentence buffer
                remaining = self._sentence_buf.flush()
                if remaining:
                    logger.info("Pipeline: SentenceBuffer flush 尾句 (%d 字符): %s",
                                 len(remaining), remaining[:40])
                    if not speaking_started:
                        await self._output.typing(False)
                        self._fsm.transition(AgentState.SPEAKING)
                        self._bus.post(EventType.STATE_CHANGE, {"state": "speaking"},
                                       priority=EventPriority.STATE_CHANGE)
                        speaking_started = True
                    if self._tts and not _is_emoji_only(remaining):
                        self._start_tts_stream(remaining)
                    else:
                        await self._output._broadcast("on_text_delta", remaining,
                            capability=ChannelCapability.TEXT_DELTA)
                        await self._output.send("")

                # If tool calls were detected, execute them and loop
                if raw_tool_calls and round_num < self.MAX_TOOL_ROUNDS:
                    # Cancel pending TTS tasks, send their text without audio
                    for t in self._tts_tasks:
                        if not t.done():
                            t.cancel()
                    self._tts_tasks.clear()

                    # 切断气泡
                    await self._output.send("")

                    await self._output.typing(False)
                    self._fsm.transition(AgentState.EXECUTING)
                    self._bus.post(EventType.STATE_CHANGE, {"state": "executing"},
                                   priority=EventPriority.STATE_CHANGE)

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
                        await self._output.action("tool_executing", params={
                            "tool": func_name,
                            "args": {k: str(v)[:80] for k, v in args.items()},
                        })
                        tool_result = await self._tools.execute(func_name, args, ctx=self._ctx)
                        await self._output.action("tool_done", params={
                            "tool": func_name,
                            "success": tool_result.success,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", "unknown"),
                            "content": tool_result.llm_content,
                        })

                    self._fsm.transition(AgentState.THINKING)
                    self._bus.post(EventType.STATE_CHANGE, {"state": "thinking"},
                                   priority=EventPriority.STATE_CHANGE)
                    await self._output.typing(True)
                    continue

                # Final answer — TTS tasks streaming in background, don't block
                if acc_text.strip():
                    if self._emotion:
                        self._emotion.analyze_assistant(acc_text)
                    if not speaking_started:
                        # 无标点的极短回复，直接发完整消息
                        await self._output.typing(False)
                        msg = _MessageObj(content=acc_text)
                        await self._output.send(msg)
                if acc_text.strip():
                    self._session.add_message("assistant", acc_text)
                    asyncio.create_task(self._after_response(user_message, acc_text),
                                       name="after-response")

                await self._transition_idle()
                return

        except Exception:
            for t in self._tts_tasks:
                if not t.done():
                    t.cancel()
            self._tts_tasks.clear()
            raise

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
        """后台 TTS task — 等首块音频到达后绑在一起发送文字+音频。

        文字和音频绑定发送，前端收到即可渲染+播放，无需任何同步逻辑。
        TTS workers=1 FIFO 保证句间顺序。
        """
        # 跳过纯 emoji/空白句 — strip 后为空，GPT-SoVits 会返回 400
        _clean = _EMOJI_RE.sub("", sentence).strip()
        if not _clean:
            return

        self._tts_stream_id += 1
        sid = self._tts_stream_id
        logger.info("Pipeline: TTS 句 %d 开始合成 (%d 字符): %s",
                    sid, len(sentence), _clean[:50])

        async def _stream(_sid: int):
            if not self._tts:
                return
            try:
                char_name = self._prompt._character.name
                first_chunk = True
                async for chunk in self._tts.synthesize_stream(sentence, character=char_name):
                    if first_chunk:
                        first_chunk = False
                        logger.info("Pipeline: 首块 TTS 音频到达，发送文字+音频")
                        # 绑定发送：文字先行，再流式音频
                        await self._output._broadcast("on_text_delta", sentence,
                            capability=ChannelCapability.TEXT_DELTA)
                        await self._output.send("")
                        # 排空此前可能缓冲的音频块（应该为空）
                    await self._output.audio_chunk(chunk, stream_id=_sid)
                await self._output.audio_end(_sid)
            except asyncio.CancelledError:
                pass  # INTERRUPT 打断
            except Exception:
                logger.warning("TTS stream failed — fallback 发送文字", exc_info=True)
                # TTS 失败兜底: 至少把文字发出去
                if first_chunk:
                    try:
                        await self._output._broadcast("on_text_delta", sentence,
                            capability=ChannelCapability.TEXT_DELTA)
                        await self._output.send("")
                    except Exception:
                        pass

        task = asyncio.create_task(_stream(sid), name=f"tts-{sid}")
        self._tts_tasks.append(task)
        self._tts_task = task

        # Clean up completed tasks
        self._tts_tasks = [t for t in self._tts_tasks if not t.done()]

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
        logger.info("Pipeline: 回复完成 → IDLE")
        await asyncio.sleep(0.1)
        if self._fsm.state == AgentState.THINKING:
            self._fsm.transition(AgentState.SPEAKING)
            self._bus.post(EventType.STATE_CHANGE, {"state": "speaking"}, priority=EventPriority.STATE_CHANGE)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(EventType.STATE_CHANGE, {"state": "idle"}, priority=EventPriority.STATE_CHANGE)

    async def _handle_interrupt(self) -> None:
        """Clean up after interruption — cancel TTS tasks, return to IDLE."""
        self._sentence_buf.reset()
        for t in self._tts_tasks:
            if not t.done():
                t.cancel()
        self._tts_tasks.clear()
        await self._output.typing(False)
        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(EventType.STATE_CHANGE, {"state": "interrupted"}, priority=EventPriority.STATE_CHANGE)
        await asyncio.sleep(0.05)
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
