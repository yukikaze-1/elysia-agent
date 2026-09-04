"""CognitiveStreaming mixin — LLM 流式消费与 StreamProcessor 事件分发。

CORE-MOD-P1-04：自 core/cognitive/loop.py 抽出。包含
``_stream_llm_response``（chunk 消费 + tool_call delta 累积）与
``_dispatch_stream_event``（StreamEvent → mood/TTS/output/generation 副作用）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from Module.output import ChannelCapability, OutputManager
from frontend_contract.ws_message import MessageChannel, SegmentKind
from core.adapter.tool_call_parser import merge_stream_tool_calls
from core.adapter.tts_adapter import TTSStreamManager
from core.cognitive.generation import GenerationContext
from core.cognitive.timing import TimingClock
from core.schema.types import SessionId
from core.text.parts import BlockRef
from core.text.stream_processor import StreamEvent, StreamEventType, StreamProcessor

if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.debug.prompt_recorder import PromptRecorder
    from core.memory.session import SessionMemory
    from core.monitoring.token_tracker import TokenTracker
    from core.personality.engine import CharacterEngine
    from core.status.fsm import FSMBackedPhaseController

logger = logging.getLogger(__name__)


async def _apply_mood(raw: str, engine: "CharacterEngine", tts_mgr: TTSStreamManager,
                      output: OutputManager, target_session: "SessionMemory | None" = None) -> None:
    """解析 <mood> 标签内容，更新 CharacterState + 统一心情发射 + TTS emotion。

    支持格式: "情绪文本" 或 "情绪文本|强度"（强度 0.1–1.0）。
    更新目标会话 character_state 的 mood / mood_intensity（而非 engine.state），
    经 output.mood() 统一发射（v1 发 expression、v2 捕获不发——mood 由 turn_completed 落位），
    设置 TTS 情绪参数。
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

    state = target_session.character_state if target_session is not None else engine.state
    prev = state.mood
    state.mood = mood_text
    state.mood_intensity = intensity
    await output.mood(mood_text, intensity)
    tts_mgr.set_emotion(mood_text)
    if prev != mood_text:
        logger.info("心情: %s → %s (%.0f%%)", prev, mood_text, intensity * 100)
    else:
        logger.info("心情: %s (%.0f%%)", mood_text, intensity * 100)


class CognitiveStreaming:
    """流式阶段 mixin：认知循环中所有逐 chunk 副作用收口在此。"""

    # 类型占位：运行时由 CognitiveLoop.__init__ 初始化
    _llm: "LLMAdapter"
    _prompt_recorder: "PromptRecorder | None"
    _token_tracker: "TokenTracker | None"
    _mood_applied: bool
    _ttft_start: float
    _engine: "CharacterEngine | None"
    _tts_mgr: TTSStreamManager
    _output: OutputManager
    _generation_ctx: GenerationContext | None
    _clock: TimingClock | None
    phases: "FSMBackedPhaseController"

    async def _stream_llm_response(
        self, messages: list[dict], tool_defs: list[dict], round_num: int,
        session_id: SessionId | None = None,
        session: "SessionMemory | None" = None,
    ) -> tuple[str, list[dict], bool, list[str], str, list[BlockRef], str]:
        """流式调用 LLM，逐 chunk 分发 StreamProcessor 事件。

        驱动 StreamProcessor 状态机，将解析出的文本增量转为 TTS/UI 副作用。
        同时累积 tool_call delta 供后续 normalize。flush() 后在本方法内重建
        blocks 与 public_text（proc 是局部变量，`_finalize_turn` 只收返回值）。

        Args:
            messages: 已构建的 LLM 消息列表（含 system + history + user）
            tool_defs: 工具定义列表（OpenAI function calling 格式）
            round_num: 当前工具调用轮次（0 = 首轮）
            session_id: 本轮目标会话 ID（token record 归属）
            session: 本轮目标会话（F1-b 捕获——_apply_mood 写 target.character_state）

        Returns:
            (acc_text, raw_tool_calls, speaking_started, all_sentences,
             inner_voice, blocks, public_text)
        """
        acc_text = ""
        raw_tool_calls: list[dict] = []
        speaking_started = False
        all_sentences: list[str] = []
        iv_think_buf_holder: list[str] = []
        t_think_start = time.perf_counter()
        self._ttft_start = t_think_start

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
                                session=session,
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
                            session_id=session_id,
                        )
        except asyncio.CancelledError:
            raise

        for event in proc.flush():
            speaking_started, all_sentences = (
                await self._dispatch_stream_event(
                    event,
                    speaking_started=speaking_started,
                    all_sentences=all_sentences,
                    iv_think_buf_holder=iv_think_buf_holder,
                    session=session,
                )
            )

        blocks = proc.get_blocks()
        public_text = proc.rebuild_public_text()
        iv_think = "\n".join(iv_think_buf_holder)

        return acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think, blocks, public_text

    async def _handle_sentence_event(
        self,
        sentence: str,
        speaking_started: bool,
        all_sentences: list[str],
    ) -> tuple[bool, list[str]]:
        """SENTENCE 事件：TTS 入队 / v2 segment 完成 / v1 气泡边界。"""
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
                await self._output.segment_completed(sentence, ctx, tts_active=tts_active)
                ctx.close_segment()
            else:
                ctx.open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)
                await self._output.segment_started(ctx)
                await self._output.segment_completed(sentence, ctx, tts_active=tts_active)
                ctx.close_segment()
        elif not tts_active:
            await self._output.send("")
        return speaking_started, all_sentences

    async def _handle_text_delta_event(self, text: str) -> None:
        """TEXT_DELTA 事件：v2 开段/增量，v1 纯文本广播。"""
        if not text:
            return
        tts_active = self._tts_mgr.active and self._tts_mgr.enabled
        ctx = self._generation_ctx
        if ctx is not None:
            if ctx.current_segment_id is None:
                ctx.open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)
                await self._output.segment_started(ctx)
            if not tts_active:
                await self._output.text_delta(text, ctx)
        elif not tts_active:
            await self._output._broadcast(
                "on_text_delta", text,
                capability=ChannelCapability.TEXT_DELTA,
            )

    async def _dispatch_stream_event(
        self, event: StreamEvent, *,
        speaking_started: bool, all_sentences: list[str],
        iv_think_buf_holder: list[str],
        session: "SessionMemory | None" = None,
    ) -> tuple[bool, list[str]]:
        """分发 StreamEvent → 副作用。

        这是 StreamProcessor（纯状态机）和 CognitiveLoop（副作用世界）之间的桥。
        按事件类型分发：mood → CharacterEngine, inner_voice → UI command,
        ttft → clock span, sentence → TTS/UI, text_delta → 纯文本模式输出。
        """
        if event.type == StreamEventType.MOOD:
            if self._engine:
                await _apply_mood(event.text, self._engine, self._tts_mgr,
                                  self._output, session)
            self._mood_applied = True

        elif event.type == StreamEventType.INNER_VOICE:
            if event.text:
                iv_think_buf_holder.append(event.text)
                try:
                    if self._generation_ctx is not None:
                        await self._output.inner_voice(event.text, self._generation_ctx)
                    else:
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
            speaking_started, all_sentences = await self._handle_sentence_event(
                event.text, speaking_started, all_sentences)

        elif event.type == StreamEventType.TEXT_DELTA:
            await self._handle_text_delta_event(event.text)

        return speaking_started, all_sentences
