import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Module.output.protocol import ChannelCapability, OutputChannel
from Module.output.manager import OutputManager
from Module.output.console import ConsoleChannel
from Module.output.websocket import WebSocketChannel


# ============================================================
# OutputChannel ABC
# ============================================================

@pytest.mark.unit
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

@pytest.mark.unit
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

@pytest.mark.service
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

    async def on_command(self, cmd: str, *, params: dict | None = None) -> None:
        self.events.append(("command", (cmd, params)))

    async def on_audio_chunk(self, chunk: bytes, *, stream_id: int = 0) -> None:
        self.events.append(("audio_chunk", (chunk, stream_id)))

    async def on_audio_end(self, stream_id: int = 0, *, format: str = "wav") -> None:
        self.events.append(("audio_end", (stream_id, format)))

    async def on_state(self, state: str) -> None:
        self.events.append(("state", (state,)))

    async def close(self) -> None:
        pass


@pytest.mark.service
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


@pytest.mark.service
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


@pytest.mark.service
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


# ============================================================
# AUDIO capability + command + stream lifecycle
# ============================================================

@pytest.mark.service
class TestAudioCapability:
    def test_console_no_audio(self):
        ch = ConsoleChannel()
        assert ChannelCapability.AUDIO not in ch.capabilities

    def test_websocket_has_audio(self):
        ch = WebSocketChannel(MagicMock())
        assert ChannelCapability.AUDIO in ch.capabilities

    @pytest.mark.asyncio
    async def test_audio_chunk_routes_to_capable_channels(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.AUDIO})
        mgr.add_channel(ch)
        ch2 = _TestChannel({ChannelCapability.TEXT_MESSAGE})
        mgr.add_channel(ch2)

        await mgr.audio_chunk(b"test")
        assert len(ch.events) == 1
        assert ch.events[0][0] == "audio_chunk"
        assert len(ch2.events) == 0

    @pytest.mark.asyncio
    async def test_audio_end_routes_to_capable_channels(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.AUDIO})
        mgr.add_channel(ch)

        await mgr.audio_end(stream_id=1)
        assert len(ch.events) == 1
        assert ch.events[0][0] == "audio_end"


@pytest.mark.service
class TestCommandMethod:
    @pytest.mark.asyncio
    async def test_command_routes_to_capable_channels(self):
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.COMMAND})
        mgr.add_channel(ch)

        await mgr.command("reload", params={"plugin": "x"})
        assert len(ch.events) == 1
        assert ch.events[0][0] == "command"
        assert ch.events[0][1] == ("reload", {"plugin": "x"})


@pytest.mark.service
class TestStreamLifecycle:
    @pytest.mark.asyncio
    async def test_stream_with_typing_brackets(self):
        """typing(True) → stream → typing(False) 完整流程。"""
        mgr = OutputManager()
        ch = _TestChannel({ChannelCapability.TEXT_DELTA, ChannelCapability.TYPING, ChannelCapability.TEXT_MESSAGE})
        mgr.add_channel(ch)

        await mgr.typing(True)
        assert ch.events[-1] == ("typing", (True,))

        async def _deltas():
            yield "a"
            yield "b"

        result = await mgr.stream(_deltas(), finalize=lambda t: t)
        assert result == "ab"
        assert ("delta", ("a",)) in ch.events
        assert ("delta", ("b",)) in ch.events
        assert ("message", ("ab",)) in ch.events

        await mgr.typing(False)
        assert ch.events[-1] == ("typing", (False,))


# ============================================================
# Error isolation — more methods + close
# ============================================================

class _FullFailingChannel(OutputChannel):
    """支持更多 fail_method 的 failing channel。"""
    def __init__(self, caps: set[ChannelCapability], fail_method: str):
        self._caps = caps
        self._fail_method = fail_method

    @property
    def capabilities(self) -> set[ChannelCapability]:
        return self._caps

    async def on_typing(self, typing: bool) -> None:
        if self._fail_method == "on_typing":
            raise RuntimeError("boom")

    async def on_text_delta(self, delta: str, *, stream_id: int | None = None) -> None:
        if self._fail_method == "on_text_delta":
            raise RuntimeError("delta fail")

    async def on_expression(self, expression: str, intensity: float = 1.0) -> None:
        if self._fail_method == "on_expression":
            raise RuntimeError("expression fail")

    async def on_action(self, action: str, *, params: dict | None = None) -> None:
        if self._fail_method == "on_action":
            raise RuntimeError("action fail")

    async def on_command(self, cmd: str, *, params: dict | None = None) -> None:
        if self._fail_method == "on_command":
            raise RuntimeError("command fail")

    async def on_message(self, msg) -> None:
        if self._fail_method == "on_message":
            raise RuntimeError("boom")

    async def close(self) -> None:
        if self._fail_method == "close":
            raise RuntimeError("close fail")


@pytest.mark.service
class TestMoreErrorIsolation:
    @pytest.mark.asyncio
    async def test_on_text_delta_failure_isolated(self):
        mgr = OutputManager()
        good = _TestChannel({ChannelCapability.TEXT_DELTA})
        bad = _FullFailingChannel({ChannelCapability.TEXT_DELTA}, fail_method="on_text_delta")
        mgr.add_channel(good)
        mgr.add_channel(bad)

        async def _deltas():
            yield "x"

        await mgr.stream(_deltas(), finalize=lambda t: t)
        assert len(good.events) >= 1

    @pytest.mark.asyncio
    async def test_on_expression_failure_isolated(self):
        mgr = OutputManager()
        good = _TestChannel({ChannelCapability.EXPRESSION})
        bad = _FullFailingChannel({ChannelCapability.EXPRESSION}, fail_method="on_expression")
        mgr.add_channel(good)
        mgr.add_channel(bad)

        await mgr.expression("happy")
        assert len(good.events) == 1

    @pytest.mark.asyncio
    async def test_on_action_failure_isolated(self):
        mgr = OutputManager()
        good = _TestChannel({ChannelCapability.ACTION})
        bad = _FullFailingChannel({ChannelCapability.ACTION}, fail_method="on_action")
        mgr.add_channel(good)
        mgr.add_channel(bad)

        await mgr.action("wave")
        assert len(good.events) == 1

    @pytest.mark.asyncio
    async def test_close_error_isolation(self):
        """close 时某个 channel 异常不影响其他 channel 关闭。"""
        mgr = OutputManager()
        bad = _FullFailingChannel({ChannelCapability.TEXT_MESSAGE}, fail_method="close")
        mgr.add_channel(bad)
        await mgr.close()  # should not raise


# ============================================================
# WebSocketChannel — method coverage
# ============================================================

@pytest.mark.service
class TestWebSocketChannelCoverage:
    @pytest.fixture
    def ws(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_on_typing(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_typing(True)
        ws.send_json.assert_called_with({"type": "typing", "typing": True})

    @pytest.mark.asyncio
    async def test_on_expression(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_expression("happy", intensity=0.8)
        ws.send_json.assert_called_with({
            "type": "expression", "expression": "happy", "intensity": 0.8,
        })

    @pytest.mark.asyncio
    async def test_on_action_with_params(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_action("wave", params={"speed": 2})
        ws.send_json.assert_called_with({
            "type": "action", "action": "wave", "params": {"speed": 2},
        })

    @pytest.mark.asyncio
    async def test_on_action_without_params(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_action("nod")
        called = ws.send_json.call_args[0][0]
        assert called["type"] == "action"
        assert "params" not in called

    @pytest.mark.asyncio
    async def test_on_command_with_params(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_command("reload", params={"p": 1})
        ws.send_json.assert_called_with({
            "type": "command", "command": "reload", "params": {"p": 1},
        })

    @pytest.mark.asyncio
    async def test_on_command_without_params(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_command("reload")
        called = ws.send_json.call_args[0][0]
        assert called["type"] == "command"
        assert "params" not in called

    @pytest.mark.asyncio
    async def test_on_message_str(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_message("plain text")
        ws.send_json.assert_called_with({
            "type": "message", "content": "plain text",
        })

    @pytest.mark.asyncio
    async def test_on_message_dict_like(self, ws):
        msg = MagicMock()
        msg.to_dict.return_value = {"role": "Elysia", "content": "hi"}
        ch = WebSocketChannel(ws)
        await ch.on_message(msg)
        called = ws.send_json.call_args[0][0]
        assert called["type"] == "message"
        assert called["role"] == "Elysia"

    @pytest.mark.asyncio
    async def test_on_audio_chunk(self, ws):
        import base64
        ch = WebSocketChannel(ws)
        await ch.on_audio_chunk(b"\x00\x01", stream_id=0)
        called = ws.send_json.call_args[0][0]
        assert called["type"] == "audio"
        assert called["data"] == base64.b64encode(b"\x00\x01").decode("ascii")
        assert called["is_final"] is False

    @pytest.mark.asyncio
    async def test_on_audio_end(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_audio_end(stream_id=0)
        ws.send_json.assert_called_with({
            "type": "audio_end", "stream_id": 0, "format": "wav",
        })

    @pytest.mark.asyncio
    async def test_on_state(self, ws):
        ch = WebSocketChannel(ws)
        await ch.on_state("thinking")
        ws.send_json.assert_called_with({
            "type": "state", "state": "thinking",
        })

    @pytest.mark.asyncio
    async def test_close_already_disconnected(self, ws):
        ws.close = AsyncMock(side_effect=ConnectionError("already closed"))
        ch = WebSocketChannel(ws)
        await ch.close()  # should not raise


# ============================================================
# ConsoleChannel — non-streaming paths
# ============================================================

@pytest.mark.unit
class TestConsoleChannelNonStreaming:
    def test_on_message_str_path(self, capsys):
        """on_message with str when _delta_printed=False — short return。"""
        ch = ConsoleChannel()
        asyncio.run(ch.on_message("hello"))
        # str path with _delta_printed=False prints nothing
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_on_message_after_delta(self, capsys):
        """on_message after on_text_delta — _delta_printed=True path。"""
        ch = ConsoleChannel()
        asyncio.run(ch.on_text_delta("streamed"))
        asyncio.run(ch.on_message(MagicMock(role="?", inner_voice="", content="")))
        captured = capsys.readouterr()
        # Just prints newline, no content
        assert "\n" in captured.out

    def test_on_message_with_inner_voice_non_streaming(self, capsys):
        """_delta_printed=False with inner_voice — prints both。"""
        ch = ConsoleChannel()
        msg = MagicMock()
        msg.role = "Elysia"
        msg.inner_voice = "thinking..."
        msg.content = "hello"
        asyncio.run(ch.on_message(msg))
        captured = capsys.readouterr()
        assert "Elysia" in captured.out
        assert "hello" in captured.out


# ============================================================
# WebSocketChannel v2 wire 方法（批次 2 Task 4）
# ============================================================

@pytest.mark.unit
class TestWebSocketV2Methods:
    def _make_ch(self):
        ws = AsyncMock()
        return WebSocketChannel(ws), ws

    @pytest.mark.asyncio
    async def test_on_user_message_ack_wire(self):
        ch, ws = self._make_ch()
        await ch.on_user_message_ack(
            message_id="msg_u1", client_request_id="req_1", client_message_id=None,
            conversation_id=None, received_at="2026-08-04T00:00:00+00:00", status="accepted",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "user_message.ack"
        assert sent["messageId"] == "msg_u1"
        assert sent["clientRequestId"] == "req_1"
        assert sent["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_on_generation_started_wire(self):
        ch, ws = self._make_ch()
        await ch.on_generation_started(
            generation_id="gen_1", message_id="msg_a1", conversation_id=None,
            request_id="req_1", model="fake/test",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.started"
        assert sent["generationId"] == "gen_1"
        assert sent["messageId"] == "msg_a1"

    @pytest.mark.asyncio
    async def test_on_generation_segment_started_wire(self):
        ch, ws = self._make_ch()
        await ch.on_generation_segment_started(
            generation_id="gen_1", segment_id="seg_1", segment_index=0,
            channel="public_reply", kind="text",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.segment.started"
        assert sent["segmentId"] == "seg_1"
        assert sent["channel"] == "public_reply"

    @pytest.mark.asyncio
    async def test_on_generation_delta_wire(self):
        ch, ws = self._make_ch()
        await ch.on_generation_delta(
            generation_id="gen_1", segment_id="seg_1", segment_index=0,
            delta_sequence=0, delta="你",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.delta"
        assert sent["deltaSequence"] == 0
        assert sent["delta"] == "你"

    @pytest.mark.asyncio
    async def test_on_generation_completed_wire(self):
        ch, ws = self._make_ch()
        from frontend_contract import TokenUsage
        usage = TokenUsage(rounds=[], turn_total=0)
        await ch.on_generation_completed(
            generation_id="gen_1", message_id="msg_a1", finish_reason="stop",
            usage=usage, model="fake/test", mood="开心",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.completed"
        assert sent["finishReason"] == "stop"
        assert sent["usage"]["turn_total"] == 0
        assert sent["mood"] == "开心"

    @pytest.mark.asyncio
    async def test_on_generation_failed_wire(self):
        ch, ws = self._make_ch()
        await ch.on_generation_failed(
            generation_id="gen_1", message_id="msg_a1",
            code="GENERATION_CANCELLED", message="已取消", retryable=False,
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.failed"
        assert sent["code"] == "GENERATION_CANCELLED"
        assert sent["retryable"] is False


# ============================================================
# 批次 8c F3 模型化发射（ActionMessage/TokenUsageMessage，Task 3）
# ============================================================

class _FakeWS:
    """假 WebSocket：仅收集 send_json 载荷（_send_json 只调 self._ws.send_json）。"""

    def __init__(self):
        self.sent = []

    async def send_json(self, d):
        self.sent.append(d)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_action_emits_modeled_message():
    """on_action 带 params → 模型化 ActionMessage wire（行为回归守卫）。"""
    from Module.output import WebSocketChannel
    ws = _FakeWS()
    ch = WebSocketChannel(ws, protocol_version=1)
    await ch.on_action("tool_executing", params={"tool": "t", "call_id": "c"})
    assert ws.sent[0]["type"] == "action"
    assert ws.sent[0]["params"] == {"tool": "t", "call_id": "c"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_action_no_params_omits_key():
    """on_action 无 params → wire 省略 params 键（行为回归守卫）。"""
    from Module.output import WebSocketChannel
    ws = _FakeWS()
    ch = WebSocketChannel(ws, protocol_version=1)
    await ch.on_action("wave")
    assert ws.sent[0]["type"] == "action"
    assert "params" not in ws.sent[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_token_usage_emits_modeled_message():
    """on_token_usage → 模型化 TokenUsageMessage：null 键省略、session 保留 byModel（批次 8c F19）。"""
    from Module.output import WebSocketChannel
    ws = _FakeWS()
    ch = WebSocketChannel(ws, protocol_version=1)
    data = {"turn": {"rounds": [{"model": "m", "input_tokens": 1, "output_tokens": 2,
                                  "total_tokens": 3, "round_num": 0,
                                  "cached_tokens": None, "reasoning_tokens": None}],
                     "turn_total": 3},
            "session": {"total": 3, "by_model": {"m": 3}}}
    await ch.on_token_usage(data)
    assert ws.sent[0]["type"] == "token_usage"
    assert ws.sent[0]["session"]["byModel"] == {"m": 3}
    assert "by_model" not in ws.sent[0]["session"]
    assert "cached_tokens" not in ws.sent[0]["turn"]["rounds"][0]
