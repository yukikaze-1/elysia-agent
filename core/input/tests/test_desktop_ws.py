"""批次 4 Task 7：WS 协议扩展（X-Client-Platform 握手 + tool.status 路由 + 重放门控）。

覆盖 spec §5.1/§5.2/§5.3 + §11 service 层（fake desktop 客户端安全往返）：
- v2 + electron 握手 → bridge.on_client_connect → is_available() True；断开 → False；
- `tool.status` 消息 → bridge.handle_status 被调（仅 v2 连接）；
- 负面隔离（review 修正，交付安全边界）：
  (a) v1 连接带伪冒 `X-Client-Platform: electron` 头 → on_client_connect 不被调、
      `tool.status` 忽略；
  (b) v2 无 platform 头（web）连接 → on_client_connect 不被调；
- server_info 连接帧 `features.desktop_runtime is True`（spec §11 回归项）；
- pending 审批重放版本门控：v2 收 enrich 版（+ summary/runtime/version/riskLevel/
  permissionRequired/approvalAuthority），v1 收原样 base（逐字节不变）。
"""

import asyncio
import json

import pytest
from aiohttp import WSMsgType

from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
from core.input.idempotency import reset_store


@pytest.fixture
def fresh_idempotency():
    """每个测试独立幂等表。"""
    reset_store()
    yield


async def _connect(client, headers, *, collect: bool = False, cap_type: str = "",
                   want_command: str = ""):
    """建连并（可选）收集建连帧直到 cap 帧 + 目标 command 出现。

    Returns:
        (ws, frames)：frames 为 None 表示未收集（仅排空到 cap 帧）。
    """
    ws = await client.session.ws_connect(client.make_url("/ws"), headers=headers)
    if not collect:
        for _ in range(5):
            try:
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    t = json.loads(m.data).get("type")
                    if t == cap_type:
                        break
            except asyncio.TimeoutError:
                break
        return ws, None
    frames: list[dict] = []
    seen_cap = False
    for _ in range(40):
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=3)
        except asyncio.TimeoutError:
            break
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        frames.append(d)
        if d.get("type") == cap_type:
            seen_cap = True
        if seen_cap and want_command and d.get("command") == want_command:
            break
    return ws, frames


@pytest.mark.service
class TestDesktopWsHandshake:
    """正向：v2 + electron 握手 + tool.status 路由。"""

    @pytest.mark.asyncio
    async def test_v2_electron_connect_marks_bridge_available(self, aiohttp_client, fresh_idempotency):
        """v2 + electron 握手 → bridge.is_available() True；断开 → False。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            assert agent.desktop_bridge.is_available() is False
            ws, _ = await _connect(
                client, {"X-Protocol-Version": "2", "X-Client-Platform": "electron"},
                cap_type="server_info")
            assert agent.desktop_bridge.is_available() is True
            assert agent.desktop_bridge._connection_id.startswith("conn_")  # noqa: SLF001
            await ws.close()
            # 断开 → finally 调 on_client_disconnect → available False
            for _ in range(50):
                if not agent.desktop_bridge.is_available():
                    break
                await asyncio.sleep(0.1)
            assert agent.desktop_bridge.is_available() is False
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_electron_tool_status_routes_to_bridge(self, aiohttp_client, fresh_idempotency):
        """v2 + electron 发 tool.status → bridge.handle_status 被调（bridge 记录）。"""
        from unittest.mock import AsyncMock, patch

        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, _ = await _connect(
                client, {"X-Protocol-Version": "2", "X-Client-Platform": "electron"},
                cap_type="server_info")
            frame = {
                "type": "tool.status", "callId": "call_redtest",
                "status": "accepted", "toolName": "desktop.file.read",
                "toolVersion": "1.0.0", "runtime": "desktop", "paramHash": "abc",
            }
            with patch.object(agent.desktop_bridge, "handle_status",
                              new=AsyncMock(return_value=True)) as m:
                await ws.send_json(frame)
                for _ in range(20):
                    if m.await_count:
                        break
                    await asyncio.sleep(0.05)
                assert m.await_count == 1, "handle_status 应被调用一次"
                sent = m.await_args.args[0]
                assert sent["callId"] == "call_redtest"
                assert sent["status"] == "accepted"
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestDesktopWsNegativeIsolation:
    """负面隔离回归：v1 伪冒 / v2 web 不收 desktop 语义 + server_info feature。"""

    @pytest.mark.asyncio
    async def test_v1_forged_platform_header_ignored(self, aiohttp_client, fresh_idempotency):
        """v1 连接带伪冒 platform 头 → on_client_connect 不被调、tool.status 忽略。"""
        from unittest.mock import AsyncMock, patch

        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, _ = await _connect(
                client, {"X-Client-Platform": "electron"},  # 无 X-Protocol-Version → v1
                cap_type="capabilities")
            assert agent.desktop_bridge.is_available() is False, "v1 不应解析 platform 头"
            with patch.object(agent.desktop_bridge, "handle_status",
                              new=AsyncMock(return_value=True)) as m:
                await ws.send_json({"type": "tool.status", "callId": "call_v1forged"})
                await asyncio.sleep(0.3)
                assert m.await_count == 0, "v1 连接不应处理 tool.status"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_web_no_platform_no_connect(self, aiohttp_client, fresh_idempotency):
        """v2 无 platform 头（web）→ on_client_connect 不被调。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, _ = await _connect(client, {"X-Protocol-Version": "2"}, cap_type="server_info")
            assert agent.desktop_bridge.is_available() is False, "web 连接不应标 desktop available"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_server_info_desktop_runtime_true(self, aiohttp_client, fresh_idempotency):
        """server_info 连接帧 features.desktop_runtime is True（spec §11 回归项）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, frames = await _connect(client, {"X-Protocol-Version": "2"},
                                        collect=True, cap_type="server_info")
            info = next(f for f in frames if f.get("type") == "server_info")
            assert info["features"]["desktop_runtime"] is True
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestDesktopWsApprovalReplay:
    """pending 审批重放版本门控：v2 enrich / v1 原样。"""

    @pytest.mark.asyncio
    async def test_replay_v2_enriched_v1_plain(self, aiohttp_client, fresh_idempotency):
        """有挂起审批时：v2 建连收 enrich 版 tool_approval，v1 收原样 base。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        # 手动注册一条 pending 审批（write_file 元数据可 enrich：risk=high, backend）
        pipeline = agent.tool_adapter._manager._pipeline  # noqa: SLF001
        params = {"path": "data/sandbox/a.txt", "content": "hello"}
        pipeline._hitl_requests["req_replay"] = (  # noqa: SLF001
            asyncio.Event(), "write_file", params)
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            # v2 连接 → enrich 版
            ws2, frames2 = await _connect(client, {"X-Protocol-Version": "2"},
                                          collect=True, cap_type="server_info",
                                          want_command="tool_approval")
            approvals2 = [f for f in frames2 if f.get("command") == "tool_approval"]
            assert len(approvals2) == 1
            p2 = approvals2[0]["params"]
            assert p2["request_id"] == "req_replay"
            assert p2["tool"] == "write_file"
            assert p2["params"] == str(params)[:200]
            manager = agent.tool_adapter._manager  # noqa: SLF001
            assert p2["summary"] == manager._build_param_summary(params)  # noqa: SLF001
            assert p2["runtime"] == "backend"
            assert p2["version"] == "1.0.0"
            assert p2["riskLevel"] == "high"
            assert p2["permissionRequired"] is True
            assert p2["approvalAuthority"] == "backend"
            await ws2.close()

            # v1 连接 → 原样 base（逐字节不变）
            ws1, frames1 = await _connect(client, {},
                                          collect=True, cap_type="capabilities",
                                          want_command="tool_approval")
            approvals1 = [f for f in frames1 if f.get("command") == "tool_approval"]
            assert len(approvals1) == 1
            p1 = approvals1[0]["params"]
            assert set(p1.keys()) == {"tool", "params", "request_id"}
            assert p1["request_id"] == "req_replay"
            assert p1["tool"] == "write_file"
            assert p1["params"] == str(params)[:200]
            await ws1.close()
        finally:
            await agent.stop()
