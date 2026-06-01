import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .protocol import ChannelCapability, OutputChannel
from .manager import OutputManager
from .console import ConsoleChannel
from .websocket import WebSocketChannel


# ============================================================
# OutputChannel ABC
# ============================================================

class TestOutputChannel:
    def test_default_noops(self):
        class DefaultOnly(OutputChannel):
            @property
            def capabilities(self) -> set[ChannelCapability]:
                return {ChannelCapability.TEXT_MESSAGE}

            async def on_message(self, msg) -> None:
                pass

            async def close(self) -> None:
                pass

        ch = DefaultOnly()
        # Default no-op methods should not raise
        asyncio.run(ch.on_typing(True))
        asyncio.run(ch.on_expression("happy"))
        asyncio.run(ch.on_action("wave"))
        asyncio.run(ch.on_audio_chunk(b""))
        asyncio.run(ch.on_command("greet"))

    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):

            class MissingCap(OutputChannel):
                pass

            MissingCap()  # type: ignore[abstract]

    def test_override_works(self):
        class Custom(OutputChannel):
            @property
            def capabilities(self) -> set[ChannelCapability]:
                return {ChannelCapability.TEXT_MESSAGE}

            async def on_message(self, msg) -> None:
                pass

            async def close(self) -> None:
                pass

        ch = Custom()
        assert ch.capabilities == {ChannelCapability.TEXT_MESSAGE}


# ============================================================
# ConsoleChannel
# ============================================================

class TestConsoleChannel:
    def test_capabilities(self):
        ch = ConsoleChannel()
        assert ChannelCapability.TEXT_DELTA in ch.capabilities
        assert ChannelCapability.TEXT_MESSAGE in ch.capabilities

    def test_on_text_delta(self, capsys):
        ch = ConsoleChannel()
        asyncio.run(ch.on_text_delta("hello"))
        captured = capsys.readouterr()
        assert captured.out == "hello"

    def test_on_message(self, capsys):
        msg = MagicMock()
        msg.role = "Elysia"
        msg.content = "你好"
        msg.inner_voice = "内心的想法"
        ch = ConsoleChannel()
        asyncio.run(ch.on_message(msg))
        captured = capsys.readouterr()
        assert "Elysia" in captured.out
        assert "你好" in captured.out


# ============================================================
# WebSocketChannel
# ============================================================

class TestWebSocketChannel:
    def test_capabilities(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws)
        assert ChannelCapability.TEXT_DELTA in ch.capabilities
        assert ChannelCapability.TYPING in ch.capabilities
        assert ChannelCapability.EXPRESSION in ch.capabilities
        assert ChannelCapability.ACTION in ch.capabilities
        assert ChannelCapability.COMMAND in ch.capabilities

    @pytest.mark.asyncio
    async def test_on_typing(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws)
        await ch.on_typing(True)
        ws.send_json.assert_called_with({"type": "typing", "typing": True})

    @pytest.mark.asyncio
    async def test_on_expression(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws)
        await ch.on_expression("happy", 0.8)
        ws.send_json.assert_called_with({
            "type": "expression",
            "expression": "happy",
            "intensity": 0.8,
        })

    @pytest.mark.asyncio
    async def test_on_text_delta(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws)
        await ch.on_text_delta("hello")
        ws.send_json.assert_called_with({"type": "delta", "content": "hello"})

    @pytest.mark.asyncio
    async def test_on_action(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws)
        await ch.on_action("wave")
        ws.send_json.assert_called_with({"type": "action", "action": "wave"})

    @pytest.mark.asyncio
    async def test_on_action_with_params(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws)
        await ch.on_action("wave", params={"speed": 2})
        ws.send_json.assert_called_with({
            "type": "action", "action": "wave", "params": {"speed": 2},
        })

    @pytest.mark.asyncio
    async def test_on_command(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws)
        await ch.on_command("change_bg", params={"bg": "night"})
        ws.send_json.assert_called_with({
            "type": "command", "command": "change_bg", "params": {"bg": "night"},
        })

    @pytest.mark.asyncio
    async def test_on_message(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws)
        msg = MagicMock()
        msg.to_dict.return_value = {"role": "Elysia", "content": "hi"}
        await ch.on_message(msg)
        ws.send_json.assert_called_with(
            {"role": "Elysia", "content": "hi", "type": "message"}
        )

    @pytest.mark.asyncio
    async def test_close(self):
        ws = MagicMock()
        ch = WebSocketChannel(ws)
        await ch.close()  # should not raise


# ============================================================
# OutputManager
# ============================================================

class _TestChannel(OutputChannel):
    def __init__(self, caps: set[ChannelCapability]):
        self._caps = caps
        self.events: list[tuple[str, tuple]] = []

    @property
    def capabilities(self) -> set[ChannelCapability]:
        return self._caps

    async def on_typing(self, typing: bool) -> None:
        self.events.append(("typing", (typing,)))

    async def on_text_delta(self, delta: str, *, stream_id: int | None = None) -> None:
        self.events.append(("delta", (delta,)))

    async def on_message(self, msg) -> None:
        self.events.append(("message", (msg,)))

    async def on_expression(self, expression: str, intensity: float = 1.0) -> None:
        self.events.append(("expression", (expression, intensity)))

    async def on_action(self, action: str, *, params: dict | None = None) -> None:
        self.events.append(("action", (action, params)))

    async def close(self) -> None:
        pass


class TestOutputManager:
    def test_add_remove_channel(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        mgr.add_channel(ch)
        assert len(mgr._channels) == 1
        mgr.remove_channel(ch)
        assert len(mgr._channels) == 0

    @pytest.mark.asyncio
    async def test_send_broadcasts_to_all(self):
        mgr = OutputManager()
        ch1 = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        ch2 = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        mgr.add_channel(ch1)
        mgr.add_channel(ch2)

        msg = MagicMock()
        await mgr.send(msg)

        assert len(ch1.events) == 1
        assert ch1.events[0][0] == "message"
        assert len(ch2.events) == 1

    @pytest.mark.asyncio
    async def test_send_specific_channels(self):
        mgr = OutputManager()
        ch1 = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        ch2 = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        mgr.add_channel(ch1)
        mgr.add_channel(ch2)

        msg = MagicMock()
        await mgr.send(msg, channels=[ch1])

        assert len(ch1.events) == 1
        assert len(ch2.events) == 0

    @pytest.mark.asyncio
    async def test_send_by_capability(self):
        mgr = OutputManager()
        ch1 = _TestChannel({ChannelCapability.TEXT_DELTA})
        ch2 = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        mgr.add_channel(ch1)
        mgr.add_channel(ch2)

        await mgr.typing(True)
        # Neither has TYPING capability, so no events should fire
        assert len(ch1.events) == 0
        assert len(ch2.events) == 0

    @pytest.mark.asyncio
    async def test_stream_deltas(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.TEXT_DELTA, ChannelCapability.TEXT_MESSAGE})
        mgr.add_channel(ch)

        async def _deltas():
            yield "hello "
            yield "world"

        result = await mgr.stream(
            _deltas(),
            finalize=lambda text: text,
        )

        assert result == "hello world"
        assert len(ch.events) == 3  # 2 deltas + 1 message

    @pytest.mark.asyncio
    async def test_stream_respects_capability(self):
        mgr = OutputManager()
        ch_full = _TestChannel(
            {ChannelCapability.TEXT_DELTA, ChannelCapability.TEXT_MESSAGE, ChannelCapability.TYPING}
        )
        mgr.add_channel(ch_full)

        async def _deltas():
            yield "hi"

        result = await mgr.stream(
            _deltas(),
            finalize=lambda text: text,
        )
        assert result == "hi"
        assert any(e[0] == "delta" for e in ch_full.events)
        assert any(e[0] == "message" for e in ch_full.events)

    @pytest.mark.asyncio
    async def test_typing_convenience(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.TYPING})
        mgr.add_channel(ch)

        await mgr.typing(True)
        assert ch.events[0] == ("typing", (True,))

    @pytest.mark.asyncio
    async def test_expression_convenience(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.EXPRESSION})
        mgr.add_channel(ch)

        await mgr.expression("sad", 0.5)
        assert ch.events[0] == ("expression", ("sad", 0.5))

    @pytest.mark.asyncio
    async def test_action_convenience(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.ACTION})
        mgr.add_channel(ch)

        await mgr.action("wave", params={"speed": 2})
        assert ch.events[0] == ("action", ("wave", {"speed": 2}))

    @pytest.mark.asyncio
    async def test_close(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        mgr.add_channel(ch)
        await mgr.close()  # should not raise


class _FailingChannel(OutputChannel):
    """用于测试错误隔离的 channel，指定方法会 raise。"""
    def __init__(self, caps: set[ChannelCapability], fail_method: str):
        self._caps = caps
        self._fail_method = fail_method

    @property
    def capabilities(self) -> set[ChannelCapability]:
        return self._caps

    async def on_typing(self, typing: bool) -> None:
        if self._fail_method == "on_typing":
            raise RuntimeError("boom")
        return

    async def on_message(self, msg) -> None:
        if self._fail_method == "on_message":
            raise RuntimeError("boom")
        return

    async def close(self) -> None:
        pass


class TestOutputManagerErrorIsolation:
    @pytest.mark.asyncio
    async def test_failing_channel_does_not_block_others(self):
        """一个 channel 失败不影响同广播中的其他 channel。"""
        mgr = OutputManager()
        good = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        bad = _FailingChannel({ChannelCapability.TEXT_MESSAGE}, fail_method="on_message")
        mgr.add_channel(good)
        mgr.add_channel(bad)

        msg = MagicMock()
        await mgr.send(msg)

        assert len(good.events) == 1  # good 还是收到了
        assert good.events[0][0] == "message"

    @pytest.mark.asyncio
    async def test_failing_channel_logs_error(self, caplog):
        """失败的 channel 应被 log 记录。"""
        import logging

        mgr = OutputManager()
        bad = _FailingChannel({ChannelCapability.TEXT_MESSAGE}, fail_method="on_message")
        mgr.add_channel(bad)

        # 给 manager 注入一个可以被 caplog 捕获的 logger
        mgr._logger = logging.getLogger("test_output")
        mgr._logger.setLevel(logging.DEBUG)

        msg = MagicMock()
        await mgr.send(msg)

        assert "broadcast" in caplog.text

    @pytest.mark.asyncio
    async def test_all_channels_fail_does_not_raise(self):
        """全部 channel 都失败也不应向上抛异常。"""
        mgr = OutputManager()
        bad = _FailingChannel({ChannelCapability.TEXT_MESSAGE}, fail_method="on_message")
        mgr.add_channel(bad)

        msg = MagicMock()
        await mgr.send(msg)  # should not raise


class TestWebSocketChannelErrorHandling:
    @pytest.mark.asyncio
    async def test_send_json_catches_connection_error(self, caplog):
        """send_json 失败时记录 warning 而不向上抛异常。"""
        import logging

        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=ConnectionError("connection closed"))
        ch = WebSocketChannel(ws)
        ch._logger = logging.getLogger("test_ws")
        ch._logger.setLevel(logging.WARNING)

        await ch.on_text_delta("hello")  # should not raise

        assert "WebSocket send failed" in caplog.text
