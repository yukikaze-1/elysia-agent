"""ToolPlugin 元数据扩展测试 — runtime/version/risk_level/output_schema + 派生属性（BE-040）。"""

import pytest

from Module.tool.protocol import ToolPlugin
from Module.tool.safety import SafetyProfile


@pytest.mark.unit
class TestToolPluginMetadata:
    def test_default_runtime_backend(self):
        """未配置子类默认 runtime=backend、approval_authority=backend。"""

        class BarePlugin(ToolPlugin):
            name = "bare"

        p = BarePlugin()
        assert p.runtime == "backend"
        assert p.approval_authority == "backend"

    def test_default_version_risk_output_schema(self):
        """默认 version=1.0.0、risk_level=safe、output_schema 为字符串公开输出。"""

        class BarePlugin(ToolPlugin):
            name = "bare"

        p = BarePlugin()
        assert p.version == "1.0.0"
        assert p.risk_level == "safe"
        assert p.output_schema == {"type": "string", "description": "工具公开输出"}

    def test_desktop_runtime_approval_authority_client(self):
        """runtime=desktop → approval_authority=client（D-h 规则）。"""

        class DesktopPlugin(ToolPlugin):
            name = "desktop.foo"
            runtime = "desktop"

        p = DesktopPlugin()
        assert p.approval_authority == "client"

    def test_local_worker_approval_authority_client(self):
        """runtime=local_worker → approval_authority=client（BE-042 派生规则，无实现保留）。"""

        class WorkerPlugin(ToolPlugin):
            name = "local.foo"
            runtime = "local_worker"

        p = WorkerPlugin()
        assert p.approval_authority == "client"

    def test_permission_required_safe_no_approval(self):
        """safe + 无人工审批 → permission_required=False（D-i）。"""

        class SafePlugin(ToolPlugin):
            name = "safe_tool"
            safety = SafetyProfile(requires_human_approval=False)

        assert SafePlugin().permission_required is False

    def test_permission_required_high_risk(self):
        """risk_level=high → permission_required=True（无需审批也要求）。"""

        class HighRiskPlugin(ToolPlugin):
            name = "high_tool"
            risk_level = "high"

        assert HighRiskPlugin().permission_required is True

    def test_permission_required_medium_risk(self):
        """risk_level=medium → permission_required=True（中等风险需授权）。"""

        class MediumPlugin(ToolPlugin):
            name = "medium_tool"
            risk_level = "medium"

        assert MediumPlugin().permission_required is True

    def test_permission_required_safe_but_approval(self):
        """safe 但 requires_human_approval=True → permission_required=True。"""

        class ApprovalPlugin(ToolPlugin):
            name = "approval_tool"
            safety = SafetyProfile(requires_human_approval=True)

        assert ApprovalPlugin().permission_required is True

    def test_permission_required_no_safety(self):
        """safety=None（默认）+ safe → permission_required=False。"""

        class BarePlugin(ToolPlugin):
            name = "bare"

        assert BarePlugin().permission_required is False
