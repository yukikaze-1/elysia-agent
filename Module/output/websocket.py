"""WebSocketChannel — JSON 消息推送。

wire format:
    {"type": "delta", "content": "..."}        — 流式 token
    {"type": "typing", "typing": true/false}    — 输入状态
    {"type": "expression", "expression": "...", "intensity": 0.8}
    {"type": "action", "action": "wave", ...}
    {"type": "command", "command": "...", ...}
    {"type": "message", ...msg.to_dict()...}    — 完整消息
    {"type": "token_usage", "turn": {...}, "session": {...}}  — Token 消耗统计
"""

import logging

from Module.logger import get_caller_logger
from Module.output.protocol import ChannelCapability, OutputChannel
from frontend_contract import (
    AudioCancelledMessage,
    AudioEndMessage,
    AudioMessage,
    AudioStartMessage,
    CommandMessage,
    ConversationUsage,
    DeltaMessage,
    ExpressionMessage,
    GenerationCompletedMessage,
    GenerationDeltaMessage,
    GenerationFailedMessage,
    GenerationSegmentCompletedMessage,
    GenerationSegmentStartedMessage,
    GenerationStartedMessage,
    MessageMessage,
    StateMessage,
    TypingMessage,
    UserAckMessage,
)


class WebSocketChannel(OutputChannel):
    """WebSocket 输出通道 — 包装单个连接。连接生命周期由外部管理。"""

    def __init__(self, ws, *, logger: logging.Logger | None = None,
                 protocol_version: int = 1, client_platform: str = ""):
        """初始化 WebSocket 输出通道。

        Args:
            ws: aiohttp WebSocket 连接对象。
            logger: 日志记录器，不传则自动检测调用模块。
            protocol_version: 协商的协议版本（1=v1 默认，2=v2，握手时注入）。
            client_platform: 客户端平台标识（批次 4，spec §5.1 D-l）：握手
                `X-Client-Platform` 头解析，v2 + electron 连接为 "electron"——
                Desktop 工具投递（bridge._deliver）与 desktop 语义的定向依据；
                v1 连接恒 ""（忽略 platform 头）。
        """
        self._ws = ws
        self._logger = logger or get_caller_logger()
        self._audio_seq: dict[int, int] = {}
        self.protocol_version = protocol_version
        self.client_platform = client_platform
        # 批次 8a（D2）：每 v2 连接的服务端分配稳定连接身份（`conn_<hex>`），
        # 握手时由 ws_handler 注入；v1 连接恒 None。tool.status 路由以此校验
        # 发送连接是投递目标（防伪造结果）。
        self.connection_id: str | None = None

    @property
    def capabilities(self) -> set[ChannelCapability]:
        return {
            ChannelCapability.TEXT_DELTA,
            ChannelCapability.TEXT_MESSAGE,
            ChannelCapability.TYPING,
            ChannelCapability.EXPRESSION,
            ChannelCapability.ACTION,
            ChannelCapability.COMMAND,
            ChannelCapability.AUDIO,
        }

    async def _send_json(self, data: dict) -> None:
        """发送 JSON 到 WebSocket 客户端，发送失败仅记录警告。

        Args:
            data: 要发送的 JSON 可序列化字典。
        """
        try:
            await self._ws.send_json(data)
        except Exception:
            self._logger.warning("WebSocket send failed", exc_info=True)

    async def on_typing(self, typing: bool) -> None:
        """发送输入状态。

        Args:
            typing: True 表示开始输入，False 表示输入结束。
        """
        await self._send_json(TypingMessage(typing=typing).wire_dict())

    async def on_text_delta(self, delta: str, *, stream_id: int | None = None) -> None:
        """发送流式文本增量。

        Args:
            delta: 单个文本增量（token）。
            stream_id: 可选的流标识。
        """
        # 契约：stream_id None → 缺省省略（X?）
        await self._send_json(DeltaMessage(content=delta, stream_id=stream_id).wire_dict())

    async def on_expression(self, expression: str, intensity: float = 1.0) -> None:
        """发送表情/情绪变更。

        Args:
            expression: 表情/情绪标识。
            intensity: 强度，0.0~1.0。
        """
        await self._send_json(ExpressionMessage(
            expression=expression, intensity=intensity,
        ).wire_dict())

    async def on_action(self, action: str, *, params: dict | None = None) -> None:
        """发送动作。

        Args:
            action: 动作标识（如 wave, nod, surprised）。
            params: 可选的动作参数。
        """
        # 注：暂不切 ActionMessage——其 params 非 Optional（default_factory={}），
        # 转换后空参数会多发 "params": {}（当前省略），破坏行为等价。
        payload: dict[str, object] = {"type": "action", "action": action}
        if params:
            payload["params"] = params
        await self._send_json(payload)

    async def on_command(self, cmd: str, *, params: dict | None = None) -> None:
        """发送前端指令。

        Args:
            cmd: 指令标识（如换背景、播放特效、弹出图片）。
            params: 可选的指令参数。
        """
        # 契约：params None → 缺省省略（X?）
        await self._send_json(CommandMessage(command=cmd, params=params).wire_dict())

    async def on_audio_chunk(self, chunk: bytes, *, stream_id: int = 0) -> None:
        """发送音频流 chunk（base64 编码）。

        Args:
            chunk: 音频字节数据。
            stream_id: 流标识，用于区分并发音频流。
        """
        # 批次 6：切 AudioMessage 建模（is_final=False 显式声明，wire 行为不变——
        # wire_dict exclude_none 下 False 仍输出；stream_id/seq/data 键与旧手动 dict 一致）
        import base64
        seq = self._audio_seq.get(stream_id, 0)
        self._audio_seq[stream_id] = seq + 1
        await self._send_json(AudioMessage(
            stream_id=stream_id, seq=seq,
            data=base64.b64encode(chunk).decode("ascii"), is_final=False).wire_dict())

    async def on_audio_start(self, stream_id: int, *, format: str = "wav",
                             sample_rate: int | None = None,
                             duration_ms: int | None = None) -> None:
        """音频流开始（stream_id = playback ID）。"""
        await self._send_json(AudioStartMessage(
            stream_id=stream_id, format=format, sampleRate=sample_rate,
            durationMs=duration_ms).wire_dict())

    async def on_audio_cancelled(self, stream_id: int) -> None:
        """音频流被取消——清理 _audio_seq 残留 + 通知前端清理孤儿 chunk。"""
        self._audio_seq.pop(stream_id, None)
        await self._send_json(AudioCancelledMessage(stream_id=stream_id).wire_dict())

    async def on_audio_end(self, stream_id: int = 0, *, format: str = "wav") -> None:
        """发送音频流结束标记。

        Args:
            stream_id: 流标识，用于区分并发音频流。
            format: 音频格式，'wav' 或 'mp3'。
        """
        self._audio_seq.pop(stream_id, None)
        await self._send_json(AudioEndMessage(
            stream_id=stream_id, format=format,
        ).wire_dict())

    async def on_state(self, state: str) -> None:
        """发送 FSM 状态变更。

        Args:
            state: FSM 状态名。
        """
        await self._send_json(StateMessage(state=state).wire_dict())

    async def on_token_usage(self, data: dict) -> None:
        """发送 token 消耗统计。

        Args:
            data: token 消耗统计数据字典。
        """
        # 注：暂不切 TokenUsageMessage——core TokenUsage dump 中 cached_tokens/
        # reasoning_tokens 为 None 时仍带键（null），转换经 wire_dict(exclude_none)
        # 会缺省省略（wire 行为变化）；session_id 已入 TokenRound（I1' 已收口），
        # 仅剩 null 省略问题未决。data 本身已是模型 dump，非手写字典。
        await self._send_json({"type": "token_usage", **data})

    # ── 批次 2 v2 结构化事件（wire generation.* / user_message.ack） ──

    async def on_user_message_ack(self, *, message_id: str, client_request_id: str,
                                  client_message_id: str | None = None,
                                  conversation_id: str | None = None,
                                  received_at: str, status: str,
                                  error: dict | None = None) -> None:
        """发送 v2 用户消息确认。"""
        await self._send_json(UserAckMessage(
            messageId=message_id, clientRequestId=client_request_id,
            clientMessageId=client_message_id, conversationId=conversation_id,
            receivedAt=received_at, status=status, error=error,
        ).wire_dict())

    async def on_generation_started(self, *, generation_id: str, message_id: str,
                                    conversation_id: str | None = None,
                                    request_id: str | None = None,
                                    model: str | None = None) -> None:
        """发送 v2 生成开始。"""
        await self._send_json(GenerationStartedMessage(
            generationId=generation_id, messageId=message_id,
            conversationId=conversation_id, requestId=request_id, model=model,
        ).wire_dict())

    async def on_generation_segment_started(self, *, generation_id: str, segment_id: str,
                                            segment_index: int, channel: str, kind: str,
                                            conversation_id: str | None = None) -> None:
        """发送 v2 分段开始。"""
        await self._send_json(GenerationSegmentStartedMessage(
            generationId=generation_id, segmentId=segment_id,
            segmentIndex=segment_index, channel=channel, kind=kind,
            conversationId=conversation_id,
        ).wire_dict())

    async def on_generation_delta(self, *, generation_id: str, segment_id: str,
                                  segment_index: int, delta_sequence: int, delta: str,
                                  conversation_id: str | None = None) -> None:
        """发送 v2 分段增量。"""
        await self._send_json(GenerationDeltaMessage(
            generationId=generation_id, segmentId=segment_id,
            segmentIndex=segment_index, deltaSequence=delta_sequence, delta=delta,
            conversationId=conversation_id,
        ).wire_dict())

    async def on_generation_segment_completed(self, *, generation_id: str, segment_id: str,
                                              segment_index: int, channel: str, kind: str,
                                              content: str | None = None,
                                              conversation_id: str | None = None) -> None:
        """发送 v2 分段完成。"""
        await self._send_json(GenerationSegmentCompletedMessage(
            generationId=generation_id, segmentId=segment_id,
            segmentIndex=segment_index, channel=channel, kind=kind, content=content,
            conversationId=conversation_id,
        ).wire_dict())

    async def on_generation_completed(self, *, generation_id: str, message_id: str,
                                      finish_reason: str, usage, model: str,
                                      mood: str | None = None,
                                      completeness: str = "complete",
                                      channel: str = "public_reply",
                                      conversation_id: str | None = None,
                                      conversation_usage: dict | None = None) -> None:
        """发送 v2 父完成。usage 为契约 TokenUsage（rounds + turn_total）。

        conversation_usage: 批次 3 会话级累计 dict {total, by_model}（来自
            get_session_stats），非 None 时填 conversationUsage。
        """
        conv_usage = None
        if conversation_usage is not None:
            conv_usage = ConversationUsage(
                total=conversation_usage.get("total", 0),
                byModel=conversation_usage.get("by_model", {}),
            )
        await self._send_json(GenerationCompletedMessage(
            generationId=generation_id, messageId=message_id, channel=channel,
            finishReason=finish_reason, usage=usage, model=model,
            mood=mood, completeness=completeness,
            conversationId=conversation_id, conversationUsage=conv_usage,
        ).wire_dict())

    async def on_generation_failed(self, *, generation_id: str, code: str, message: str,
                                   retryable: bool, message_id: str | None = None,
                                   details: dict | None = None,
                                   model: str | None = None,
                                   conversation_id: str | None = None) -> None:
        """发送 v2 父失败。model 为 turn 启动快照（批次 5 D8），可缺省省略。"""
        await self._send_json(GenerationFailedMessage(
            generationId=generation_id, messageId=message_id, code=code,
            message=message, retryable=retryable, model=model,
            details=details or {},
            conversationId=conversation_id,
        ).wire_dict())

    async def on_message(self, msg) -> None:
        """发送完整消息。

        Args:
            msg: 消息对象（str 或 ChatMessage 等）。
        """
        if isinstance(msg, str):
            data = {"content": msg}
        elif hasattr(msg, "to_dict"):
            data = msg.to_dict()
        else:
            data = vars(msg)
        # 契约：MessageMessage 仅 role/content/mood；其余键（如 content_type/
        # tool_calls）由广播方另行发送，不在此帧承载
        await self._send_json(MessageMessage(
            role=data.get("role"), content=data.get("content"), mood=data.get("mood"),
        ).wire_dict())

    async def close(self) -> None:
        """关闭 WebSocket 连接，发送 close frame 并等待客户端确认。"""
        try:
            await self._ws.close()
        except Exception:
            # 连接可能已断开——常规良性情况，debug 级留痕（供诊断）
            self._logger.debug("WebSocket close 失败（连接可能已断开）", exc_info=True)
