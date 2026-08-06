"""DesktopToolBridge 发送连接校验测试（批次 8a Task 2 / D2，marker=service）。

覆盖（brief Step 1 + spec §4.2/D2 发送连接校验 + 离线重连例外）：
- 错连接提交 tool.status → 拒绝（sender_connection_id != target_connection 且非
  离线重传例外 → return False，future 不 resolve）；
- 断连重连后：调用 status=offline、target 是旧 conn id，当前已连接 Electron 重传
  结果 → 放行 + repo 归属迁移到新连接（target_connection → 新 conn id）；
- offline 调用 + 非当前连接提交 → 仍拒绝（不能借 offline 状态伪装）。

构造沿用 test_bridge.py 的 FakeDesktopRepo（镜像 DesktopCallRepository 的 mark
迁移守卫语义：offline → 终态合法）；`valid_status` 是 wire 帧构造（ToolStatusMessage
camelCase）。
"""

import pytest

from core.desktop.bridge import DesktopToolBridge
from core.desktop.tests.test_bridge import FakeDesktopRepo
from core.schema.utils import now_iso

pytestmark = pytest.mark.service


# ============================================================================
# 构造辅助
# ============================================================================

def valid_status(call_id: str, **extra) -> dict:
    """构造 tool.status wire 帧（camelCase，对齐 ToolStatusMessage；默认 result 终态）。"""
    frame = {
        "type": "tool.status",
        "callId": call_id,
        "status": "result",
        "toolName": "desktop.file.read",
        "toolVersion": "1.0.0",
        "runtime": "desktop",
        "paramHash": "sha256-abc",
    }
    frame.update(extra)
    return frame


def repo_stub_with_call(*, call_id: str, target_connection: str,
                        status: str = "pending") -> FakeDesktopRepo:
    """构造预置一条调用文档的 fake repo（对齐 request_and_wait 组装 doc 的字段集）。"""
    repo = FakeDesktopRepo()
    repo.calls[call_id] = {
        "call_id": call_id,
        "tool_name": "desktop.file.read",
        "tool_version": "1.0.0",
        "runtime": "desktop",
        "arguments": {"path": "/tmp/a.txt"},
        "arguments_hash": "sha256-abc",
        "reason": "测试",
        "risk_level": "medium",
        "permission_required": True,
        "approval_authority": "client",
        "status": status,
        "target_connection": target_connection,
        "created_at": now_iso(),
        "expires_at": now_iso(),
        "updated_at": now_iso(),
        "result": None,
        "error": None,
    }
    return repo


# ============================================================================
# D2 发送连接校验
# ============================================================================

class TestSenderConnectionCheck:
    @pytest.mark.asyncio
    async def test_handle_status_rejects_wrong_sender(self):
        """错连接提交 → 拒绝（sender_connection_id != target，非离线重传例外）。"""
        bridge = DesktopToolBridge(repo=repo_stub_with_call(
            call_id="c1", target_connection="conn_real"))
        ok = await bridge.handle_status(
            valid_status(call_id="c1"), sender_connection_id="conn_other")
        assert ok is False
        # repo 未被篡改路径触碰（无 result 落库、状态仍 pending）
        doc = await bridge._repo.get("c1")
        assert doc["status"] == "pending"
        assert doc["result"] is None

    @pytest.mark.asyncio
    async def test_handle_status_allows_offline_resend_after_reconnect(self):
        """断连重连后：offline 调用 + 当前已连接 Electron 重传 → 放行 + 归属迁移。"""
        bridge = DesktopToolBridge(repo=repo_stub_with_call(
            call_id="c1", target_connection="conn_old", status="offline"))
        await bridge.on_client_connect("conn_new")  # 重连换新 conn id
        ok = await bridge.handle_status(
            valid_status(call_id="c1", output="resumed"), sender_connection_id="conn_new")
        assert ok is True
        doc = await bridge._repo.get("c1")
        assert doc["target_connection"] == "conn_new"  # 归属迁移到新连接
        assert doc["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_handle_status_rejects_offline_resend_from_other(self):
        """offline 调用 + 非当前连接提交 → 仍拒绝（不能借 offline 状态伪装）。"""
        bridge = DesktopToolBridge(repo=repo_stub_with_call(
            call_id="c1", target_connection="conn_old", status="offline"))
        await bridge.on_client_connect("conn_new")
        ok = await bridge.handle_status(
            valid_status(call_id="c1"), sender_connection_id="conn_evil")
        assert ok is False
        # 拒绝后 repo 不变：仍 offline、归属未迁移
        doc = await bridge._repo.get("c1")
        assert doc["status"] == "offline"
        assert doc["target_connection"] == "conn_old"
