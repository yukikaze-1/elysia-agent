"""CognitiveFinalize mixin — 轮次收尾阶段。

CORE-MOD-P1-04：自 core/cognitive/loop.py 抽出。负责 mood fallback、公开回复
归档、结构化块推送、generation completed 终态与 after_response 触发。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from frontend_contract.ws_message import MessageChannel, SegmentKind
from core.schema.message import AssistantMessage
from core.schema.token import TokenUsageSummary
from core.schema.types import SessionId
from core.text.parts import BlockRef, build_parts
from core.utils import log_conversation, new_message_id
from core.utils.text_utils import MessageObj

if TYPE_CHECKING:
    from Module.output import OutputManager
    from core.adapter.tts_adapter import TTSStreamManager
    from core.cognitive.generation import GenerationContext
    from core.cognitive.generation_registry import GenerationRegistry
    from core.debug.response_recorder import ResponseRecorder
    from core.memory.session import SessionMemory
    from core.monitoring.token_tracker import TokenTracker
    from core.personality.engine import CharacterEngine
    from core.prompt.prompt_assembler import PromptAssembler

logger = logging.getLogger(__name__)


class CognitiveFinalize:
    """轮次收尾阶段 mixin。"""

    # 类型占位：运行时由 CognitiveLoop.__init__ / configure() 初始化
    _engine: "CharacterEngine | None"
    _mood_applied: bool
    _tts_mgr: "TTSStreamManager"
    _output: "OutputManager"
    _generation_ctx: "GenerationContext | None"
    _generation_registry: "GenerationRegistry | None"
    _generation_completed: bool
    _token_tracker: "TokenTracker | None"
    _response_recorder: "ResponseRecorder | None"
    _prompt: "PromptAssembler"
    _session: "SessionMemory"
    _session_work_reserver: Callable[[SessionId], Awaitable["SessionMemory"]] | None
    _session_work_releaser: Callable[[SessionId], None] | None
    _pending_after_response: list[asyncio.Task]
    _after_response: Callable[..., Awaitable[None]]
    _transition_idle: Callable[..., Awaitable[None]]

    def _apply_final_mood_fallback(self, target: "SessionMemory",
                                    user_message: str) -> None:
        """LLM 未输出 <mood> 时关键词检测并设置 TTS 情绪。"""
        if not self._engine or self._mood_applied:
            return
        logger.debug("心情: <mood> 未检测到，使用关键词 fallback")
        self._engine.analyze_user(user_message)
        # M3（Task 12，plan-defect 修正）：mood 读端迁移——读 self._engine.state
        # （实例权威）。此前回退读 target.character_state.mood（非权威——Task 10
        # 后 _apply_mood 写 engine.state），导致 LLM 未输出 <mood> 时读到 stale/default。
        fallback_mood = self._engine.state.mood if self._engine else None
        self._tts_mgr.set_emotion(fallback_mood)

    async def _emit_final_public_text(self, public_text: str,
                                      speaking_started: bool) -> None:
        """公开文本的最终发射：personality 分析 + v2 segment / v1 message。"""
        if not public_text.strip():
            return
        if self._engine:
            self._engine.analyze_assistant(public_text)
        if speaking_started:
            return
        await self._output.typing(False)
        ctx = self._generation_ctx
        if ctx is not None and ctx.protocol_version >= 2:
            if ctx.current_segment_id is not None:
                await self._output.segment_completed(public_text, ctx)
                ctx.close_segment()
            else:
                ctx.open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)
                await self._output.segment_started(ctx)
                await self._output.segment_completed(public_text, ctx)
                ctx.close_segment()
        else:
            await self._output.send(MessageObj(content=public_text))

    async def _archive_final_turn(
        self,
        acc_text: str,
        all_sentences: list[str],
        blocks: list[BlockRef],
        iv_think_buf: str,
        public_text: str,
        user_message: str,
        round_num: int,
        target: "SessionMemory",
    ) -> None:
        """AssistantMessage 归档 + response recorder + after_response + 块推送。"""
        assistant_parts, content_type = build_parts(all_sentences, blocks)
        assistant_msg_id = (self._generation_ctx.assistant_message_id
                            if self._generation_ctx is not None else new_message_id())
        target.add_message(AssistantMessage(
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
            logger.debug("💭 %s", iv_think_buf.strip())
            log_conversation("inner_voice", self._prompt._character.name,
                             iv_think_buf.strip())

        background_reserved = False
        if self._session_work_reserver is not None:
            await self._session_work_reserver(target.session_id)
            background_reserved = True

        async def _run_after_response() -> None:
            try:
                await self._after_response(
                    user_message, public_text,
                    session=target, engine=self._engine)
            finally:
                if background_reserved and self._session_work_releaser is not None:
                    self._session_work_releaser(target.session_id)

        try:
            task = asyncio.create_task(_run_after_response(), name="after-response")
        except Exception:
            if background_reserved and self._session_work_releaser is not None:
                self._session_work_releaser(target.session_id)
            raise
        self._pending_after_response.append(task)
        self._pending_after_response = [
            t for t in self._pending_after_response if not t.done()
        ]

        for kind, content, lang, _after in blocks:
            params: dict = {"block_type": kind, "content": content.strip()}
            if kind == "code":
                params["language"] = lang
            await self._output.command("block", params=params)

    async def _mark_generation_completed(self) -> None:
        """generation registry 成功路径终态 completed。"""
        if self._generation_ctx is None:
            return
        self._generation_completed = True
        if self._generation_registry is None:
            return
        try:
            await self._generation_registry.mark(
                self._generation_ctx.generation_id,
                status="completed", finish_reason="stop")
        except Exception:
            logger.debug("generation mark completed 失败", exc_info=True)

    async def _emit_turn_completed(self, conversation_id: SessionId | None,
                                   target: "SessionMemory") -> None:
        """v2 generation.completed：usage/model/mood/conversationUsage 落位。"""
        if self._generation_ctx is None:
            return
        try:
            from frontend_contract import TokenRound, TokenUsage
            usages = (self._token_tracker.current_turn_usages
                      if self._token_tracker is not None else [])
            rounds = [TokenRound.model_validate(r.model_dump(mode="json")) for r in usages]
            usage = TokenUsage(rounds=rounds, turn_total=sum(r.total_tokens for r in usages))
            conversation_usage: dict | None = None
            if self._token_tracker is not None:
                try:
                    conversation_usage = await self._token_tracker.get_session_stats(
                        target.session_id)
                except Exception:
                    logger.debug("get_session_stats 失败，conversation_usage=None",
                                 exc_info=True)
            # M3（Task 12，plan-defect 修正）：mood 读端迁移——读 self._engine.state
            # （实例权威）。此前回退读 target.character_state.mood（非权威——Task 10
            # 后 _apply_mood 写 engine.state），导致 completed.mood 读到 stale/default（"平静"）。
            mood = (self._engine.state.mood
                    if self._engine is not None else None)
            await self._output.turn_completed(
                "stop", usage, self._generation_ctx.model_id, mood, self._generation_ctx,
                conversation_id=conversation_id or target.session_id,
                conversation_usage=conversation_usage)
        except Exception:
            logger.debug("turn_completed 发射失败", exc_info=True)

    async def _finalize_turn(
        self, acc_text: str, all_sentences: list[str],
        iv_think_buf: str, speaking_started: bool,
        user_message: str, round_num: int,
        blocks: list[BlockRef], public_text: str,
        *,
        conversation_id: SessionId | None = None,
        session: "SessionMemory | None" = None,
    ) -> None:
        """收尾本轮：mood fallback → 归档 session → 推送结构化块 → after_response。"""
        target = session if session is not None else self._session
        # 注意：不在此处 flush_emojis——最后一句 TTS 的 pop(sid-1) 在 wait_all 后才执行。

        self._apply_final_mood_fallback(target, user_message)
        if public_text.strip():
            await self._emit_final_public_text(public_text, speaking_started)
            await self._archive_final_turn(
                acc_text, all_sentences, blocks, iv_think_buf,
                public_text, user_message, round_num, target)
        await self._mark_generation_completed()
        await self._emit_turn_completed(conversation_id, target)
        await self._transition_idle()
