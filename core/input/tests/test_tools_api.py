"""批次 4 Task 8：REST 注册表导出 + 附件上传（BE-042/043）。

覆盖 spec §6（注册表导出）+ §5.4（附件上传持有者校验，marker=service）：
- GET /api/tools：16 工具（4 desktop runtime=desktop/risk/approvalAuthority=client/available
  + 12 backend runtime=backend/available）；desktopAvailable 随 bridge 连接状态翻转；
  认证 401（AGENT_ACCESS_TOKEN 配置时匿名拒）。
- GET /api/tools/calls/{callId}：正常返回 DesktopCallStatus（status 用 repo 枚举，
  expired 如实返回）；未知 callId → 404 CALL_NOT_FOUND。
- POST /api/uploads/desktop/{callId}：正常 → url + 落盘 /uploads/desktop/<callId>.<ext>；
  未知 callId → 404；非 target 连接 → 403；终态 call → 拒绝（403，防伪附件）。
"""


import pytest
from aiohttp import FormData

from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
from core.storage.idempotency import reset_store
from Module.tool.protocol import to_llm_name


@pytest.fixture
def fresh_idempotency():
    """每个测试独立幂等表。"""
    reset_store()
    yield


# 4 个 desktop 工具名（desktop.command.run 不注册，spec §四.7）
DESKTOP_NAMES = {
    "desktop.file.select",
    "desktop.file.read",
    "desktop.file.write",
    "desktop.screen.capture",
}
# definitions() 暴露脱点名（OpenAI 禁点号，desktop.* → desktop_*）
LLM_DESKTOP_NAMES = {to_llm_name(n) for n in DESKTOP_NAMES}


def _call_doc(call_id, *, status="running", connection="conn_test"):
    """构造 desktop 调用文档（repo schema，spec §4.1）。"""
    return {
        "call_id": call_id,
        "tool_name": "desktop.file.read",
        "tool_version": "1.0.0",
        "runtime": "desktop",
        "arguments": {"path": "/tmp/a.txt"},
        "arguments_hash": "sha256abc",
        "reason": "测试",
        "risk_level": "medium",
        "permission_required": True,
        "approval_authority": "client",
        "status": status,
        "target_connection": connection,
        "created_at": "2026-08-05T00:00:00",
        "expires_at": "2026-08-05T01:00:00",
        "updated_at": "2026-08-05T00:00:00",
        "result": None,
        "error": None,
    }


async def _upsert(agent, doc):
    """把调用文档写入注入的 desktop repo（进程内缓存，读权威）。"""
    repo = agent.desktop_bridge._repo  # noqa: SLF001 — 测试内直读注入 repo
    await repo.upsert(doc)


@pytest.mark.service
class TestToolsRegistry:
    """GET /api/tools 注册表导出。"""

    @pytest.mark.asyncio
    async def test_registry_lists_16_tools_with_desktop_metadata(
            self, aiohttp_client, fresh_idempotency):
        """返回 4 desktop（runtime=desktop/risk/approvalAuthority=client）+ 12 backend。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            resp = await client.get("/api/tools")
            assert resp.status == 200
            data = await resp.json()
            assert data["desktopAvailable"] is False  # 无 desktop 连接
            tools = {t["name"]: t for t in data["tools"]}
            assert len(tools) == 16
            # 4 desktop：runtime=desktop、approvalAuthority=client、available=False（未连接）
            for name in DESKTOP_NAMES:
                t = tools[name]
                assert t["runtime"] == "desktop"
                assert t["approvalAuthority"] == "client"
                assert t["available"] is False
                assert t["version"] == "1.0.0"
                assert t["riskLevel"] in ("safe", "medium", "high")
                assert isinstance(t["permissionRequired"], bool)
                assert isinstance(t["inputSchema"], dict)
                assert isinstance(t["outputSchema"], dict)
            # D-g risk 标注
            assert tools["desktop.file.select"]["riskLevel"] == "safe"
            assert tools["desktop.file.read"]["riskLevel"] == "medium"
            assert tools["desktop.file.write"]["riskLevel"] == "high"
            assert tools["desktop.screen.capture"]["riskLevel"] == "high"
            # desktop.screen.capture outputSchema 为 object（含 attachments）
            assert tools["desktop.screen.capture"]["outputSchema"]["type"] == "object"
            # 12 backend：runtime=backend、available=True、无 desktop.command.run
            backend = {n: t for n, t in tools.items() if n not in DESKTOP_NAMES}
            assert len(backend) == 12
            assert all(t["runtime"] == "backend" for t in backend.values())
            assert all(t["available"] is True for t in backend.values())
            assert "desktop.command.run" not in tools
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_desktop_available_flips_with_bridge(
            self, aiohttp_client, fresh_idempotency):
        """desktopAvailable 随 bridge 状态翻转；连接后 desktop 工具 available=True。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            # 未连接：desktopAvailable False
            data = await (await client.get("/api/tools")).json()
            assert data["desktopAvailable"] is False
            # 模拟 Electron 握手：bridge 标 connected
            await agent.desktop_bridge.on_client_connect("conn_x")
            data = await (await client.get("/api/tools")).json()
            assert data["desktopAvailable"] is True
            tools = {t["name"]: t for t in data["tools"]}
            for name in DESKTOP_NAMES:
                assert tools[name]["available"] is True
            # 断开：回到 False
            await agent.desktop_bridge.on_client_disconnect("conn_x")
            data = await (await client.get("/api/tools")).json()
            assert data["desktopAvailable"] is False
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_auth_401_anonymous(self, aiohttp_client, fresh_idempotency):
        """配 AGENT_ACCESS_TOKEN 后匿名 GET /api/tools → 401 AUTH_REQUIRED。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False, access_token="secret")
        client = await aiohttp_client(app)
        try:
            resp = await client.get("/api/tools")
            assert resp.status == 401
            body = await resp.json()
            assert body["code"] == "AUTH_REQUIRED"
            # 有效 token 放行
            resp = await client.get(
                "/api/tools", headers={"Authorization": "Bearer secret"})
            assert resp.status == 200
        finally:
            await agent.stop()


@pytest.mark.service
class TestLlmExposureFilter:
    """D-j：desktop 工具仅 desktop 客户端连接时暴露给 LLM。

    definitions()（LLM function schema）与 plugin_list（工具面板）双断言——
    desktop 未连接时 definitions 移除 desktop 工具（避免 LLM 选不可用工具）；
    plugin_list 仍列出（available=false，客户端知晓存在但不可调，spec §3.2）。
    """

    @pytest.mark.asyncio
    async def test_definitions_and_plugin_list_gated_by_bridge(
            self, aiohttp_client, fresh_idempotency):
        """连接/断连 → definitions 含/不含 desktop 工具 + plugin_list available 翻转。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            adapter = agent.tool_adapter
            manager = adapter._manager  # noqa: SLF001 — 测试内直读 manager 断言 plugin_list

            # 未连接：definitions 无 desktop 工具；plugin_list 列出 desktop（available=False）
            defs = [d["function"]["name"] for d in adapter.definitions()]
            assert not (LLM_DESKTOP_NAMES & set(defs))
            assert "read_file" in defs and "write_file" in defs  # backend 恒在
            plugs = {p["name"]: p for p in manager.plugin_list}
            for name in DESKTOP_NAMES:
                assert name in plugs, "plugin_list 仍列出 desktop 工具（存在但不可用）"
                assert plugs[name]["available"] is False
                assert plugs[name]["runtime"] == "desktop"
            assert all(plugs[n]["available"] is True for n in plugs
                       if n not in DESKTOP_NAMES)

            # 连接：definitions 含 desktop 工具；plugin_list available=True
            await agent.desktop_bridge.on_client_connect("conn_x")
            agent.sync_desktop_available()  # noqa: SLF001 — 组合根私有同步入口
            defs = [d["function"]["name"] for d in adapter.definitions()]
            assert all(n in defs for n in LLM_DESKTOP_NAMES)
            plugs = {p["name"]: p for p in manager.plugin_list}
            assert all(plugs[n]["available"] is True for n in DESKTOP_NAMES)
            assert "desktop.command.run" not in defs  # spec §四.7 不注册

            # 断开：definitions 移除 desktop 工具；plugin_list 回到 available=False
            await agent.desktop_bridge.on_client_disconnect("conn_x")
            agent.sync_desktop_available()  # noqa: SLF001
            defs = [d["function"]["name"] for d in adapter.definitions()]
            assert not (LLM_DESKTOP_NAMES & set(defs))
            plugs = {p["name"]: p for p in manager.plugin_list}
            assert all(plugs[n]["available"] is False for n in DESKTOP_NAMES)
        finally:
            await agent.stop()


@pytest.mark.service
class TestToolsCallStatus:
    """GET /api/tools/calls/{callId} 状态查询。"""

    @pytest.mark.asyncio
    async def test_call_status_returns_repo_enum(self, aiohttp_client, fresh_idempotency):
        """正常：返回 DesktopCallStatus（status 用 repo 枚举 running，字段完整）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await _upsert(agent, _call_doc("call_status_ok", status="running"))
            resp = await client.get("/api/tools/calls/call_status_ok")
            assert resp.status == 200
            data = await resp.json()
            assert data["callId"] == "call_status_ok"
            assert data["toolName"] == "desktop.file.read"
            assert data["toolVersion"] == "1.0.0"
            assert data["runtime"] == "desktop"
            assert data["paramHash"] == "sha256abc"
            assert data["status"] == "running"  # repo 枚举
            assert data["createdAt"] and data["expiresAt"]
            # result/error 为 None 时 wire_dict 缺省省略（exclude_none=True）
            assert data.get("result") is None
            assert data.get("error") is None
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_call_status_expired_returned_truthfully(
            self, aiohttp_client, fresh_idempotency):
        """超时/过期态如实返回 expired（不抛 408，spec §6）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await _upsert(agent, _call_doc("call_expired", status="expired"))
            resp = await client.get("/api/tools/calls/call_expired")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "expired"
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_call_status_unknown_404(self, aiohttp_client, fresh_idempotency):
        """未知 callId → 404 CALL_NOT_FOUND。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            resp = await client.get("/api/tools/calls/call_unknown")
            assert resp.status == 404
            body = await resp.json()
            assert body["code"] == "CALL_NOT_FOUND"
        finally:
            await agent.stop()


@pytest.mark.service
class TestDesktopUpload:
    """POST /api/uploads/desktop/{callId} 附件上传（持有者校验）。"""

    @pytest.fixture
    def upload_dir(self, monkeypatch, tmp_path):
        """把桌面附件落盘目录重定向到 tmp_path。"""
        d = tmp_path / "uploads" / "desktop"
        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS_DESKTOP", str(d))
        return d

    async def _post(self, client, call_id, *, connection, filename="shot.png",
                    content_type="image/png", body=None):
        """构造浏览器同款 multipart 并 POST。"""
        form = FormData()
        form.add_field("file", body or (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
                       filename=filename, content_type=content_type)
        return await client.post(
            f"/api/uploads/desktop/{call_id}",
            data=form,
            headers={"X-Client-Connection": connection},
        )

    @pytest.mark.asyncio
    async def test_upload_ok_returns_url_and_lands_on_disk(
            self, aiohttp_client, fresh_idempotency, upload_dir):
        """正常：返回 {url: /uploads/desktop/<callId>.png} + 文件落盘。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await _upsert(agent, _call_doc("call_up_ok", status="running",
                                           connection="conn_up"))
            resp = await self._post(client, "call_up_ok", connection="conn_up")
            assert resp.status == 200, (resp.status, await resp.text())
            data = await resp.json()
            assert data["url"] == "/uploads/desktop/call_up_ok.png"
            assert (upload_dir / "call_up_ok.png").exists()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_upload_text_txt_accepted_for_desktop_read_attachment(
            self, aiohttp_client, fresh_idempotency, upload_dir):
        """desktop.file.read >64 KiB 附件回传：text/plain .txt 必须被接受并落盘。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await _upsert(agent, _call_doc("call_up_txt", status="running",
                                           connection="conn_up"))
            resp = await self._post(client, "call_up_txt", connection="conn_up",
                                    filename="large-read.txt",
                                    content_type="text/plain",
                                    body=b"hello text attachment")
            assert resp.status == 200, (resp.status, await resp.text())
            data = await resp.json()
            assert data["url"] == "/uploads/desktop/call_up_txt.txt"
            assert (upload_dir / "call_up_txt.txt").exists()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_upload_unknown_call_404(self, aiohttp_client, fresh_idempotency,
                                           upload_dir):
        """未知 callId → 404 CALL_NOT_FOUND（不落盘）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            resp = await self._post(client, "call_noexist", connection="conn_x")
            assert resp.status == 404
            body = await resp.json()
            assert body["code"] == "CALL_NOT_FOUND"
            assert not upload_dir.exists() or not list(upload_dir.iterdir())
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_upload_wrong_connection_403(self, aiohttp_client, fresh_idempotency,
                                               upload_dir):
        """上传连接 != target_connection → 403 INVALID_REQUEST（不落盘）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await _upsert(agent, _call_doc("call_owner", status="running",
                                           connection="conn_owner"))
            resp = await self._post(client, "call_owner", connection="conn_intruder")
            assert resp.status == 403
            body = await resp.json()
            assert body["code"] == "INVALID_REQUEST"
            assert not upload_dir.exists() or not list(upload_dir.iterdir())
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_upload_terminal_call_rejected(self, aiohttp_client, fresh_idempotency,
                                                 upload_dir):
        """终态 call（succeeded）拒绝接收附件（防伪附件，不落盘）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await _upsert(agent, _call_doc("call_done", status="succeeded",
                                           connection="conn_done"))
            resp = await self._post(client, "call_done", connection="conn_done")
            assert resp.status == 403
            body = await resp.json()
            assert body["code"] == "INVALID_REQUEST"
            assert not upload_dir.exists() or not list(upload_dir.iterdir())
        finally:
            await agent.stop()
