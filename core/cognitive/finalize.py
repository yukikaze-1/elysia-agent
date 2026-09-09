"""CognitiveFinalize mixin — 轮次收尾阶段。

CORE-MOD-P1-04：自 core/cognitive/loop.py 抽出。负责 mood fallback、公开回复
归档、结构化块推送、generation completed 终态与 after_response 触发。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from frontend_contract.ws_message import MessageChannel, SegmentKind
from core.schema.message import (
    AssistantMessage,
    CharacterMessageSnapshot,
    CharacterPresentationSnapshot,
)
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
    from core.character.runtime import CharacterRuntime

logger = logging.getLogger(__name__)


def _optional_snapshot_ref(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
    _current_runtime: "CharacterRuntime | None"  # M3（I1）：回合边界 flush 用（loop _prepare_turn 设置）
    _session_work_reserver: Callable[[SessionId], Awaitable["SessionMemory"]] | None
    _session_work_releaser: Callable[[SessionId], None] | None
    _pending_after_response: list[asyncio.Task]
    _after_response: Callable[..., Awaitable["asyncio.Task[None] | None"]]
    _transition_idle: Callable[..., Awaitable[None]]

    def _apply_final_mood_fallback(self, target: "SessionMemory",
                                    user_message: str) -> None:
        """LLM 未输出 <mood> 时关键词检测并设置 TTS 情绪。"""
        if not self._engine or self._mood_applied:
            return
        logger.debug("心情: <mood> 未检测到，使用关键词 fallback")
        self._engine.analyze_user(user_message, ctx=self._generation_ctx)
        # M3（Task 12，plan-defect 修正）：mood 读端迁移——读 self._engine.state
        # （实例权威）。此前回退读 target.character_state.mood（非权威——Task 10
        # 后 _apply_mood 写 engine.state），导致 LLM 未输出 <mood> 时读到 stale/default。
        fallback_mood = self._engine.state.mood if self._engine else None
        self._tts_mgr.set_emotion(fallback_mood)

    def _turn_character_snapshot(self) -> CharacterMessageSnapshot | None:
        """从本回合冻结 identity + 对应 runtime 构造归档审计快照。"""
        ctx = self._generation_ctx
        runtime = self._current_runtime
        if ctx is None or runtime is None:
            return None
        if not (
            ctx.character_id
            and ctx.character_instance_id
            and ctx.character_revision >= 1
        ):
            return None
        if (
            runtime.definition.characterId != ctx.character_id
            or runtime.instance.instanceId != ctx.character_instance_id
            or runtime.revision.revision != ctx.character_revision
        ):
            logger.error("回合角色 identity 与 runtime 不一致，拒绝写入错误审计快照")
            return None
        raw = runtime.revision.presentation
        presentation = raw if isinstance(raw, dict) else {}
        return CharacterMessageSnapshot(
            character_id=ctx.character_id,
            character_instance_id=ctx.character_instance_id,
            character_revision=ctx.character_revision,
            display_name=runtime.revision.displayName,
            presentation=CharacterPresentationSnapshot(
                avatar_asset_id=_optional_snapshot_ref(presentation.get("avatarAssetId")),
                live2d_model_id=_optional_snapshot_ref(presentation.get("live2dModelId")),
                theme_color=_optional_snapshot_ref(presentation.get("themeColor")),
            ),
        )

    async def _emit_final_public_text(self, public_text: str,
                                      speaking_started: bool) -> None:
        """公开文本的最终发射：personality 分析 + v2 segment / v1 message。"""
        if not public_text.strip():
            return
        if self._engine:
            self._engine.analyze_assistant(public_text, ctx=self._generation_ctx)
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
            session_id=target.session_id,
            character=self._turn_character_snapshot(),
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

        # Task 7（记忆 v2 隔离）：create_task 前急切读出 runtime——anti-race，与上方
        # target/self._engine 同构。per-turn 不重绑 self._consolidator（会跨 instance 写错）。
        runtime = self._current_runtime

        # BE-STAGEH-01：tail lease——主回合 lease 由 process() 在返回时释放，但
        # 记忆沉淀/性格反思/MemoryReflector/会话保存仍在后台 task 中运行。tail
        # task 在 finally 中释放这一份，使 busy 覆盖到「最后一次相关状态持久化」。
        if runtime is not None:
            runtime.begin_generation_tail()

        async def _run_after_response() -> None:
            try:
                reflect_task = await self._after_response(
                    user_message, public_text,
                    session=target, engine=self._engine,
                    consolidator=runtime.consolidator if runtime else None,
                    memory_reflector=runtime.reflector if runtime else None,
                )
                # 反思 task 由 engine.buffer_message 创建并登记在 tail registry。
                # 在此 await 它：否则「task 已创建、尚未持 _reflect_lock」的窗口里
                # reset 仍可插入（evolution_locks_busy 只看锁，看不到未启动的 task）。
                if reflect_task is not None:
                    try:
                        await reflect_task
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("性格反思任务异常", exc_info=True)
            finally:
                if runtime is not None:
                    runtime.end_generation()
                if background_reserved and self._session_work_releaser is not None:
                    self._session_work_releaser(target.session_id)

        try:
            task = asyncio.create_task(_run_after_response(), name="after-response")
        except Exception:
            if runtime is not None:
                runtime.end_generation()
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
        await self._flush_runtime_at_turn_boundary()
        await self._emit_state_change_if_needed(conversation_id)
        await self._transition_idle()

    async def _emit_state_change_if_needed(self, conversation_id: SessionId | None) -> None:
        """回合边界按 mood 变化发射 character.state.changed。

        发射点:flush 调完之后(stateVersion 已 CAS 递增,见 spec §3.3/§7.2)。
        仅 v2 + character_state_stream=true 且 mood 真变化时发射——不比较 updated_at
        (展示字段)/proactiveEnabled(immutable 常量)。

        [B2 修复] 必须 gate protocol_version >= 2(对齐 finalize.py:82 现有 turn_completed
        模式)——否则 v1 turn 也会进此广播,破坏 v1 wire 不变性。channels 用
        self._output._split_versions() 取 v2 通道(不广播到 v1)。
        [B1 修复] OutputManager 只有私有 _broadcast(无公开 broadcast),且是 async——
        需 await self._output._broadcast(...)。
        """
        ctx = self._generation_ctx
        if ctx is None or ctx.protocol_version < 2:
            return
        runtime = self._current_runtime
        if runtime is None:
            return
        try:
            view = runtime.public_state()
            if not runtime.has_public_state_changed(view):
                return
            runtime._last_public_state = view  # noqa: SLF001
            _v1, v2 = self._output._split_versions()  # noqa: SLF001 —— OutputManager 无公开 broadcast
            await self._output._broadcast(  # noqa: SLF001
                "on_character_state_changed",
                conversation_id=(
                    conversation_id
                    or ctx.conversation_id
                    or self._session.session_id
                ),
                character_id=runtime.definition.characterId,
                character_instance_id=runtime.instance.instanceId,
                character_revision=runtime.revision.revision,
                state_version=runtime.stateVersion,
                state=view,
                channels=v2,
            )
        except Exception:
            logger.debug("回合边界 state change 发射失败", exc_info=True)

    async def _flush_runtime_at_turn_boundary(self) -> None:
        """M3（I1 修复）：回合边界持久化 runtime 状态。

        spec §5.2：回合结束 runtime.flush() 把内存 CharacterRuntimeState 经
        CharacterInstanceRepository 写回 CharacterInstance.state（CAS state_version）。
        修复前 flush 仅发生在 shutdown（registry.close_all）——turn 结束后
        reflector 缓冲与 engine.state 变化从不落库，重启即丢。此处先同步
        reflector → runtime.state（engine.sync_reflector_to_runtime_state），再 flush
        当前 runtime（persist_fn 已由 _make_runtime 于 agent 侧接好）。

        防御：无 runtime / 无 engine 时静默跳过（直接构造 loop 的测试路径无 persist，
        不应抛）。flush 失败记日志，不中断 turn（持久化失败不应让用户对话挂掉）。
        """
        runtime = self._current_runtime
        if runtime is None:
            return
        try:
            # 阶段 F（§2.6/架构稿 §8.5）：MemoryReflector per-runtime 后计数器改经
            # runtime.state 持久化（session.save_state 不再承载，bind_memory_reflector 已移除）。
            # 回合边界先把 runtime.reflector 计数器 sync 进 runtime.state，再 flush 落库。
            if runtime.reflector is not None:
                mr_state = runtime.reflector.save_state()
                runtime.state.mr_round_count = mr_state.mr_round_count
                runtime.state.mr_new_memory_count = mr_state.mr_new_memory_count
                runtime.state.mr_last_summary = mr_state.mr_last_summary
            if self._engine is not None:
                self._engine.sync_reflector_to_runtime_state()
            await runtime.flush()
        except Exception:
            logger.debug("回合边界 runtime flush 失败", exc_info=True)
