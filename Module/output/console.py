"""ConsoleChannel — 终端打字机效果输出。"""

from Module.output.protocol import ChannelCapability, OutputChannel


class ConsoleChannel(OutputChannel):
    """终端输出通道。支持流式 delta 和完整消息。

    When text is streamed via on_text_delta, on_message only prints the
    inner_voice (if any) — the content was already delivered by the stream.
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

    async def on_message(self, msg) -> None:
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
            # Content already streamed — just finalize the line and show inner_voice
            print()
            self._delta_printed = False
            if inner:
                print(f"  [{role} 💭] {inner}")
        else:
            # Non-streaming delivery — print full content
            if inner:
                print(f"  [{role} 💭] {inner}")
            print(f"  [{role}]: {content}")

    async def close(self) -> None:
        pass
