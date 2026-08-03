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
    AudioEndMessage,
    CommandMessage,
    DeltaMessage,
    ExpressionMessage,
    MessageMessage,
    StateMessage,
    TypingMessage,
)


class WebSocketChannel(OutputChannel):
    """WebSocket 输出通道 — 包装单个连接。连接生命周期由外部管理。"""

    def __init__(self, ws, *, logger: logging.Logger | None = None):
        """初始化 WebSocket 输出通道。

        Args:
            ws: aiohttp WebSocket 连接对象。
            logger: 日志记录器，不传则自动检测调用模块。
        """
        self._ws = ws
        self._logger = logger or get_caller_logger()
        self._audio_seq: dict[int, int] = {}

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
        # 注：暂不切 AudioMessage——现 wire 带 is_final 键（AudioMessage 未建模），
        # 转换会丢键破坏行为等价；is_final 为前端未消费的冗余键，留待契约收口。
        import base64
        seq = self._audio_seq.get(stream_id, 0)
        self._audio_seq[stream_id] = seq + 1
        await self._send_json({
            "type": "audio",
            "data": base64.b64encode(chunk).decode("ascii"),
            "stream_id": stream_id,
            "seq": seq,
            "is_final": False,
        })

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
        # 注：暂不切 TokenUsageMessage——data.turn.rounds 由 core TokenUsage dump 而来
        # （含 session_id 键，TokenRound 未建模），转换会丢该键；前端不消费，
        # 留待契约收口。data 本身已是模型 dump，非手写字典。
        await self._send_json({"type": "token_usage", **data})

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
            pass  # 连接可能已经断开
