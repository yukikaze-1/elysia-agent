"""ConsoleChannel — 终端打字机效果输出。"""

from typing import Any

from Module.output.protocol import ChannelCapability, OutputChannel


class ConsoleChannel(OutputChannel):
    """终端输出通道。支持流式 delta 和完整消息。

    当文本经 on_text_delta 流式输出时，on_message 仅打印 inner_voice（如有）——
    内容已由流式通道投递。
    """

    def __init__(self):
        """初始化终端输出通道。"""
        self._delta_printed = False

    @property
    def capabilities(self) -> set[ChannelCapability]:
        return {ChannelCapability.TEXT_DELTA, ChannelCapability.TEXT_MESSAGE}

    async def on_text_delta(self, delta: str, *, stream_id: int | None = None) -> None:
        """逐字打印，无换行。

        Args:
            delta: 单个文本增量。
            stream_id: 流标识（未使用）。
        """
        self._delta_printed = True
        print(delta, end="", flush=True)

    async def on_message(self, msg: Any) -> None:
        """打印格式化消息。

        Args:
            msg: 消息对象（str 或 ChatMessage）。若为 str 且已通过 delta 输出则仅换行。若为 ChatMessage
                则打印 role/content/inner_voice。
        """
        if isinstance(msg, str):
            if self._delta_printed:
                print()
                self._delta_printed = False
            return
        inner = getattr(msg, "inner_voice", "")
        role = getattr(msg, "role", "?")
        content = getattr(msg, "content", "")

        if self._delta_printed:
            # 内容已流式输出 —— 只需收尾并展示 inner_voice
            print()
            self._delta_printed = False
            if inner:
                print(f"  [{role} 💭] {inner}")
        else:
            # 非流式投递 —— 打印完整内容
            if inner:
                print(f"  [{role} 💭] {inner}")
            print(f"  [{role}]: {content}")

    async def close(self) -> None:
        pass
