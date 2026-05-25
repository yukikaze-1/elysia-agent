"""ConsoleChannel — 终端打字机效果输出。"""

from .protocol import ChannelCapability, OutputChannel


class ConsoleChannel(OutputChannel):
    """终端输出通道。支持流式 delta 和完整消息。"""

    @property
    def capabilities(self) -> set[ChannelCapability]:
        return {ChannelCapability.TEXT_DELTA, ChannelCapability.TEXT_MESSAGE}

    async def on_text_delta(self, delta: str) -> None:
        """逐字打印，无换行。"""
        print(delta, end="", flush=True)

    async def on_message(self, msg) -> None:
        """打印带格式的完整消息（含 inner_voice）。"""
        inner = getattr(msg, "inner_voice", "")
        role = getattr(msg, "role", "?")
        content = getattr(msg, "content", "")
        print()
        if inner:
            print(f"  [{role} 💭] {inner}")
        print(f"  [{role}]: {content}")

    async def close(self) -> None:
        pass
