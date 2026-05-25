"""集成测试。运行: python -m Module.output"""

import asyncio

from .console import ConsoleChannel
from .manager import OutputManager
from .websocket import WebSocketChannel


class FakeMsg:
    def __init__(self, role="Elysia", content="", inner_voice=""):
        self.role = role
        self.content = content
        self.inner_voice = inner_voice

    def __repr__(self):
        return f"FakeMsg(role={self.role!r}, content={self.content!r})"


async def main():
    mgr = OutputManager()
    mgr.add_channel(ConsoleChannel())

    # 1. Non-streaming send
    print("=== 非流式 send ===")
    await mgr.send(FakeMsg(role="Elysia", content="你好，世界！", inner_voice="这是一条测试消息"))

    # 2. Streaming with typing indicator
    print()
    print("=== 流式 + typing ===")

    async def fake_deltas():
        for token in ["流", "式", "输", "出", "测", "试", "！"]:
            yield token
            await asyncio.sleep(0.05)

    await mgr.typing(True)
    result = await mgr.stream(
        fake_deltas(),
        finalize=lambda text: FakeMsg(role="Elysia", content=text, inner_voice="流式装配完成"),
    )
    await mgr.typing(False)
    print(f"\n  result: {result}")

    # 3. Expression
    print()
    print("=== 表情 ===")
    await mgr.expression("happy", 0.8)

    # 4. Action
    print()
    print("=== 动作 ===")
    await mgr.action("wave")

    # 5. Command
    print()
    print("=== 指令 ===")
    await mgr.command("change_bg", params={"bg": "night_sky"})

    # 6. WebSocket channel (mock)
    print()
    print("=== WebSocket 通道 (mock) ===")

    class MockWS:
        async def send_json(self, data):
            print(f"  WS → {data}")

    ws_mgr = OutputManager()
    ws_mgr.add_channel(WebSocketChannel(MockWS()))

    await ws_mgr.typing(True)

    async def ws_deltas():
        yield "Hello "
        yield "World!"

    await ws_mgr.stream(
        ws_deltas(),
        finalize=lambda text: FakeMsg(role="Elysia", content=text),
    )
    await ws_mgr.expression("surprised", 1.0)
    await ws_mgr.typing(False)

    await mgr.close()
    print()
    print("全部测试完成!")


if __name__ == "__main__":
    asyncio.run(main())
