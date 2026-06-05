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
from typing import AsyncIterator, Callable

from Module.logger import get_caller_logger
from .protocol import ChannelCapability, OutputChannel


class OutputManager:
    def __init__(self, *, logger: logging.Logger | None = None):
        self._logger = logger or get_caller_logger()
        self._channels: list[OutputChannel] = []

    # ---- Channel management ----

    def add_channel(self, channel: OutputChannel) -> None:
        self._channels.append(channel)

    def remove_channel(self, channel: OutputChannel) -> None:
        self._channels.remove(channel)

    # ---- Internal: filter channels ----

    def _filter(
        self,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> list[OutputChannel]:
        result = channels if channels is not None else self._channels
        if capability is not None:
            result = [c for c in result if capability in c.capabilities]
        return result

    async def _broadcast(self, method: str, *args, channels=None, capability=None, **kwargs):
        """向筛选后的通道广播指定方法。单通道失败不影响其他通道。"""
        targets = self._filter(channels=channels, capability=capability)
        if not targets:
            return
        results = await asyncio.gather(
            *(getattr(c, method)(*args, **kwargs) for c in targets),
            return_exceptions=True,
        )
        for c, result in zip(targets, results):
            if isinstance(result, Exception):
                self._logger.debug(
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

        finalize 接收累积的完整文本，返回 ChatMessage 等消息对象。
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
        msg,
        *,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        """非流式 — 直接发送完整消息。"""
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
        await self._broadcast(
            "on_action", action, params=params,
            channels=channels, capability=capability or ChannelCapability.ACTION,
        )

    async def command(
        self,
        cmd: str,
        *,
        params: dict | None = None,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
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
        """广播 token 消耗统计到所有通道（前端实时更新用量）。"""
        await self._broadcast(
            "on_token_usage", data,
            channels=channels,
        )

    async def audio_chunk(
        self,
        chunk: bytes,
        *,
        stream_id: int = 0,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        await self._broadcast(
            "on_audio_chunk", chunk, stream_id=stream_id,
            channels=channels, capability=capability or ChannelCapability.AUDIO,
        )

    async def audio_end(
        self,
        stream_id: int = 0,
        *,
        channels: list[OutputChannel] | None = None,
        capability: ChannelCapability | None = None,
    ) -> None:
        await self._broadcast(
            "on_audio_end", stream_id,
            channels=channels, capability=capability or ChannelCapability.AUDIO,
        )

    async def close(self) -> None:
        tasks = [c.close() for c in self._channels]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for channel, result in zip(self._channels, results):
            if isinstance(result, Exception):
                self._logger.warning(
                    "Failed to close channel %s: %s",
                    type(channel).__name__, result,
                )
