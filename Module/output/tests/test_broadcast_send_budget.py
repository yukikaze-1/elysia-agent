# 测试文件：Module/output/tests/test_broadcast_send_budget.py
"""输出层发送预算与逐通道隔离守卫（S2-H1，marker=service）。

**修前**：`_broadcast` 逐个 `await` 通道发送、**无任何预算**；`WebSocketChannel.send_json`
直连 aiohttp，客户端握手后不读数据 → TCP 背压 → 该 await **永久挂起**。关键在于
`gather(return_exceptions=True)` 只隔离**异常**，隔离不了**挂起**——于是生产者协程
（LLM delta / TTS 音频 / 终结帧）一并挂起：`finish_turn` 不执行、FSM 停在 SPEAKING
并抑制 VAD 打断，而且**没有任何错误日志**（报告实测：`send_json` 3s 未返回）。

本文件测的是"**有界**"这件事本身，不是那 5 秒——预算被压到 50ms。
"""

import asyncio

import pytest

from Module.output.manager import (
    _CHANNEL_FAILURE_LIMIT,
    OutputManager,
)
from Module.output.protocol import ChannelCapability, OutputChannel

pytestmark = pytest.mark.service


class _Channel(OutputChannel):
    """可编程假通道：正常 / 永久挂起 / 抛异常。"""

    def __init__(self, name: str, *, hang: bool = False, boom: bool = False) -> None:
        self.name = name
        self.received: list = []
        self._hang = hang
        self._boom = boom

    @property
    def capabilities(self) -> set[ChannelCapability]:
        return {ChannelCapability.TEXT_MESSAGE}

    async def on_message(self, msg) -> None:
        if self._hang:
            await asyncio.sleep(3600)
        if self._boom:
            raise RuntimeError("boom")
        self.received.append(msg)

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fast_budget(monkeypatch):
    """发送预算压到 50ms（被测的是"有界"，不是 5s 这个具体值）。"""
    monkeypatch.setattr("Module.output.manager._CHANNEL_SEND_TIMEOUT", 0.05)


class TestSendBudget:
    @pytest.mark.asyncio
    async def test_stalled_channel_does_not_block_broadcast(self):
        """核心：一个不读数据的通道不得让整个广播挂住（修前永久挂起）。"""
        mgr = OutputManager()
        good, stalled = _Channel("good"), _Channel("stalled", hang=True)
        mgr.add_channel(good)
        mgr.add_channel(stalled)

        await asyncio.wait_for(mgr.send("x"), timeout=2.0)   # 修前：这里超时

        assert good.received == ["x"], "健康通道必须照旧收到帧（逐通道隔离）"
        assert mgr._channel_failures[stalled] == 1  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_not_evicted_before_limit(self):
        """"偶发慢一帧"不该被摘除——阈值前必须保留。"""
        mgr = OutputManager()
        stalled = _Channel("stalled", hang=True)
        mgr.add_channel(stalled)
        for _ in range(_CHANNEL_FAILURE_LIMIT - 1):
            await mgr.send("x")
        assert mgr.channels == [stalled]

    @pytest.mark.asyncio
    async def test_stalled_channel_evicted_after_limit(self):
        """连续超预算到阈值 → 摘除（否则之后每一帧都要再罚一次预算）。"""
        mgr = OutputManager()
        stalled = _Channel("stalled", hang=True)
        mgr.add_channel(stalled)
        for _ in range(_CHANNEL_FAILURE_LIMIT):
            await mgr.send("x")
        assert mgr.channels == [], "连续超预算的通道必须被摘除"
        assert stalled not in mgr._channel_failures  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_counter(self):
        """计数是**连续**的：成功一次即清零（否则会累积误杀）。"""
        mgr = OutputManager()
        stalled = _Channel("stalled", hang=True)
        mgr.add_channel(stalled)
        await mgr.send("x")
        assert mgr._channel_failures[stalled] == 1  # noqa: SLF001
        stalled._hang = False                       # noqa: SLF001 — 恢复正常
        await mgr.send("y")
        assert mgr._channel_failures == {}          # noqa: SLF001
        assert stalled.received == ["y"]

    @pytest.mark.asyncio
    async def test_exception_isolation_semantics_unchanged(self):
        """回归：**异常**级隔离语义不变——记 warning、不摘除、不影响其他通道。"""
        mgr = OutputManager()
        good, boom = _Channel("good"), _Channel("boom", boom=True)
        mgr.add_channel(good)
        mgr.add_channel(boom)

        await asyncio.wait_for(mgr.send("x"), timeout=2.0)

        assert good.received == ["x"]
        assert boom in mgr.channels, "抛异常（非挂起）不得摘除通道"
        assert mgr._channel_failures == {}  # noqa: SLF001 — 异常不计入超预算计数

    @pytest.mark.asyncio
    async def test_remove_channel_is_idempotent(self):
        """摘除机制的前提：连接层随后再移除同一个通道不得抛 ValueError。"""
        mgr = OutputManager()
        ch = _Channel("c")
        mgr.add_channel(ch)
        mgr.remove_channel(ch)
        mgr.remove_channel(ch)      # 修前：ValueError: list.remove(x): x not in list
        assert mgr.channels == []
