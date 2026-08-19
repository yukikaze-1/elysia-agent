"""批次 4 Desktop 工具桥契约建模测试：tool.request/tool.status + 工具注册表 REST（marker=unit）。

对照 spec §5.2/§5.3/§9 逐字段建模：
- ToolRequestMessage（type=tool.request，Server→Client）：callId/toolName/toolVersion/runtime/
  arguments/reason/riskLevel/permissionRequired/approvalAuthority/paramHash/expiresAt/requestId
- ToolStatusMessage（type=tool.status，Client→Server）：callId/status/toolName/toolVersion/runtime/
  paramHash/output/error/attachments/executionSummary；status 判别六态 ToolStatusEnum
- ToolAttachment（{url, mimeType, label}）
- rest：ToolMetadata/ToolRegistryResponse/DesktopCallStatus（命名避开 debug.py 既有同名类——grep 确认无冲突）
"""
import pytest
from pydantic import TypeAdapter

from frontend_contract import ws_message as w
from frontend_contract.rest import DesktopCallStatus, ToolMetadata, ToolRegistryResponse


# ── ToolStatusEnum（六态判别） ────────────────────────────────────────────


@pytest.mark.unit
def test_tool_status_enum_six_values():
    """ToolStatusEnum 六态：accepted/rejected/running/result/error/cancelled。"""
    assert {e.value for e in w.ToolStatusEnum} == {
        "accepted", "rejected", "running", "result", "error", "cancelled",
    }
    assert len(list(w.ToolStatusEnum)) == 6


# ── ToolAttachment ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_tool_attachment_wire():
    """ToolAttachment：{url, mimeType, label}；label 可缺省省略。"""
    d = w.ToolAttachment(
        url="/uploads/desktop/call_1.png", mimeType="image/png", label="截图").wire_dict()
    assert d == {"url": "/uploads/desktop/call_1.png", "mimeType": "image/png", "label": "截图"}
    d2 = w.ToolAttachment(url="/uploads/desktop/call_1.txt", mimeType="text/plain").wire_dict()
    assert d2 == {"url": "/uploads/desktop/call_1.txt", "mimeType": "text/plain"}
    assert "label" not in d2


# ── ToolRequestMessage（Server→Client） ──────────────────────────────────


@pytest.mark.unit
def test_tool_request_message_wire():
    """ToolRequestMessage：type=tool.request + 全字段；reason/requestId 可缺省省略。"""
    msg = w.ToolRequestMessage(
        callId="call_abc", toolName="desktop.file.read", toolVersion="1.0.0",
        runtime="desktop", sourceCallId="tc_desktop_1",
        arguments={"path": "/home/user/doc.txt"},
        reason="用户请求分析所选文件", riskLevel="medium", permissionRequired=True,
        approvalAuthority="client", paramHash="sha256:deadbeef",
        expiresAt="2026-08-05T00:01:00+00:00", requestId="req_1",
    )
    d = msg.wire_dict()
    assert d["type"] == "tool.request"
    assert d["callId"] == "call_abc"
    assert d["toolName"] == "desktop.file.read"
    assert d["toolVersion"] == "1.0.0"
    assert d["runtime"] == "desktop"
    assert d["arguments"] == {"path": "/home/user/doc.txt"}
    assert d["reason"] == "用户请求分析所选文件"
    assert d["riskLevel"] == "medium"
    assert d["permissionRequired"] is True
    assert d["approvalAuthority"] == "client"
    assert d["paramHash"] == "sha256:deadbeef"
    assert d["expiresAt"] == "2026-08-05T00:01:00+00:00"
    assert d["requestId"] == "req_1"
    # reason/requestId 可缺省省略
    msg2 = w.ToolRequestMessage(
        callId="call_2", toolName="desktop.screen.capture", toolVersion="1.0.0",
        runtime="desktop", sourceCallId="tc_desktop_2",
        arguments={}, riskLevel="high", permissionRequired=True,
        approvalAuthority="client", paramHash="sha256:0", expiresAt="t",
    )
    d2 = msg2.wire_dict()
    assert "reason" not in d2
    assert "requestId" not in d2


@pytest.mark.unit
def test_tool_request_message_in_server_message_union():
    """ToolRequestMessage 加入 ServerMessage 判别联合（Server→Client 定向投递帧）。"""
    from typing import get_args
    members = get_args(get_args(w.ServerMessage)[0])
    assert w.ToolRequestMessage in members


# ── ToolStatusMessage（Client→Server） ───────────────────────────────────


@pytest.mark.unit
def test_tool_status_message_wire_with_attachments():
    """ToolStatusMessage：type=tool.status + status=result + attachments 递归 wire。"""
    msg = w.ToolStatusMessage(
        callId="call_abc", status=w.ToolStatusEnum.RESULT,
        toolName="desktop.screen.capture", toolVersion="1.0.0", runtime="desktop",
        paramHash="sha256:deadbeef", output="截图已捕获",
        attachments=[
            w.ToolAttachment(
                url="/uploads/desktop/call_abc.png", mimeType="image/png", label="截图"),
        ],
        executionSummary="已截取屏幕（1080p）",
    )
    d = msg.wire_dict()
    assert d["type"] == "tool.status"
    assert d["callId"] == "call_abc"
    assert d["status"] == "result"          # str Enum → wire 值
    assert d["toolName"] == "desktop.screen.capture"
    assert d["toolVersion"] == "1.0.0"
    assert d["runtime"] == "desktop"
    assert d["paramHash"] == "sha256:deadbeef"
    assert d["output"] == "截图已捕获"
    assert d["attachments"][0] == {
        "url": "/uploads/desktop/call_abc.png", "mimeType": "image/png", "label": "截图"}
    assert d["executionSummary"] == "已截取屏幕（1080p）"
    assert "error" not in d                 # None 缺省省略


@pytest.mark.unit
def test_tool_status_message_error_shape():
    """ToolStatusMessage：status=error 带 error；output/attachments 缺省省略。"""
    msg = w.ToolStatusMessage(
        callId="call_1", status=w.ToolStatusEnum.ERROR,
        toolName="desktop.file.write", toolVersion="1.0.0", runtime="desktop",
        paramHash="sha256:0", error={"code": "PERMISSION_DENIED", "message": "用户拒绝写入"},
    )
    d = msg.wire_dict()
    assert d["status"] == "error"
    assert d["error"] == {"code": "PERMISSION_DENIED", "message": "用户拒绝写入"}
    assert "output" not in d
    assert "attachments" not in d
    assert "executionSummary" not in d


@pytest.mark.unit
def test_tool_status_four_signal_states_wire():
    """ToolStatusMessage：accepted/running/rejected/cancelled 四态 wire 值。"""
    for status, expected in (("ACCEPTED", "accepted"), ("RUNNING", "running"),
                             ("REJECTED", "rejected"), ("CANCELLED", "cancelled")):
        msg = w.ToolStatusMessage(
            callId="call_1", status=getattr(w.ToolStatusEnum, status),
            toolName="desktop.file.read", toolVersion="1.0.0", runtime="desktop",
            paramHash="sha256:0",
        )
        assert msg.wire_dict()["status"] == expected


@pytest.mark.unit
def test_tool_status_message_validation():
    """ToolStatusMessage：非法 status 值校验失败（判别六态枚举约束）。"""
    with pytest.raises(Exception):
        w.ToolStatusMessage(
            callId="call_1", status="bogus", toolName="desktop.file.read",
            toolVersion="1.0.0", runtime="desktop", paramHash="sha256:0",
        )


# ── rest：ToolMetadata / ToolRegistryResponse / DesktopCallStatus ────────


@pytest.mark.unit
def test_tool_metadata_wire():
    """ToolMetadata：name/description/runtime/riskLevel/permissionRequired/approvalAuthority/
    version/inputSchema/outputSchema/available。"""
    meta = ToolMetadata(
        name="desktop.file.read", description="读取用户所选/指定文件的文本内容",
        runtime="desktop", riskLevel="medium", permissionRequired=True,
        approvalAuthority="client", version="1.0.0",
        inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        outputSchema={"type": "string", "description": "工具公开输出"},
        available=True,
    )
    d = meta.wire_dict()
    assert d == {
        "name": "desktop.file.read",
        "description": "读取用户所选/指定文件的文本内容",
        "runtime": "desktop",
        "riskLevel": "medium",
        "permissionRequired": True,
        "approvalAuthority": "client",
        "version": "1.0.0",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "outputSchema": {"type": "string", "description": "工具公开输出"},
        "available": True,
    }


@pytest.mark.unit
def test_tool_registry_response_wire():
    """ToolRegistryResponse：tools 数组 + desktopAvailable（desktop 未连接时 available=false 仍列出）。"""
    meta = ToolMetadata(
        name="desktop.file.read", description="读取文件", runtime="desktop",
        riskLevel="medium", permissionRequired=True, approvalAuthority="client",
        version="1.0.0", inputSchema={}, outputSchema={}, available=False,
    )
    d = ToolRegistryResponse(tools=[meta], desktopAvailable=False).wire_dict()
    assert d["tools"][0]["name"] == "desktop.file.read"
    assert d["tools"][0]["available"] is False
    assert d["desktopAvailable"] is False


@pytest.mark.unit
def test_desktop_call_status_wire():
    """DesktopCallStatus：callId/toolName/toolVersion/runtime/paramHash/status/createdAt/expiresAt/result/error。"""
    status = DesktopCallStatus(
        callId="call_abc", toolName="desktop.file.read", toolVersion="1.0.0",
        runtime="desktop", paramHash="sha256:deadbeef", status="succeeded",
        createdAt="2026-08-05T00:00:00+00:00", expiresAt="2026-08-05T00:02:00+00:00",
        result={"output": "文件内容", "attachments": [], "execution_summary": "已读取文件（4.2 KB）"},
    )
    d = status.wire_dict()
    assert d["callId"] == "call_abc"
    assert d["toolName"] == "desktop.file.read"
    assert d["toolVersion"] == "1.0.0"
    assert d["runtime"] == "desktop"
    assert d["paramHash"] == "sha256:deadbeef"
    assert d["status"] == "succeeded"
    assert d["createdAt"] == "2026-08-05T00:00:00+00:00"
    assert d["expiresAt"] == "2026-08-05T00:02:00+00:00"
    assert d["result"]["output"] == "文件内容"
    assert "error" not in d                 # None 缺省省略


@pytest.mark.unit
def test_desktop_call_status_status_enum():
    """DesktopCallStatus：status 限 repo 枚举 9 值（wire 六态翻译固定 + 服务端态）；非法值校验失败。"""
    for status in ("pending", "accepted", "running", "succeeded", "failed",
                   "rejected", "cancelled", "expired", "offline"):
        d = DesktopCallStatus(
            callId="c", toolName="desktop.file.read", toolVersion="1.0.0",
            runtime="desktop", paramHash="h", status=status, createdAt="t", expiresAt="t",
        )
        assert d.wire_dict()["status"] == status
    with pytest.raises(Exception):
        DesktopCallStatus(
            callId="c", toolName="desktop.file.read", toolVersion="1.0.0",
            runtime="desktop", paramHash="h", status="bogus", createdAt="t", expiresAt="t",
        )


# ── 阶段 6 工具双轨展示帧（B-6-01） ─────────────────────────────────────


@pytest.mark.unit
def test_tool_runtime_enum_values():
    """ToolRuntime 值域（backend/desktop/local_worker）。"""
    assert [r.value for r in w.ToolRuntime] == ["backend", "desktop", "local_worker"]


@pytest.mark.unit
def test_tool_started_wire_shape():
    """tool.started wire：camelCase + 必填/可缺省 + 可选字段缺省省略。"""
    msg = w.ToolStartedMessage(callId="tc_1", toolName="web.fetch", runtime=w.ToolRuntime.BACKEND)
    d = msg.wire_dict()
    assert d == {"type": "tool.started", "callId": "tc_1", "toolName": "web.fetch",
                 "runtime": "backend"}
    d2 = w.ToolStartedMessage(callId="tc_2", toolName="desktop.file.read",
                              runtime=w.ToolRuntime.DESKTOP, inputSummary="path=<redacted>",
                              generationId="gen_1", conversationId="conv_1").wire_dict()
    assert d2["inputSummary"] == "path=<redacted>"
    assert d2["generationId"] == "gen_1" and d2["conversationId"] == "conv_1"


@pytest.mark.unit
def test_tool_completed_wire_shape():
    """tool.completed wire：成功与失败（error dict）。"""
    ok = w.ToolCompletedMessage(callId="tc_1", toolName="web.fetch", runtime=w.ToolRuntime.BACKEND,
                                success=True, outputSummary="title", truncated=False).wire_dict()
    assert ok["success"] is True and ok["outputSummary"] == "title"
    err = w.ToolCompletedMessage(callId="tc_1", toolName="web.fetch", runtime=w.ToolRuntime.BACKEND,
                                 success=False, error={"code": "TOOL_ERROR", "message": "boom"},
                                 errorType="TOOL_ERROR").wire_dict()
    assert err["error"] == {"code": "TOOL_ERROR", "message": "boom"}


@pytest.mark.unit
def test_tool_messages_in_server_union():
    """两新帧入 ServerMessage 判别联合。"""
    for frame in ({"type": "tool.started", "callId": "tc_1", "toolName": "x",
                   "runtime": "backend"},
                  {"type": "tool.completed", "callId": "tc_1", "toolName": "x",
                   "runtime": "backend", "success": True}):
        TypeAdapter(w.ServerMessage).validate_python(frame)


@pytest.mark.unit
def test_tool_request_source_call_id_required():
    """tool.request.sourceCallId 缺省 → 校验失败。"""
    with pytest.raises(Exception):
        w.ToolRequestMessage(callId="call_1", toolName="desktop.file.read", toolVersion="1.0.0",
                             runtime="desktop", arguments={}, riskLevel="high",
                             permissionRequired=True, approvalAuthority="client",
                             paramHash="h", expiresAt="2026-08-19T00:00:00+00:00")
