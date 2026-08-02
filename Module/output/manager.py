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
from Module.output.protocol import ChannelCapability, OutputChannel


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

    async def _broadcast(self, method: str, *args, channels=None, capability=None, **kwargs):
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
        msg,
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
        """广播 token 消耗统计到所有通道（前端实时更新用量）。

        Args:
            data: token 消耗统计数据字典。
            channels: 候选通道列表，为 None 则使用全部已注册通道。
        """
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
