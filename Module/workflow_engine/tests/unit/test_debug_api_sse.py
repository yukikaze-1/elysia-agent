"""WorkflowDebugAPI SSE 传输层测试（2026-09-12）。

守两件事：

1. **keepalive 注释行**：handler 停在 `q.get()` 上时无法感知对端已离开，只有写失败
   才会走 `finally` → `unsubscribe` → 停轮询。每 `_SSE_KEEPALIVE_INTERVAL` 秒写一行
   `: keepalive` 强制一次写，把「关页后闲置轮询多跑 30s」压到该间隔内。
   它是**注释**（SSE 规范要求客户端忽略 `:` 开头的行），不是业务事件。
2. **断连确实停掉轮询**：端到端——客户端关闭 → 轮询协程归零并停止。
"""

import asyncio

import pytest
from aiohttp import web

from Module.workflow_engine import debug_api as debug_api_module
from Module.workflow_engine.debug_api import WorkflowDebugAPI

pytestmark = pytest.mark.service


class _FakeWorkflowEngine:
    """最小 WorkflowEngine 桩。"""

    def __init__(self) -> None:
        self.snapshot_calls = 0

    def _get_debug_snapshot(self) -> dict:
        self.snapshot_calls += 1
        return {"tasks": []}


async def _serve(api: WorkflowDebugAPI, aiohttp_client):
    """起一个只挂 SSE 端点的 aiohttp app，返回 client。"""
    app = web.Application()
    app.router.add_get("/stream", api.sse_handler)
    return await aiohttp_client(app)


async def _read_until(resp, needle: bytes, *, budget: float = 3.0) -> bytes:
    """持续读直到正文包含 needle 或超时；返回已读到的全部字节。"""
    seen = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while needle not in seen and loop.time() < deadline:
        try:
            chunk = await asyncio.wait_for(resp.content.read(4096), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if not chunk:
            break
        seen += chunk
    return seen


class TestSseKeepalive:
    @pytest.mark.asyncio
    async def test_keepalive_comment_written_when_idle(self, aiohttp_client, monkeypatch):
        """无业务事件时仍周期性写 `: keepalive`（强制一次写以探测断连）。"""
        monkeypatch.setattr(debug_api_module, "_SSE_KEEPALIVE_INTERVAL", 0.05)
        api = WorkflowDebugAPI(_FakeWorkflowEngine())
        await api.start()
        client = await _serve(api, aiohttp_client)
        try:
            resp = await client.get("/stream")
            assert resp.status == 200
            body = await _read_until(resp, b": keepalive")
            assert b": keepalive\n\n" in body, f"未收到 keepalive 注释行，实收 {body[:200]!r}"
            resp.close()
        finally:
            await api.stop()

    @pytest.mark.asyncio
    async def test_keepalive_is_comment_not_event(self, aiohttp_client, monkeypatch):
        """keepalive 必须是注释行——`:` 开头，SSE 客户端按规范忽略。"""
        monkeypatch.setattr(debug_api_module, "_SSE_KEEPALIVE_INTERVAL", 0.05)
        api = WorkflowDebugAPI(_FakeWorkflowEngine())
        await api.start()
        client = await _serve(api, aiohttp_client)
        try:
            resp = await client.get("/stream")
            body = await _read_until(resp, b": keepalive")
            assert b"event: keepalive" not in body
            # 首帧仍是订阅时的全量快照，keepalive 不插队业务事件
            assert body.startswith(b"event: snapshot\n")
            resp.close()
        finally:
            await api.stop()

    @pytest.mark.asyncio
    async def test_snapshot_arrives_before_first_keepalive(self, aiohttp_client, monkeypatch):
        """keepalive 间隔远大于订阅延迟时，首帧绝不迟到。"""
        monkeypatch.setattr(debug_api_module, "_SSE_KEEPALIVE_INTERVAL", 30.0)
        api = WorkflowDebugAPI(_FakeWorkflowEngine())
        await api.start()
        client = await _serve(api, aiohttp_client)
        try:
            resp = await client.get("/stream")
            body = await _read_until(resp, b"event: snapshot", budget=2.0)
            assert b"event: snapshot" in body
            resp.close()
        finally:
            await api.stop()


class TestDisconnectStopsPolling:
    @pytest.mark.asyncio
    async def test_client_close_stops_poll_loop(self, aiohttp_client, monkeypatch):
        """端到端：客户端断开 → 轮询停止（不再有人为空转付成本）。"""
        monkeypatch.setattr(debug_api_module, "_SSE_KEEPALIVE_INTERVAL", 0.05)
        monkeypatch.setattr(debug_api_module, "_POLL_INTERVAL", 0.01)
        ts = _FakeWorkflowEngine()
        api = WorkflowDebugAPI(ts)
        await api.start()
        client = await _serve(api, aiohttp_client)
        try:
            resp = await client.get("/stream")
            await _read_until(resp, b"event: snapshot")
            assert api._poll_task is not None, "订阅后轮询应已启动"

            resp.close()   # 客户端断开

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while api._poll_task is not None and loop.time() < deadline:
                await asyncio.sleep(0.05)
            assert api._poll_task is None, "客户端断开后轮询未停止"
            assert ts.snapshot_calls > 0
        finally:
            await api.stop()

    @pytest.mark.asyncio
    async def test_shutdown_notifies_subscriber_and_stops(self, aiohttp_client, monkeypatch):
        """stop() 推 sentinel 让 handler 退出，并停掉轮询。"""
        monkeypatch.setattr(debug_api_module, "_POLL_INTERVAL", 0.01)
        api = WorkflowDebugAPI(_FakeWorkflowEngine())
        await api.start()
        client = await _serve(api, aiohttp_client)
        resp = await client.get("/stream")
        await _read_until(resp, b"event: snapshot")

        await api.stop()
        assert api._poll_task is None
        assert api._subscribers == {}
        resp.close()


class TestStopIsNonBlocking:
    """S3-05：`stop()` 绝不能被慢/半开订阅者卡住——它是关停路径上的一步。

    原实现 `await q.put(_SENTINEL)`：队列 maxsize=256，一个不读数据的 SSE 客户端
    就能把队列打满，于是**关停永久挂起**。广播路径早就把满队列当可丢弃
    （`put_nowait` + QueueFull 跳过），停止路径必须同理。
    """

    @pytest.mark.asyncio
    async def test_stop_returns_with_a_full_subscriber_queue(self):
        api = WorkflowDebugAPI(_FakeWorkflowEngine())
        await api.start()
        _sid, q = await api.subscribe()
        # 打满队列（模拟半开客户端：TCP 窗口满 → handler 不再 drain）
        while not q.full():
            q.put_nowait({"event": "tick", "data": {}})
        assert q.full()

        # 必须在有限时间内返回（挂起的话这里会超时）
        await asyncio.wait_for(api.stop(), timeout=2.0)
        assert api._subscribers == {}

    @pytest.mark.asyncio
    async def test_full_queue_still_receives_the_sentinel(self):
        """腾空队列是对的，但**不能连 sentinel 一起吞掉**——否则 handler 永不退出。"""
        api = WorkflowDebugAPI(_FakeWorkflowEngine())
        await api.start()
        _sid, q = await api.subscribe()
        while not q.full():
            q.put_nowait({"event": "tick", "data": {}})

        await asyncio.wait_for(api.stop(), timeout=2.0)

        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
        assert debug_api_module._SENTINEL in seen, (
            "队列满时 sentinel 没送到 → SSE handler 不会退出（连接吊在那儿）")
