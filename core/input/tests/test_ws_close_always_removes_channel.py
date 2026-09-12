# 测试文件：core/input/tests/test_ws_close_always_removes_channel.py
"""S2-E1 回归守卫：连接清理无论如何都要把 channel 从注册表摘掉。

背景（评测报告 S2-E1）：`_close_ws_connection` 把**纯注册表清理**
`agent.output.remove_channel(conn.channel)` 排在两个可失败 await
（`audio_buf.flush()`、`voice_mgr.close()`）**之后**，且全程无 `try/finally`。
任一次抛错即让该连接**永久留在** `agent.output.channels`：

* 此后每次广播都对这条死连接 `send_json` —— 每帧一条 warning + 完整 traceback，
  日志随消息量线性爆炸；
* desktop 状态永久失真；v2 断线取消 generation 一并失效。

本文件用「让 flush 抛错」的探针证明清理仍然发生——守卫要先证明自己能抓到违规。
"""

import asyncio

import pytest

from core.input.ws_handler import _WsConnection, _close_ws_connection


class _RaisingBuffer:
    """flush() 必抛的 audio buffer 替身。"""

    def __init__(self) -> None:
        self.flush_called = False

    async def flush(self) -> None:
        self.flush_called = True
        raise RuntimeError("boom: flush failed（模拟真实清理路径抛错）")


class _OkBuffer:
    def __init__(self) -> None:
        self.flush_called = False

    async def flush(self) -> None:
        self.flush_called = True


class _RaisingVoice:
    """close() 必抛的 voice manager 替身。"""

    emitted_message_ids: set[str] = set()

    async def close(self) -> None:
        raise RuntimeError("boom: voice close failed")


def _make_conn(*, audio_buf=None, voice_mgr=None, protocol_version: int = 1) -> _WsConnection:
    return _WsConnection(
        ws=None,  # type: ignore[arg-type] - 本测试不触碰 ws
        channel=object(),  # type: ignore[arg-type] - 只作为 remove_channel 的入参
        client_id="c1",
        protocol_version=protocol_version,
        desktop_conn_id=None,
        voice_mgr=voice_mgr,
        sent_message_ids=set(),
        audio_buf=audio_buf,
    )


class _FakeOutput:
    def __init__(self) -> None:
        self.channels: list[object] = []
        self.removed: list[object] = []

    def remove_channel(self, channel) -> None:  # noqa: ANN001 - 测试替身
        self.removed.append(channel)
        if channel in self.channels:
            self.channels.remove(channel)


class _FakeAgent:
    def __init__(self) -> None:
        self.output = _FakeOutput()


@pytest.mark.unit
class TestCloseWsConnectionAlwaysRemovesChannel:
    """channel 移除必须无条件执行。"""

    @pytest.mark.asyncio
    async def test_flush_failure_still_removes_channel(self):
        """flush() 抛错 → 仍移除 channel（原缺陷：channel 永久泄漏）。"""
        buf = _RaisingBuffer()
        agent = _FakeAgent()
        conn = _make_conn(audio_buf=buf)
        agent.output.channels.append(conn.channel)

        await _close_ws_connection(conn, agent)  # type: ignore[arg-type]

        assert buf.flush_called is True
        assert agent.output.removed == [conn.channel], "flush 抛错也必须移除 channel"
        assert agent.output.channels == [], "死连接不得留在注册表"

    @pytest.mark.asyncio
    async def test_voice_close_failure_still_removes_channel(self):
        """voice_mgr.close() 抛错 → 仍移除 channel。"""
        agent = _FakeAgent()
        conn = _make_conn(audio_buf=_OkBuffer(), voice_mgr=_RaisingVoice())
        agent.output.channels.append(conn.channel)

        await _close_ws_connection(conn, agent)  # type: ignore[arg-type]

        assert agent.output.removed == [conn.channel]
        assert agent.output.channels == []

    @pytest.mark.asyncio
    async def test_happy_path_removes_channel(self):
        """正常路径同样移除（收口不得误伤）。"""
        buf = _OkBuffer()
        agent = _FakeAgent()
        conn = _make_conn(audio_buf=buf)
        agent.output.channels.append(conn.channel)

        await _close_ws_connection(conn, agent)  # type: ignore[arg-type]

        assert buf.flush_called is True
        assert agent.output.channels == []

    @pytest.mark.asyncio
    async def test_sent_message_ids_collected_even_when_flush_raises(self):
        """flush 抛错时 voice 的 emitted ids 仍须并入——v2 断线取消依赖它。"""
        class _Voice:
            emitted_message_ids = {"msg_a", "msg_b"}

            async def close(self) -> None:
                pass

        agent = _FakeAgent()
        conn = _make_conn(audio_buf=_RaisingBuffer(), voice_mgr=_Voice())

        await _close_ws_connection(conn, agent)  # type: ignore[arg-type]

        assert conn.sent_message_ids == {"msg_a", "msg_b"}

    @pytest.mark.asyncio
    async def test_cancelled_error_still_removes_channel(self):
        """CancelledError 也不得绕过注册表清理（停机取消路径）。"""

        class _CancellingBuffer:
            async def flush(self) -> None:
                raise asyncio.CancelledError()

        agent = _FakeAgent()
        conn = _make_conn(audio_buf=_CancellingBuffer())
        agent.output.channels.append(conn.channel)

        with pytest.raises(asyncio.CancelledError):
            await _close_ws_connection(conn, agent)  # type: ignore[arg-type]

        assert agent.output.channels == [], "取消也不得泄漏 channel"
