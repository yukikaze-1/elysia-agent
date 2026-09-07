"""OutputManager — 多通道广播 + capability 过滤 + 流式协调。

使用方式:
    mgr = OutputManager()
    mgr.add_channel(ConsoleChannel())
    mgr.add_channel(WebSocketChannel(ws))

    await mgr.typing(True)
    final_msg = await mgr.stream(llm.chat_stream(...), finalize=make_msg)
    await mgr.typing(False)
"""

import asyncio
import logging
from typing import Any, AsyncIterator, Callable

from Module.logger import get_caller_logger
from Module.output.protocol import ChannelCapability, OutputChannel
from frontend_contract.ws_message import MessageChannel, SegmentKind


class OutputManager:
    """输出管理器 — 多通道广播 + capability 过滤 + 流式协调。"""

    def __init__(self, *, logger: logging.Logger | None = None):
        """初始化输出管理器。

        Args:
            logger: 日志记录器，不传则自动检测调用模块。
        """
        self._logger = logger or get_caller_logger()
        self._channels: list[OutputChannel] = []

    # ---- Channel management ----

    def add_channel(self, channel: OutputChannel) -> None:
        """添加输出通道。

        Args:
            channel: 输出通道实例。
        """
        self._channels.append(channel)

    def remove_channel(self, channel: OutputChannel) -> None:
        """移除输出通道。

        Args:
            channel: 要移除的输出通道实例。
        """
        self._channels.remove(channel)

    # ---- Internal: filter channels ----

    def _filter(
        self,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> list[OutputChannel]:
        """按能力过滤通道列表。

        Args:
            channels: 待过滤的通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，为 None 则不过滤。否则只返回支持该能力的通道。

        Returns:
            过滤后的通道列表。
        """
        result = channels if channels is not None else self._channels
        if capability is not None:
            result = [c for c in result if capability in c.capabilities]
        return result

    async def _broadcast(self, method: str, *args: Any, channels: list[OutputChannel] | None = None,
                         capability: ChannelCapability | None = None, **kwargs: Any):
        """向筛选后的通道广播指定方法。单通道失败不影响其他通道。

        Args:
            method: 要调用的通道方法名。
            *args: 传给通道方法的位置参数。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，用于过滤通道。
            **kwargs: 传给通道方法的关键字参数。
        """
        targets = self._filter(channels=channels, capability=capability)
        if not targets:
            return
        results = await asyncio.gather(
            *(getattr(c, method)(*args, **kwargs) for c in targets),
            return_exceptions=True,
        )
        for c, result in zip(targets, results):
            if isinstance(result, Exception):
                self._logger.warning(
                    "broadcast %s to %s failed: %s", method, type(c).__name__, result
                )

    # ---- Public API ----

    async def stream(
        self,
        deltas: AsyncIterator[str],
        finalize: Callable[[str], object],
        *,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ):
        """流式输出 — 逐 delta 推送，完成后发送完整消息。

        Args:
            deltas: 流式文本增量迭代器。
            finalize: 接收累积的完整文本，返回 ChatMessage 等消息对象。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，用于过滤通道。

        Returns:
            finalize 返回的消息对象。
        """
        parts: list[str] = []
        async for delta in deltas:
            parts.append(delta)
            await self._broadcast(
                "on_text_delta", delta,
                channels=channels, capability=capability,
            )
        full_text = "".join(parts)
        msg = finalize(full_text)
        await self._broadcast(
            "on_message", msg,
            channels=channels, capability=capability,
        )
        return msg

    async def send(
        self,
        msg: Any,
        *,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        """非流式 — 直接发送完整消息。

        Args:
            msg: 消息对象（ChatMessage 或 str）。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，用于过滤通道。
        """
        await self._broadcast(
            "on_message", msg,
            channels=channels, capability=capability,
        )

    # ---- 便捷方法 — 自动按 capability 路由 ----

    async def typing(
        self,
        typing: bool,
        *,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        """广播输入状态。

        Args:
            typing: True 表示开始输入，False 表示输入结束。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，默认自动设为 TYPING。
        """
        await self._broadcast(
            "on_typing", typing,
            channels=channels, capability=capability or ChannelCapability.TYPING,
        )

    async def expression(
        self,
        expression: str,
        intensity: float = 1.0,
        *,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        """广播表情/情绪变更。

        Args:
            expression: 表情/情绪标识。
            intensity: 强度，0.0~1.0。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，默认自动设为 EXPRESSION。
        """
        await self._broadcast(
            "on_expression", expression, intensity=intensity,
            channels=channels, capability=capability or ChannelCapability.EXPRESSION,
        )

    async def action(
        self,
        action: str,
        *,
        params: dict | None = None,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        """广播动作。

        Args:
            action: 动作标识（如 wave, nod, surprised）。
            params: 可选的动作参数。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，默认自动设为 ACTION。
        """
        await self._broadcast(
            "on_action", action, params=params,
            channels=channels, capability=capability or ChannelCapability.ACTION,
        )

    async def live2d_directive(
        self,
        *,
        conversation_id: str | None = None,
        generation_id: str | None = None,
        character_id: str | None = None,
        character_instance_id: str | None = None,
        character_revision: int | None = None,
        expression: str | None = None,
        motion: str | None = None,
        state: str | None = None,
        priority: int | None = None,
        duration_ms: int | None = None,
        channels: list[OutputChannel] | None = None,
    ) -> None:
        """向 v2 通道广播带角色归属的 Live2D 低优先级建议。"""
        _v1, v2 = self._split_versions(channels)
        await self._broadcast(
            "on_live2d_directive",
            conversation_id=conversation_id,
            generation_id=generation_id,
            character_id=character_id,
            character_instance_id=character_instance_id,
            character_revision=character_revision,
            expression=expression,
            motion=motion,
            state=state,
            priority=priority,
            duration_ms=duration_ms,
            channels=v2,
            capability=ChannelCapability.EXPRESSION,
        )

    async def tool_started(
        self, *, call_id: str, tool_name: str, runtime: str,
        v1_args: dict, input_summary: str | None,
        generation_id: str | None = None, conversation_id: str | None = None,
        v1_suppress: bool = False,
        channels: list[OutputChannel] | None = None,
    ) -> None:
        """工具开始：v1 发 action{tool_executing}（wire 逐字节不变）；v2 发 tool.started（D6-02）。

        v1_suppress=True：跳过 v1 action{tool_executing}——submit_task 场景 v1 从未发过，
        加帧会破坏「v1 帧集不变」（spec Critical 2 修正）。
        """
        v1, v2 = self._split_versions(channels)
        if v1 and not v1_suppress:
            await self._broadcast(
                "on_action", "tool_executing",
                params={"tool": tool_name, "call_id": call_id, "args": v1_args},
                channels=v1, capability=ChannelCapability.ACTION,
            )
        if v2:
            await self._broadcast(
                "on_tool_started",
                call_id=call_id, tool_name=tool_name, runtime=runtime,
                input_summary=input_summary,
                generation_id=generation_id, conversation_id=conversation_id,
                channels=v2,
            )

    async def tool_completed(
        self, *, call_id: str, tool_name: str, runtime: str,
        success: bool, v1_content: str, output_summary: str | None,
        v1_truncated: bool, output_truncated: bool,
        error_v1: str | None, error_v2: dict | None, error_type: str | None,
        duration_ms: float | None,
        generation_id: str | None = None, conversation_id: str | None = None,
        channels: list[OutputChannel] | None = None,
    ) -> None:
        """工具完成：v1 发 action{tool_done}（wire 逐字节不变）；v2 发 tool.completed（D6-02）。

        error_v1（v1 tool_done.error，str）与 error_v2（v2 tool.completed.error，dict
        {code,message}，D-ERROR）语义独立，不得互相塞错类型。
        """
        v1, v2 = self._split_versions(channels)
        if v1:
            await self._broadcast(
                "on_action", "tool_done",
                params={"tool": tool_name, "call_id": call_id, "success": success,
                        "content": v1_content, "error": error_v1, "error_type": error_type,
                        "duration_ms": duration_ms, "truncated": v1_truncated},
                channels=v1, capability=ChannelCapability.ACTION,
            )
        if v2:
            await self._broadcast(
                "on_tool_completed",
                call_id=call_id, tool_name=tool_name, runtime=runtime,
                success=success, output_summary=output_summary,
                truncated=output_truncated, error=error_v2, error_type=error_type,
                duration_ms=duration_ms, generation_id=generation_id,
                conversation_id=conversation_id,
                channels=v2,
            )

    async def command(
        self,
        cmd: str,
        *,
        params: dict | None = None,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        """广播前端指令。

        Args:
            cmd: 指令标识（如换背景、播放特效、弹出图片）。
            params: 可选的指令参数。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，默认自动设为 COMMAND。
        """
        await self._broadcast(
            "on_command", cmd, params=params,
            channels=channels, capability=capability or ChannelCapability.COMMAND,
        )

    async def token_usage(
        self,
        data: dict,
        *,
        channels: list[OutputChannel] | None = None,
    ) -> None:
        """广播 token 消耗统计（前端实时更新用量）。

        批次 2：仅 v1 通道——v2 的 usage 由 generation.completed.usage 承载（spec §5.1 抑制表）。

        Args:
            data: token 消耗统计数据字典。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
        """
        v1, _v2 = self._split_versions(channels)
        await self._broadcast(
            "on_token_usage", data,
            channels=v1,
        )

    async def audio_chunk(
        self,
        chunk: bytes,
        *,
        stream_id: int = 0,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        """广播音频流 chunk。

        Args:
            chunk: 音频字节数据。
            stream_id: 流标识，用于区分并发音频流。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，默认自动设为 AUDIO。
        """
        await self._broadcast(
            "on_audio_chunk", chunk, stream_id=stream_id,
            channels=channels, capability=capability or ChannelCapability.AUDIO,
        )

    async def audio_end(
        self,
        stream_id: int = 0,
        *,
        format: str = "wav",
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        """广播音频流结束标记。

        Args:
            stream_id: 流标识，用于区分并发音频流。
            format: 音频格式，'wav' 或 'mp3'。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
            capability: 能力标记，默认自动设为 AUDIO。
        """
        await self._broadcast(
            "on_audio_end", stream_id, format=format,
            channels=channels, capability=capability or ChannelCapability.AUDIO,
        )

    async def audio_start(self, stream_id: int, *, format: str = "wav",
                          sample_rate: int | None = None, duration_ms: int | None = None,
                          channels: list[OutputChannel] | None = None,
                          capability: ChannelCapability | None = None) -> None:
        """广播音频流开始（stream_id = playback ID，BE-063）。"""
        await self._broadcast(
            "on_audio_start", stream_id, format=format, sample_rate=sample_rate,
            duration_ms=duration_ms, channels=channels,
            capability=capability or ChannelCapability.AUDIO)

    async def audio_cancelled(self, stream_id: int, *,
                              channels: list[OutputChannel] | None = None,
                              capability: ChannelCapability | None = None) -> None:
        """广播音频流被取消（BE-063）。"""
        await self._broadcast(
            "on_audio_cancelled", stream_id, channels=channels,
            capability=capability or ChannelCapability.AUDIO)

    async def notify_state(self, state: str) -> None:
        """通知所有通道 FSM 状态变更（前端据此切换动画）。

        Args:
            state: FSM 状态名。
        """
        for ch in self._channels:
            try:
                await ch.on_state(state)
            except Exception:
                self._logger.warning(
                    "Channel %s on_state(%s) failed",
                    type(ch).__name__, state, exc_info=True,
                )

    # ---- 批次 2 v2 版本感知统一回合事件（单一语义源，双 wire 呈现） ----
    # 批次 3：8 个统一方法全部透传 ctx.conversation_id 到 v2 的 on_generation_*
    # （conversationId），turn_completed 另透传 conversation_usage（conversationUsage）；
    # v1 通道不接收 generation.*，wire 保持字节不变。

    def _split_versions(self, channels: list[OutputChannel] | None = None):
        """按协议版本拆分通道：v1（protocol_version=1）与 v2（>=2）。

        Returns:
            (v1_channels, v2_channels)
        """
        channels = channels if channels is not None else self._channels
        v1 = [c for c in channels if getattr(c, "protocol_version", 1) == 1]
        v2 = [c for c in channels if getattr(c, "protocol_version", 1) >= 2]
        return v1, v2

    async def turn_started(self, ctx: Any, *, channels: list[OutputChannel] | None = None) -> None:
        """生成开始：v2 发 generation.started；v1 无新增（typing/state 由现有路径）。

        Args:
            ctx: GenerationContext（含 generation_id / assistant_message_id /
                conversation_id——批次 3 v2 conversationId 数据源；
                model_id——批次 5 D8 模型快照，Started.model 数据源；
                request_id——批次 8c F4 关联请求 ID，Started.requestId 数据源）。
            channels: 候选通道，None 用全部。
        """
        _v1, v2 = self._split_versions(channels)
        await self._broadcast(
            "on_generation_started",
            generation_id=ctx.generation_id, message_id=ctx.assistant_message_id,
            conversation_id=ctx.conversation_id,
            request_id=ctx.request_id,
            model=ctx.model_id or None,
            # M3（I2）：generation 审计字段贯通到 wire（None 时省略）。
            character_id=ctx.character_id or None,
            character_instance_id=ctx.character_instance_id or None,
            character_revision=ctx.character_revision or None,
            channels=v2,
        )

    async def segment_started(self, ctx: Any, *, channels: list[OutputChannel] | None = None) -> None:
        """分段开始：v2 发 generation.segment.started；v1 无。

        Args:
            ctx: GenerationContext（当前分段已由 loop 打开；conversation_id 透传 v2）。
        """
        _v1, v2 = self._split_versions(channels)
        await self._broadcast(
            "on_generation_segment_started",
            generation_id=ctx.generation_id, segment_id=ctx.current_segment_id,
            segment_index=ctx.current_segment_index, channel=ctx.channel, kind=ctx.kind,
            conversation_id=ctx.conversation_id,
            channels=v2,
        )

    async def text_delta(self, text: str, ctx: Any, *, channels: list[OutputChannel] | None = None) -> None:
        """文本增量：v1 发 delta；v2 发 generation.delta（deltaSequence 由 ctx 分配）。

        Args:
            text: 文本增量。
            ctx: GenerationContext。
        """
        v1, v2 = self._split_versions(channels)
        await self._broadcast("on_text_delta", text,
                              channels=v1, capability=ChannelCapability.TEXT_DELTA)
        await self._broadcast(
            "on_generation_delta",
            generation_id=ctx.generation_id, segment_id=ctx.current_segment_id,
            segment_index=ctx.current_segment_index, delta_sequence=ctx.next_delta(),
            delta=text,
            conversation_id=ctx.conversation_id,
            channels=v2,
        )

    async def segment_completed(self, text: str, ctx: Any, *, tts_active: bool = False,
                                channels: list[OutputChannel] | None = None) -> None:
        """分段完成：v1 按 TTS 状态发 send("") 或不变；v2 仅活动段发 generation.segment.completed。

        ctx 无活动段（current_segment_id None）时仍发 v1 句子边界，但 v2 不发——
        避免空 message 噪音泄漏到 v2（v2 段语义由活动段承载）。

        Args:
            text: 完整段文本。
            ctx: GenerationContext。
            tts_active: TTS 是否活跃（v1 呈现据此决定是否发空 message 句子边界）。
        """
        v1, v2 = self._split_versions(channels)
        if not tts_active:
            await self._broadcast("on_message", "",
                                  channels=v1, capability=ChannelCapability.TEXT_MESSAGE)
        if ctx.current_segment_id is not None:
            await self._broadcast(
                "on_generation_segment_completed",
                generation_id=ctx.generation_id, segment_id=ctx.current_segment_id,
                segment_index=ctx.current_segment_index, channel=ctx.channel, kind=ctx.kind,
                content=text,
                conversation_id=ctx.conversation_id,
                channels=v2,
            )

    async def inner_voice(self, text: str, ctx: Any, *, channels: list[OutputChannel] | None = None) -> None:
        """Inner Voice：v1 发 command inner_voice；v2 作为 inner_voice 段（started+completed，含文本）。

        Args:
            text: inner_voice 文本。
            ctx: GenerationContext（方法内部打开 inner_voice 段并关闭）。
        """
        v1, v2 = self._split_versions(channels)
        await self._broadcast("on_command", "inner_voice", params={"content": text},
                              channels=v1, capability=ChannelCapability.COMMAND)
        ctx.open_segment(MessageChannel.INNER_VOICE, SegmentKind.TEXT)
        sid = ctx.current_segment_id
        idx = ctx.current_segment_index
        await self._broadcast(
            "on_generation_segment_started",
            generation_id=ctx.generation_id, segment_id=sid, segment_index=idx,
            channel=ctx.channel, kind=ctx.kind,
            conversation_id=ctx.conversation_id,
            channels=v2,
        )
        await self._broadcast(
            "on_generation_segment_completed",
            generation_id=ctx.generation_id, segment_id=sid, segment_index=idx,
            channel=ctx.channel, kind=ctx.kind, content=text,
            conversation_id=ctx.conversation_id,
            channels=v2,
        )
        ctx.close_segment()

    async def mood(self, expression: str, intensity: float = 1.0,
                   *, channels: list[OutputChannel] | None = None) -> None:
        """心情：v1 发 expression；v2 捕获不发（mood 由 turn_completed 落位）。

        Args:
            expression: 心情标识。
            intensity: 强度 0.0~1.0。
        """
        v1, _v2 = self._split_versions(channels)
        await self._broadcast("on_expression", expression, intensity=intensity,
                              channels=v1, capability=ChannelCapability.EXPRESSION)

    async def turn_completed(self, finish_reason: str, usage: Any, model: str, mood: str | None,
                             ctx: Any, *, channels: list[OutputChannel] | None = None,
                             conversation_id: str | None = None,
                             conversation_usage: dict | None = None) -> None:
        """父完成：v1 NO-OP（TokenTracker.record 已实时广播 v1 token_usage，防双发）；
        v2 发 generation.completed。

        Args:
            finish_reason: stop/tool_calls。
            usage: 契约 TokenUsage（rounds + turn_total）。
            model: 实际模型。
            mood: 心情。
            ctx: GenerationContext。
            conversation_id: 批次 3 目标会话 ID（v2 conversationId）。
            conversation_usage: 批次 3 会话级 token 累计 dict {total, by_model}
                （来自 get_session_stats，v2 填 conversationUsage）。
        """
        _v1, v2 = self._split_versions(channels)
        await self._broadcast(
            "on_generation_completed",
            generation_id=ctx.generation_id, message_id=ctx.assistant_message_id,
            finish_reason=finish_reason, usage=usage, model=model, mood=mood,
            conversation_id=conversation_id, conversation_usage=conversation_usage,
            # M3（I2）：generation 审计字段贯通到 wire（None 时省略）。
            character_id=ctx.character_id or None,
            character_instance_id=ctx.character_instance_id or None,
            character_revision=ctx.character_revision or None,
            channels=v2,
        )

    async def turn_failed(self, code: str, message: str, retryable: bool, label: str,
                          ctx: Any, *, channels: list[OutputChannel] | None = None) -> None:
        """父失败：v1 逐字节复刻现状（\n❌ {label}: {msg[:200]} + send("")）；
        v2 发 generation.failed。

        Args:
            code: 稳定错误码。
            message: 公开说明。
            retryable: 是否可重试。
            label: v1 错误标签（LLM 调用失败/内部错误）。
            ctx: GenerationContext（model_id——批次 5 D8 模型快照，failed 帧 model 数据源）。
        """
        v1, v2 = self._split_versions(channels)
        await self._broadcast(
            "on_text_delta", f"\n❌ {label}: {message[:200]}",
            channels=v1, capability=ChannelCapability.TEXT_DELTA,
        )
        await self._broadcast("on_message", "",
                              channels=v1, capability=ChannelCapability.TEXT_MESSAGE)
        await self._broadcast(
            "on_generation_failed",
            generation_id=ctx.generation_id, message_id=ctx.assistant_message_id,
            code=code, message=message, retryable=retryable,
            model=ctx.model_id or None,
            conversation_id=ctx.conversation_id,
            # M3（I2）：generation 审计字段贯通到 wire（None 时省略）。
            character_id=ctx.character_id or None,
            character_instance_id=ctx.character_instance_id or None,
            character_revision=ctx.character_revision or None,
            channels=v2,
        )

    async def turn_cancelled(self, ctx: Any, *, channels: list[OutputChannel] | None = None) -> None:
        """取消（用户打断）：v2 发 generation.failed{CANCELLED}；v1 不发——
        现状打断无 ❌ delta（state interrupted 由 FSM 承担），保持 v1 兼容。

        Args:
            ctx: GenerationContext（model_id——批次 5 D8 模型快照，cancelled 帧 model 数据源）。
        """
        _v1, v2 = self._split_versions(channels)
        await self._broadcast(
            "on_generation_failed",
            generation_id=ctx.generation_id, message_id=ctx.assistant_message_id,
            code="GENERATION_CANCELLED", message="已取消", retryable=False,
            model=ctx.model_id or None,
            conversation_id=ctx.conversation_id,
            # M3（I2）：generation 审计字段贯通到 wire（None 时省略）。
            character_id=ctx.character_id or None,
            character_instance_id=ctx.character_instance_id or None,
            character_revision=ctx.character_revision or None,
            channels=v2,
        )

    async def close(self) -> None:
        """关闭所有通道。"""
        tasks = [c.close() for c in self._channels]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for channel, result in zip(self._channels, results):
            if isinstance(result, Exception):
                self._logger.warning(
                    "Failed to close channel %s: %s",
                    type(channel).__name__, result,
                )
