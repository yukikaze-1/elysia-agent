"""WebSocketChannel — JSON 消息推送。

wire format:
    {"type": "delta", "content": "..."}        — 流式 token
    {"type": "typing", "typing": true/false}    — 输入状态
    {"type": "expression", "expression": "...", "intensity": 0.8}
    {"type": "action", "action": "wave", ...}
    {"type": "command", "command": "...", ...}
    {"type": "message", ...msg.to_dict()...}    — 完整消息
"""

import logging

from Module.logger import get_caller_logger
from .protocol import ChannelCapability, OutputChannel


class WebSocketChannel(OutputChannel):
    """WebSocket 输出通道 — 包装单个连接。连接生命周期由外部管理。"""

    def __init__(self, ws, *, logger: logging.Logger | None = None):
        self._ws = ws
        self._logger = logger or get_caller_logger()

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
        try:
            await self._ws.send_json(data)
        except Exception:
            self._logger.warning("WebSocket send failed", exc_info=True)

    async def on_typing(self, typing: bool) -> None:
        await self._send_json({"type": "typing", "typing": typing})

    async def on_text_delta(self, delta: str, *, stream_id: int | None = None) -> None:
        payload: dict = {"type": "delta", "content": delta}
        if stream_id is not None:
            payload["stream_id"] = stream_id
        await self._send_json(payload)

    async def on_expression(self, expression: str, intensity: float = 1.0) -> None:
        await self._send_json({
            "type": "expression",
            "expression": expression,
            "intensity": intensity,
        })

    async def on_action(self, action: str, *, params: dict | None = None) -> None:
        payload: dict[str, object] = {"type": "action", "action": action}
        if params:
            payload["params"] = params
        await self._send_json(payload)

    async def on_command(self, cmd: str, *, params: dict | None = None) -> None:
        payload: dict[str, object] = {"type": "command", "command": cmd}
        if params:
            payload["params"] = params
        await self._send_json(payload)

    async def on_audio_chunk(self, chunk: bytes, *, stream_id: int = 0) -> None:
        import base64
        self._audio_seq = getattr(self, "_audio_seq", {})
        seq = self._audio_seq.get(stream_id, 0)
        self._audio_seq[stream_id] = seq + 1
        await self._send_json({
            "type": "audio",
            "data": base64.b64encode(chunk).decode("ascii"),
            "stream_id": stream_id,
            "seq": seq,
            "is_final": False,
        })

    async def on_audio_end(self, stream_id: int = 0) -> None:
        await self._send_json({
            "type": "audio_end",
            "stream_id": stream_id,
        })

    async def on_state(self, state: str) -> None:
        await self._send_json({"type": "state", "state": state})

    async def on_message(self, msg) -> None:
        if isinstance(msg, str):
            data = {"content": msg}
        elif hasattr(msg, "to_dict"):
            data = msg.to_dict()
        else:
            data = vars(msg)
        data["type"] = "message"
        await self._send_json(data)

    async def close(self) -> None:
        """关闭 WebSocket 连接，发送 close frame 并等待客户端确认。"""
        try:
            await self._ws.close()
        except Exception:
            pass  # 连接可能已经断开
