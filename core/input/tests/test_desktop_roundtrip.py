"""批次 4 Task 10：service 测试收敛——fake desktop 客户端安全往返 + 断连/重连 + 幂等/篡改。

覆盖 spec §11 service 矩阵缺口（Task 7/8/9 已覆盖握手/注册表/附件/ImagePart 落盘/
tool_approval 版本门控，本文件补完整往返闭环）：
- **安全往返**：electron + v2 → 多轮 tool-calling fake LLM 选 `desktop.file.read` →
  收 `tool.request`（callId/risk/paramHash/permissionRequired/approvalAuthority/expiresAt）
  → 回 `accepted → running → result` → Agent 拿 ToolResult + 落盘消息；
- **screen.capture 截图**：附件先 `POST /api/uploads/desktop/{callId}`（X-Client-Connection
  持有者校验）→ result attachments 回传 → 落盘消息含 `ImagePart`；
- **断连**：pending（未接手）断连 → 立即 resolve UNAVAILABLE，回合及时收尾；
- **断连/重连**：accepted 断连 → offline（future 挂起）+ 重连重发 result →
  `offline → succeeded`（BE-043「重传结果」闭环）；**重连不自动重投**（新连接无二次
  tool.request）；
- **重连交叠投递**：新旧 Electron WS 同时存在时，tool.request 只发给 bridge 登记的
  最新 connectionId，旧连接不执行副作用；
- **幂等**：同轮两条同参 `desktop.file.read` 并发 → 桥锁内去重仅一次投递（单 tool.request）；
- **篡改**：回传改 paramHash → 拒绝 + Agent 及时收到失败（非挂 120s 超时）；
- **tools/calls camelCase（批次 8c F9）**：`GET /api/tools/calls/{id}` result 键归
  camelCase（execution_summary→executionSummary、attachments[].mime_type→mimeType），
  repo 存储 snake 键不变，REST 边界转换。

Fake 约定（spec §11）：多轮 tool-calling fake LLM（`_ToolCallingLLM`，test_v2_generation.py）
+ `_inject_fake_storage`（已注入 DesktopCallRepository + `bridge._repo` 双引用同步，
bridge 在 Agent.__init__ 构造时绑定 repo——只换 `agent._desktop_call_repo` 不同步
`bridge._repo` 会致 service 测试写真实 Mongo）。
"""

import asyncio
import json

import pytest
from aiohttp import WSMsgType

from core.cognitive.tests.test_v2_generation import _ToolCallingLLM, _make_agent_with
from core.storage.idempotency import reset_store
from core.schema.message import ToolResultMessage
from core.schema.part import ImagePart
from Module.tool.protocol import ErrorType

pytestmark = pytest.mark.service


@pytest.fixture
def fresh_idempotency():
    """每个测试独立幂等表。"""
    reset_store()
    yield


async def _connect(client):
    """建连 v2 + electron（X-Client-Platform: electron）→ 返回 (ws, connectionId)。

    server_info 帧携带握手生成的 connectionId（Electron 附件上传 X-Client-Connection 头
    的数据源，spec §5.1）；connectionId 恒 conn_ 前缀。
    """
    ws = await client.session.ws_connect(
        client.make_url("/ws"),
        headers={"X-Protocol-Version": "2", "X-Client-Platform": "electron"},
    )
    conn_id = None
    for _ in range(8):
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=5)
        except asyncio.TimeoutError:
            break
        if m.type == WSMsgType.TEXT:
            d = json.loads(m.data)
            if d.get("type") == "server_info":
                conn_id = d.get("connectionId")
                break
    assert conn_id and conn_id.startswith("conn_"), "v2+electron 应下发 connectionId"
    return ws, conn_id


async def _send_text_wait_tool_request(ws, content: str = "帮我读取文件"):
    """发 text 消息并读帧直到 `tool.request` 出现。

    Returns:
        (tool_request_frame, frames_before)：tool_request 帧 + 其前全部帧（含连接期帧）。
    """
    req_id = f"req_{asyncio.get_running_loop().time():.6f}"
    await ws.send_json({"type": "text", "content": content, "clientRequestId": req_id})
    frames: list[dict] = []
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=30)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        frames.append(d)
        if d.get("type") == "tool.request":
            return d, frames


async def _collect_until_idle(ws, timeout: float = 30.0) -> list[dict]:
    """收帧直到 `state{idle}`（回合终态）。"""
    frames: list[dict] = []
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=timeout)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        frames.append(d)
        if d.get("type") == "state" and d.get("state") == "idle":
            return frames


async def _until(predicate, timeout: float = 5.0) -> None:
    """轮询等待条件成立（测试协调用，短超时防挂死）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("wait condition not met")
        await asyncio.sleep(0.01)


async def _wait_call_status(bridge, call_id: str, status: str, timeout: float = 5.0) -> None:
    """轮询等桥 repo 的 call 状态到达目标（await 版——lambda 不能 await）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        doc = await bridge.get_call(call_id)
        if doc is not None and doc.get("status") == status:
            return
        if loop.time() > deadline:
            raise AssertionError(f"call {call_id} 未在 {timeout}s 内到达 {status}")
        await asyncio.sleep(0.01)


async def _wait_pending_without_call(bridge, call_id: str, timeout: float = 5.0) -> None:
    """等桥 pending_calls() 不再含该 call（future 已 resolve/弹出，无 120s 冻结）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pending = await bridge.pending_calls()
        if all(c["call_id"] != call_id for c in pending):
            return
        if loop.time() > deadline:
            raise AssertionError(f"call {call_id} 仍挂在 pending_calls()——future 未 resolve")
        await asyncio.sleep(0.01)


async def _wait_pending_contains(bridge, call_id: str, timeout: float = 5.0) -> None:
    """等桥 pending_calls() 含该 call（accepted 已接手断连后 future 应仍挂起）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pending = await bridge.pending_calls()
        if any(c["call_id"] == call_id for c in pending):
            return
        if loop.time() > deadline:
            raise AssertionError(f"call {call_id} 未列入 pending_calls()——已提前 resolve")
        await asyncio.sleep(0.01)


async def _wait_agent_idle(agent, timeout: float = 5.0) -> None:
    """等 Agent 回到完全 idle（断连 interrupt 后回合及时收尾，不冻结 120s）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not agent.status.is_completely_idle:
        if loop.time() > deadline:
            raise AssertionError("Agent 未在超时内回到 idle——回合可能被冻结")
        await asyncio.sleep(0.01)


async def _receive_tool_request_within(ws, window: float) -> bool:
    """在给定窗口内收帧，若出现 tool.request 返回 True（重连不自动重投断言用）。"""
    try:
        while True:
            m = await asyncio.wait_for(ws.receive(), timeout=window)
            if m.type == WSMsgType.TEXT and json.loads(m.data).get("type") == "tool.request":
                return True
    except asyncio.TimeoutError:
        return False


def _status(tool_req: dict, status: str, **overrides) -> dict:
    """构造 tool.status wire 帧（camelCase，回传一致性字段取自 tool.request）。"""
    frame = {
        "type": "tool.status",
        "callId": tool_req["callId"],
        "status": status,
        "toolName": tool_req["toolName"],
        "toolVersion": tool_req["toolVersion"],
        "runtime": tool_req["runtime"],
        "paramHash": tool_req["paramHash"],
    }
    frame.update(overrides)
    return frame


def _desktop_result(agent, tool_name: str = "desktop.file.read") -> ToolResultMessage | None:
    """从活动会话取指定 desktop 工具的 ToolResultMessage（工具执行结果落盘断言）。"""
    for m in agent.session.messages:
        if isinstance(m, ToolResultMessage) and m.tool_name == tool_name:
            return m
    return None


class TestDesktopRoundtrip:
    """fake desktop 客户端安全往返：file.read 文本 + screen.capture 截图 ImagePart。"""

    @pytest.mark.asyncio
    async def test_file_read_roundtrip(self, aiohttp_client, fresh_idempotency):
        """electron + v2 → LLM 选 desktop.file.read → tool.request → accepted/running/result
        → Agent 拿 ToolResult + 落盘消息成功。"""
        from core.input.server import create_app

        agent = _make_agent_with(_ToolCallingLLM(
            tool_name="desktop.file.read", arguments={"path": "/tmp/a.txt"}))
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, conn_id = await _connect(client)
            tool_req, frames = await _send_text_wait_tool_request(ws, "帮我读取 /tmp/a.txt")
            # tool.request 定向投递帧完整性（spec §5.2）
            assert tool_req["toolName"] == "desktop.file.read"
            assert tool_req["callId"].startswith("call_")
            assert tool_req["toolVersion"] == "1.0.0"
            assert tool_req["runtime"] == "desktop"
            assert tool_req["arguments"] == {"path": "/tmp/a.txt"}
            assert tool_req["riskLevel"] == "medium"
            assert tool_req["permissionRequired"] is True
            assert tool_req["approvalAuthority"] == "client"
            assert tool_req["paramHash"]  # sha256 参数摘要非空
            assert tool_req["expiresAt"] > "2020"
            # B-6-05：sourceCallId 由 ctx.call_id 透传（D6-07）——字段已贯通至 wire。
            # 注意：真实 Agent 路径经 TaskScheduler._worker_loop 以**裸 base ctx** 执行工具
            # （core/task/scheduler.py:412，call_id 恒 ""，仅 trace_id 每回合 set，
            # loop.py:355）→ sourceCallId 现为 ""。非空值仅在 _execute_tools_directly 路径
            # （clone_for_call(call_id=tc.id)）成立。待 scheduler 补 call_id 贯通后收紧。
            # requestId 由 per-turn trace_id 填充（new_message_id()，非空串）；空 trace_id 的
            # 直接执行路径经 `or None` 才 wire 省略（防 requestId:""）。
            assert tool_req.get("sourceCallId") == ""
            assert (tool_req.get("requestId") or "").startswith("msg_")

            # Electron 接手：accepted → running → result
            await ws.send_json(_status(tool_req, "accepted"))
            await ws.send_json(_status(tool_req, "running"))
            await ws.send_json(_status(
                tool_req, "result", output="文件内容：hello desktop",
                executionSummary="已读取文件（4.2 KB）"))

            # 回合结束：generation.completed + state idle
            frames2 = await _collect_until_idle(ws)
            types = [f["type"] for f in frames2]
            assert "generation.completed" in types

            # Agent 拿 ToolResult + 落盘消息（TextPart 用 llm_summary = executionSummary）
            tr = await self._until_result(agent)
            assert tr.success
            assert [p.type for p in tr.content] == ["text"]
            assert tr.content[0].content == "已读取文件（4.2 KB）"
            # 桥 repo 终态 succeeded（可查），result 字段含原始 output
            doc = await agent.desktop_bridge.get_call(tool_req["callId"])
            assert doc["status"] == "succeeded"
            assert doc["result"]["output"] == "文件内容：hello desktop"
            assert doc["result"]["execution_summary"] == "已读取文件（4.2 KB）"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_overlapping_electron_connections_deliver_only_to_latest(
            self, aiohttp_client, fresh_idempotency):
        """重连交叠窗口：旧连接仍注册时，tool.request 只投递给最新连接。"""
        from core.input.server import create_app

        agent = _make_agent_with(_ToolCallingLLM(
            tool_name="desktop.file.read", arguments={"path": "/tmp/a.txt"}))
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            old_ws, old_conn = await _connect(client)
            new_ws, new_conn = await _connect(client)
            assert old_conn != new_conn
            assert agent.desktop_bridge._connection_id == new_conn  # noqa: SLF001

            tool_req, _ = await _send_text_wait_tool_request(new_ws, "读取文件")
            assert not await _receive_tool_request_within(old_ws, window=0.3)

            await new_ws.send_json(_status(
                tool_req, "result", output="ok", executionSummary="最新连接完成"))
            frames = await _collect_until_idle(new_ws)
            assert "generation.completed" in [f["type"] for f in frames]
            doc = await agent.desktop_bridge.get_call(tool_req["callId"])
            assert doc["target_connection"] == new_conn
            assert doc["status"] == "succeeded"

            await old_ws.close()
            await new_ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_screen_capture_roundtrip_image_part(
            self, aiohttp_client, fresh_idempotency, upload_dir):
        """screen.capture：附件上传 → result attachments → 落盘消息含 ImagePart。"""
        from core.input.server import create_app

        agent = _make_agent_with(_ToolCallingLLM(
            tool_name="desktop.screen.capture", arguments={}))
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, conn_id = await _connect(client)
            tool_req, frames = await _send_text_wait_tool_request(ws, "截个屏")
            assert tool_req["toolName"] == "desktop.screen.capture"
            assert tool_req["riskLevel"] == "high"
            call_id = tool_req["callId"]

            # 附件字节先经 REST 上传（持有者校验：X-Client-Connection = 握手 conn_id）
            from aiohttp import FormData
            form = FormData()
            form.add_field("file", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
                           filename="shot.png", content_type="image/png")
            resp = await client.post(
                f"/api/uploads/desktop/{call_id}",
                data=form, headers={"X-Client-Connection": conn_id})
            assert resp.status == 200, (resp.status, await resp.text())
            url = (await resp.json())["url"]
            assert url == f"/uploads/desktop/{call_id}.png"

            # 回传 result（attachments 填后端托管 url）
            await ws.send_json(_status(tool_req, "accepted"))
            await ws.send_json(_status(tool_req, "running"))
            await ws.send_json(_status(
                tool_req, "result", output="截图已捕获", executionSummary="已截取用户屏幕",
                attachments=[{"url": url, "mimeType": "image/png", "label": "截图"}]))

            frames2 = await _collect_until_idle(ws)
            assert "generation.completed" in [f["type"] for f in frames2]

            # 落盘消息含 ImagePart（回放/搜索自动吃到，spec §5.4）
            tr = await self._until_result(agent, tool_name="desktop.screen.capture")
            assert tr.success
            assert [p.type for p in tr.content] == ["text", "image"]
            img = next(p for p in tr.content if isinstance(p, ImagePart))
            assert img.src == url
            await ws.close()
        finally:
            await agent.stop()

    @pytest.fixture
    def upload_dir(self, monkeypatch, tmp_path):
        """把桌面附件落盘目录重定向到 tmp_path（防污染真实 /uploads/desktop）。"""
        d = tmp_path / "uploads" / "desktop"
        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS_DESKTOP", str(d))
        return d

    async def _until_result(self, agent, tool_name: str = "desktop.file.read",
                            timeout: float = 5.0) -> ToolResultMessage:
        """轮询等工具结果落盘（短超时——篡改/断连路径应及时而非挂 120s）。"""
        await _until(lambda: _desktop_result(agent, tool_name) is not None, timeout=timeout)
        return _desktop_result(agent, tool_name)


class TestDesktopDisconnectReconnect:
    """断连/重连（D-o）：pending 立即 UNAVAILABLE；accepted→offline→重传 result 恢复。

    注：v2 连接断开时 ws_handler 除 `on_client_disconnect`（桥标 offline + 未接手 call
    立即 resolve UNAVAILABLE）外，还会 post `InterruptData(source="disconnect")` 取消在途
    generation（批次 2 重连语义）——Agent 回合经 interrupt 及时收尾（generation.failed
    + 回到 idle），断连/重传结果的可观察断言以桥 repo 状态迁移为准（spec §4.1 迁移表 +
    BE-043「重传结果」闭环），不依赖回合内 ToolResultMessage 落盘（回合已被取消）。
    """

    @pytest.mark.asyncio
    async def test_pending_disconnect_resolves_unavailable(self, aiohttp_client,
                                                           fresh_idempotency):
        """未接手（pending）call 断连 → 桥立即 resolve UNAVAILABLE + repo offline，回合及时收尾。"""
        from core.input.server import create_app

        agent = _make_agent_with(_ToolCallingLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, conn_id = await _connect(client)
            tool_req, frames = await _send_text_wait_tool_request(ws, "读取文件")
            call_id = tool_req["callId"]
            # 未回 accepted → Electron 直接断开
            await ws.close()
            # 桥立即 resolve UNAVAILABLE：repo 标 offline + _pending 弹出（无 120s 冻结）
            await _wait_call_status(agent.desktop_bridge, call_id, "offline")
            # future 已 resolve → pending_calls() 不再含该 call（挂起时会被列入）
            await _wait_pending_without_call(agent.desktop_bridge, call_id)
            # Agent 回合经断连 interrupt 及时收尾（不冻结 120s）
            await _wait_agent_idle(agent)
            assert agent.desktop_bridge.is_available() is False
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_accepted_disconnect_reconnect_result_recovery(self, aiohttp_client,
                                                                 fresh_idempotency):
        """accepted 断连 → offline（future 挂起）+ 重连重发 result → succeeded 闭环；
        重连不自动重投（新连接无二次 tool.request）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_ToolCallingLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws1, conn1 = await _connect(client)
            tool_req, frames = await _send_text_wait_tool_request(ws1, "读取文件")
            call_id = tool_req["callId"]
            # Electron 已接手
            await ws1.send_json(_status(tool_req, "accepted"))
            # 断连（不回 result）→ 桥标 offline（accepted 已接手，future 不 resolve）
            await ws1.close()
            await _wait_call_status(agent.desktop_bridge, call_id, "offline")
            assert agent.desktop_bridge.is_available() is False
            # future 挂起（不 resolve）→ pending_calls() 仍含该 call
            await _wait_pending_contains(agent.desktop_bridge, call_id)

            # 重连（新连接，不自动重投执行请求）
            ws2, conn2 = await _connect(client)
            # 短窗口确认无二次 tool.request（重连不自动重投，BE-043）
            assert not await _receive_tool_request_within(ws2, window=0.5)
            # 重传 result（BE-043「重传结果」闭环）：offline → succeeded + future resolve
            await ws2.send_json(_status(
                tool_req, "result", output="重传恢复内容", executionSummary="重连后重传"))
            await _wait_call_status(agent.desktop_bridge, call_id, "succeeded")
            doc = await agent.desktop_bridge.get_call(call_id)
            assert doc["result"]["output"] == "重传恢复内容"
            assert doc["result"]["execution_summary"] == "重连后重传"
            # future 已 resolve → pending_calls() 不再含该 call
            await _wait_pending_without_call(agent.desktop_bridge, call_id)
            await ws2.close()
        finally:
            await agent.stop()

    async def _until_result(self, agent, tool_name: str = "desktop.file.read",
                            timeout: float = 5.0) -> ToolResultMessage:
        """轮询等工具结果落盘（短超时——断连/重传路径应及时而非挂 120s）。"""
        await _until(lambda: _desktop_result(agent, tool_name) is not None, timeout=timeout)
        return _desktop_result(agent, tool_name)


class TestDesktopIdempotencyTamper:
    """幂等复用 + 篡改拒绝（BE-043）。"""

    @pytest.mark.asyncio
    async def test_same_params_concurrent_single_deliver(self, aiohttp_client,
                                                         fresh_idempotency):
        """同轮两条同参 desktop.file.read 并发 → 桥去重仅一次投递（单 tool.request）。"""
        from core.input.server import create_app

        extra = [{"id": "call_llm_desktop_2", "type": "function",
                  "function": {"name": "desktop.file.read",
                               "arguments": json.dumps({"path": "/tmp/a.txt"})}}]
        agent = _make_agent_with(_ToolCallingLLM(
            tool_name="desktop.file.read", arguments={"path": "/tmp/a.txt"},
            extra_tool_calls=extra))
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, conn_id = await _connect(client)
            tool_req, frames = await _send_text_wait_tool_request(ws, "读取文件")
            # 两条同参调用并发经锁串行化 → 仅一次投递
            tool_requests = [f for f in frames if f.get("type") == "tool.request"]
            assert len(tool_requests) == 1, "同参并发应复用 callId 仅一次投递"

            await ws.send_json(_status(tool_req, "result", output="ok", executionSummary="完成"))
            frames2 = await _collect_until_idle(ws)
            # 两条 ToolResultMessage 都成功（共享同一结果）
            results = [m for m in agent.session.messages
                       if isinstance(m, ToolResultMessage)]
            assert len(results) == 2
            assert all(m.success for m in results)
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_tampered_paramhash_rejected_timely(self, aiohttp_client,
                                                      fresh_idempotency):
        """回传改 paramHash → 拒绝 + Agent 及时收到失败（非挂超时）+ repo failed。"""
        from core.input.server import create_app

        agent = _make_agent_with(_ToolCallingLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws, conn_id = await _connect(client)
            tool_req, frames = await _send_text_wait_tool_request(ws, "读取文件")
            call_id = tool_req["callId"]
            # 篡改：paramHash 用错误值（BE-043 参数摘要变化拒绝）
            await ws.send_json(_status(tool_req, "result", paramHash="TAMPERED", output="x"))
            # Agent 及时收到失败（短超时——立即 resolve 而非挂 120s）
            tr = await self._until_result(agent)
            assert not tr.success
            assert tr.error_type == ErrorType.TOOL_ERROR
            assert tr.error == "参数与请求不一致，已拒绝"
            doc = await agent.desktop_bridge.get_call(call_id)
            assert doc["status"] == "failed"
            assert doc["error"]["code"] == "CALL_IDEMPOTENCY_CONFLICT"
            await ws.close()
        finally:
            await agent.stop()

    async def _until_result(self, agent, tool_name: str = "desktop.file.read",
                            timeout: float = 5.0) -> ToolResultMessage:
        """轮询等工具结果落盘（短超时——篡改拒绝应立即 resolve）。"""
        await _until(lambda: _desktop_result(agent, tool_name) is not None, timeout=timeout)
        return _desktop_result(agent, tool_name)


# ============================================================================
# tools/calls REST 边界 camelCase（批次 8c F9）
# ============================================================================

@pytest.mark.asyncio
async def test_tools_call_result_camel_case(aiohttp_client):
    """GET /api/tools/calls/{id} result 键归 camelCase（executionSummary/mimeType）。

    repo 存储仍为 snake 键（execution_summary/mime_type），转换只在 REST 边界；
    测试经注入的 fake desktop repo 直接 upsert snake 键 doc 验证归一。
    """
    from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
    from core.input.server import create_app

    agent = _make_agent_with(_UsageLLM())
    await agent.start()
    await agent._desktop_call_repo.upsert({  # noqa: SLF001
        "call_id": "call_1", "tool_name": "t", "tool_version": "1", "runtime": "desktop",
        "arguments_hash": "h", "status": "succeeded", "created_at": "t", "expires_at": "t2",
        "result": {"output": "o", "execution_summary": "s",
                   "attachments": [{"url": "u", "mime_type": "image/png", "label": "l"}]},
    })
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        resp = await client.get("/api/tools/calls/call_1")
        data = await resp.json()
        assert data["result"]["executionSummary"] == "s"
        assert data["result"]["attachments"][0]["mimeType"] == "image/png"
    finally:
        await agent.stop()


# ============================================================================
# capabilities 广播版本门控（批次 8c F15）
# ============================================================================

@pytest.mark.asyncio
async def test_broadcast_capabilities_skips_v2():
    """broadcast_capabilities 只重播 v1 通道；v2 通道跳过（server_info 取代 capabilities）。

    v1 浏览器通道（无头）恒 protocol_version=1 → 保留重播；v2 Electron 建连收
    server_info 后不再收 capabilities（spec F15）。真实 Agent + 假 ws 通道——
    WebSocketChannel._send_json 只调 self._ws.send_json，假 ws 可捕获。
    """
    from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
    from core.services.capabilities import broadcast_capabilities
    from Module.output import WebSocketChannel

    class _FakeWS:
        def __init__(self):
            self.sent = []

        async def send_json(self, d):
            self.sent.append(d)

    agent = _make_agent_with(_UsageLLM())
    v1 = WebSocketChannel(_FakeWS(), protocol_version=1)
    v2 = WebSocketChannel(_FakeWS(), protocol_version=2)
    agent.output._channels = [v1, v2]  # noqa: SLF001
    await broadcast_capabilities(agent)
    assert len(v1._ws.sent) == 1   # v1 收到 capabilities
    assert len(v2._ws.sent) == 0   # v2 跳过
