"""CognitiveLoop — the core ReAct scheduler.

Orchestrates THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE.
Uses structured streaming for sub-100ms TTFT and simultaneous tool-call detection.
"""

import asyncio
import datetime
import json
import logging
import re
import time
from dataclasses import dataclass

from .message_id import new_message_id

from .timing import TimingClock

from Module.output import OutputManager, ChannelCapability
from Module.tts import TTSBackend
from Module.tts.text_preprocess import strip_all_markup

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
logger = logging.getLogger(__name__)

def content_text_from_content(content: list[dict]) -> str:
    """从 content 数组提取纯文本（给日志用）。"""
    return "".join(
        p.get("content", "") for p in content
        if isinstance(p, dict) and p.get("type") == "text"
    )


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


_BLOCK_PATTERN: re.Pattern = re.compile(
    r'<\|markdown\|>(.*?)</\|markdown\|>|'
    r'<\|code\|(\w+)?>(.*?)</\|code\|>',
    re.DOTALL,
)

_BLOCK_START_RE: re.Pattern = re.compile(r'<\|(markdown|code)(?:\|(\w*))?\|?>')

# 最长标记 "</|markdown|>" 为 13 字符，流式处理时保留末尾这么多以防截断
_BLOCK_LOOKBEHIND = 13


class _BlockFilter:
    """流式过滤结构化块标记，阻止代码/markdown 内容进入 TTS。"""

    def __init__(self):
        self._buf = ""
        self._depth = 0

    def feed(self, chunk: str) -> str:
        """喂入一个 chunk，返回可以去 TTS/分句的干净文本。"""
        self._buf += chunk
        out: list[str] = []
        i = 0

        while i < len(self._buf):
            if self._depth > 0:
                # 在块内 — 找结束标记
                end_md = self._buf.find("</|markdown|>", i)
                end_code = self._buf.find("</|code|>", i)
                candidates = []
                if end_md >= 0:
                    candidates.append(end_md)
                if end_code >= 0:
                    candidates.append(end_code)
                if candidates:
                    end_pos = min(candidates)
                    end_len = len("</|markdown|>") if self._buf.startswith("</|markdown|>", end_pos) else len("</|code|>")
                    i = end_pos + end_len
                    self._depth -= 1
                else:
                    # 没有结束标记 — 保留末尾以防截断
                    keep = min(len(self._buf) - i, _BLOCK_LOOKBEHIND)
                    i = len(self._buf) - keep
                    break
            else:
                # 不在块内 — 找开始标记
                m = _BLOCK_START_RE.search(self._buf, i)
                if m:
                    out.append(self._buf[i:m.start()])
                    i = m.end()
                    self._depth += 1
                else:
                    # 没有开始标记 — 安全地输出大部分，保留末尾防截断
                    safe = len(self._buf) - _BLOCK_LOOKBEHIND
                    if safe > i:
                        out.append(self._buf[i:safe])
                        i = safe
                    break

        self._buf = self._buf[i:]
        return "".join(out)

    def flush(self) -> str:
        """清空缓冲区，返回剩余的未被标记包裹的文本。"""
        result = self._buf
        self._buf = ""
        self._depth = 0
        # 去掉可能残留的未闭合标记
        cleaned = _BLOCK_START_RE.sub("", result)
        return cleaned


def _build_parts_with_blocks(
    acc_text: str, sentences: list[str],
) -> tuple[list[dict], dict[str, list[int]]]:
    """从 LLM 输出文本中解析结构化块标记，构建 parts 和 content_type。

    对话文本的句子作为 text parts，标记包裹的内容作为 markdown/code parts，
    追加在句子之后。
    """
    parts: list[dict] = [{"content": s} for s in sentences]
    content_type: dict[str, list[int]] = {"markdown": [], "code": []}

    if "<|markdown|>" not in acc_text and "<|code|" not in acc_text:
        return parts, content_type

    for m in _BLOCK_PATTERN.finditer(acc_text):
        md_content = m.group(1)
        if md_content is not None:
            content_type["markdown"].append(len(parts))
            parts.append({"content": md_content.strip()})
        else:
            lang = m.group(2) or ""
            code_content = m.group(3)
            content_type["code"].append(len(parts))
            p: dict = {"content": code_content.strip()}
            if lang:
                p["language"] = lang
            parts.append(p)

    return parts, content_type


@dataclass
class _MessageObj:
    role: str = "assistant"
    content: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


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
        self._tts = tts
        self._ctx = ctx
        self._sentence_buf = SentenceBuffer()
        self._process_lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None
        self._tts_tasks: list[asyncio.Task] = []
        self._tts_total: int = 0   # 全局计数器，跨轮次持续递增
        self._tts_seq: int = 0     # 本轮计数器，process() 开始时重置
        self._executing_tools: list[str] = []  # 当前正在执行中的工具名 (供 debug 面板)
        self._timing_history: list[dict] = []  # 最近 100 轮请求的计时数据 (供 debug 面板)
        self._pending_emojis: dict[int, list[str]] = {}  # TTS句N后面的emoji队列

    def cancel_current(self) -> bool:
        """取消当前认知任务（如有）。返回 True 表示确实取消了任务。"""
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            return True
        return False

    def set_tool_context(self, ctx) -> None:
        """设置当前 turn 的工具上下文。"""
        self._ctx = ctx

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

            self._tts_seq = 0
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

    async def _think_loop(self, user_message: str, user_name: str,
                          stt_emotion: str = "", stt_events: list[str] | None = None,
                          _on_tool_calls=None,
                          content: list[dict] | None = None) -> None:
        if self._emotion:
            if stt_emotion and stt_emotion != "NEUTRAL":
                self._emotion.analyze_user_stt(stt_emotion, stt_events)
            else:
                self._emotion.analyze_user(user_message)

        with self._clock.span("prompt") as t:
            messages = await self._prompt.build_messages(
                user_message=user_message, user_name=user_name,
                stt_emotion=stt_emotion, stt_events=stt_events,
                content=content,
            )
        user_content = content if content else [{"type": "text", "content": user_message}]
        self._session.add_message({
            "id": new_message_id(),
            "role": "user",
            "content": user_content,
            "timestamp": datetime.datetime.now().astimezone().isoformat(),
        })
        tool_defs = self._tool_executor.definitions() if self._tool_executor else None
        logger.debug("提示词构建: %d ms, %d 条消息, %d 个工具", t.elapsed_ms, len(messages), len(tool_defs or []))

        await self._output.typing(True)

        try:
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                acc_text = ""
                raw_tool_calls: list[dict] = []
                speaking_started = False
                all_sentences: list[str] = []
                block_filter = _BlockFilter()
                ttft_done = False
                t_think_start = time.perf_counter()

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
                                self._clock._spans["ttft"] = ttft_ms
                                logger.debug("TTFT: %d ms", ttft_ms)
                            acc_text += content

                            # 过滤结构化块标记 — 代码和 markdown 不走 TTS
                            clean = block_filter.feed(content)

                            # TTS 关闭时流式逐字发送；TTS 开启时不发（由 _start_tts_stream 发送完整句）
                            if not self._tts and clean:
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
                                if self._tts and not _is_emoji_only(sentence):
                                    self._start_tts_stream(sentence)
                                elif self._tts:
                                    self._pending_emojis.setdefault(
                                        self._tts_total, []).append(sentence)
                                else:
                                    await self._output._broadcast("on_text_delta", sentence,
                                        capability=ChannelCapability.TEXT_DELTA)
                                    await self._output.send("")

                        if tc_deltas:
                            self._merge_stream_tool_calls(raw_tool_calls, tc_deltas)
                except asyncio.CancelledError:
                    raise

                # Flush remaining text in sentence buffer + block filter
                trailing = block_filter.flush()
                if trailing:
                    self._sentence_buf.feed(trailing)
                remaining = self._sentence_buf.flush()
                if remaining:
                    if _is_emoji_only(remaining):
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
                    if self._tts and not _is_emoji_only(remaining):
                        self._start_tts_stream(remaining)
                    elif self._tts:
                        self._pending_emojis.setdefault(
                            self._tts_total, []).append(remaining)
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

                    normalized = self._normalize_tool_calls(raw_tool_calls)
                    assistant_entry: dict = {"role": "assistant", "content": acc_text or None}
                    if normalized:
                        assistant_entry["tool_calls"] = normalized
                    messages.append(assistant_entry)

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
                            "args": {k: str(v)[:80] for k, v in args.items()},
                        })

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
                        await self._output.action("tool_done", params={
                            "tool": tc["function"]["name"],
                            "call_id": tc.get("id", "unknown"),
                            "success": tool_result.success,
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
                for seq in sorted(self._pending_emojis):
                    for e in self._pending_emojis[seq]:
                        await self._output._broadcast("on_text_delta", e,
                            capability=ChannelCapability.TEXT_DELTA)
                        await self._output.send("")
                self._pending_emojis.clear()

                if acc_text.strip():
                    if self._emotion:
                        self._emotion.analyze_assistant(acc_text)
                    if not speaking_started:
                        # 无标点的极短回复，直接发完整消息
                        await self._output.typing(False)
                        msg = _MessageObj(content=acc_text)
                        await self._output.send(msg)
                if acc_text.strip():
                    parts, content_type = _build_parts_with_blocks(acc_text, all_sentences)
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
            for t in self._tts_tasks:
                if not t.done():
                    t.cancel()
            self._tts_tasks.clear()
            raise

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

    def _start_tts_stream(self, sentence: str) -> None:
        """后台 TTS task — 等首块音频到达后绑在一起发送文字+音频。

        文字和音频绑定发送，前端收到即可渲染+播放，无需任何同步逻辑。
        TTS workers=1 FIFO 保证句间顺序。
        """
        # 跳过空句 — 用 strip_all_markup 预处理后为空会导致 GPT-SoVits 返回 400
        _clean = strip_all_markup(sentence)
        if not _clean:
            return

        self._tts_total += 1
        self._tts_seq += 1
        sid = self._tts_total
        seq = self._tts_seq
        logger.info("TTS 句 %d (总#%d) 开始 (%d 字符)", seq, sid, len(sentence))
        logger.debug("TTS 句内容: %s", _clean[:60])

        async def _stream(_sid: int, _seq: int):
            if not self._tts:
                return
            try:
                char_name = self._prompt._character.name
                first_chunk = True
                t_tts = time.perf_counter()
                async for chunk in self._tts.synthesize_stream(sentence, character=char_name):
                    if first_chunk:
                        first_chunk = False
                        logger.info("TTS 句 %d (总#%d) 首块音频到达，发送文字+音频", _seq, _sid)
                        # 先发排在本句之前的 emoji
                        for e in self._pending_emojis.pop(_sid - 1, []):
                            await self._output._broadcast("on_text_delta", e,
                                capability=ChannelCapability.TEXT_DELTA, stream_id=_sid)
                            await self._output.send("")
                        # 发本句文字
                        await self._output._broadcast("on_text_delta", sentence,
                            capability=ChannelCapability.TEXT_DELTA, stream_id=_sid)
                        await self._output.send("")
                    await self._output.audio_chunk(chunk, stream_id=_sid)
                await self._output.audio_end(_sid)
                t_tts_elapsed = (time.perf_counter() - t_tts) * 1000
                logger.info("TTS 句 %d (总#%d) 播放完成 (%d ms)", _seq, _sid, int(t_tts_elapsed))
                # 发排在本句之后的 emoji
                for e in self._pending_emojis.pop(_sid, []):
                    await self._output._broadcast("on_text_delta", e,
                        capability=ChannelCapability.TEXT_DELTA, stream_id=_sid)
                    await self._output.send("")
            except asyncio.CancelledError:
                pass  # INTERRUPT 打断
            except Exception:
                logger.warning("TTS stream failed — fallback 发送文字", exc_info=True)
                # TTS 失败兜底: 至少把文字发出去
                if first_chunk:
                    try:
                        await self._output._broadcast("on_text_delta", sentence,
                            capability=ChannelCapability.TEXT_DELTA, stream_id=_sid)
                        await self._output.send("")
                    except Exception:
                        pass

        task = asyncio.create_task(_stream(sid, seq), name=f"tts-{sid}")
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

    async def _transition_idle(self) -> None:
        # 等待所有 TTS 任务完成（或被 interrupt 取消）
        pending = [t for t in self._tts_tasks if not t.done()]
        if pending:
            logger.debug("等待 %d 个 TTS 任务完成", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
            self._tts_tasks.clear()

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
        self._pending_emojis.clear()
        for t in self._tts_tasks:
            if not t.done():
                t.cancel()
        self._tts_tasks.clear()
        await self._output.typing(False)
        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(EventType.STATE_CHANGE, {"state": "interrupted", "request_id": get_request_id()}, priority=EventPriority.STATE_CHANGE)
        await asyncio.sleep(0.05)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(EventType.STATE_CHANGE, {"state": "idle", "request_id": get_request_id()}, priority=EventPriority.STATE_CHANGE)
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
