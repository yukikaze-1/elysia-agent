"""Desktop 工具插件测试 — 元数据、参数校验、execute 委托桥、paramHash 确定性（BE-040）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from Module._common.error_type import ErrorType
from Module.tool.protocol import ToolContext, ToolResult
from core.tools.desktop.plugin import (
    DesktopFileReadPlugin,
    DesktopFileSelectPlugin,
    DesktopFileWritePlugin,
    DesktopScreenCapturePlugin,
    FileReadParams,
    arguments_hash,
    canonical_json,
)


@pytest.mark.unit
class TestDesktopPluginMetadata:
    """4 个 desktop 插件的类名/name/runtime/risk/request_timeout。"""

    def test_all_four_plugin_names(self):
        """4 个插件名齐全。"""
        plugins = [
            DesktopFileSelectPlugin(),
            DesktopFileReadPlugin(),
            DesktopFileWritePlugin(),
            DesktopScreenCapturePlugin(),
        ]
        names = {p.name for p in plugins}
        assert names == {
            "desktop.file.select",
            "desktop.file.read",
            "desktop.file.write",
            "desktop.screen.capture",
        }

    def test_runtime_and_version(self):
        """全部 runtime=desktop、version=1.0.0、approval_authority=client。"""
        for plugin in (
            DesktopFileSelectPlugin(),
            DesktopFileReadPlugin(),
            DesktopFileWritePlugin(),
            DesktopScreenCapturePlugin(),
        ):
            assert plugin.runtime == "desktop"
            assert plugin.version == "1.0.0"
            assert plugin.approval_authority == "client"

    def test_risk_levels(self):
        """risk 标注：select=safe、read=medium、write=high、capture=high。"""
        assert DesktopFileSelectPlugin().risk_level == "safe"
        assert DesktopFileReadPlugin().risk_level == "medium"
        assert DesktopFileWritePlugin().risk_level == "high"
        assert DesktopScreenCapturePlugin().risk_level == "high"

    def test_request_timeout(self):
        """safety.request_timeout 必须显式声明 120s——防 SafetyPipeline 30s 截断桥往返。"""
        for plugin in (
            DesktopFileSelectPlugin(),
            DesktopFileReadPlugin(),
            DesktopFileWritePlugin(),
            DesktopScreenCapturePlugin(),
        ):
            assert plugin.safety is not None
            assert plugin.safety.request_timeout == 120.0

    def test_screen_capture_output_schema_object(self):
        """screen.capture 的 output_schema 为包含 attachments 的媒体结果。"""
        assert DesktopScreenCapturePlugin().output_schema == {
            "type": "object",
            "description": "包含 attachments 的媒体结果",
        }

    def test_permission_required(self):
        """permission_required 随 risk 派生：read/write/capture=True、select=False。"""
        assert DesktopFileSelectPlugin().permission_required is False
        assert DesktopFileReadPlugin().permission_required is True
        assert DesktopFileWritePlugin().permission_required is True
        assert DesktopScreenCapturePlugin().permission_required is True


@pytest.mark.unit
class TestDesktopFileReadParams:
    def test_path_required(self):
        """desktop.file.read 的 param_model 校验 path 必填。"""
        with pytest.raises(ValidationError):
            FileReadParams()
        with pytest.raises(ValidationError):
            FileReadParams(path="")


@pytest.mark.unit
class TestParamHash:
    """paramHash 确定性 — 规范化 JSON 排序 + SHA-256（BE-043 权威依据）。"""

    def test_same_arguments_same_hash(self):
        """同参数两次计算同哈希。"""
        args = {"path": "/home/user/doc.txt", "region": "full"}
        assert arguments_hash(args) == arguments_hash(dict(args))

    def test_different_arguments_different_hash(self):
        """不同参数异哈希。"""
        assert arguments_hash({"path": "/a"}) != arguments_hash({"path": "/b"})

    def test_canonical_json_key_sorting(self):
        """canonical JSON 键排序：键序不同但内容相同 → 同序列化、同哈希。"""
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
        assert arguments_hash({"b": 1, "a": 2}) == arguments_hash({"a": 2, "b": 1})

    def test_canonical_json_compact_format(self):
        """紧凑分隔：sort_keys + separators=(",", ":")。"""
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'


@pytest.mark.unit
class TestDesktopExecute:
    def test_no_bridge_returns_unavailable(self):
        """ctx.services 无 desktop_bridge → ToolResult.fail(error_type=UNAVAILABLE)。"""
        plugin = DesktopFileReadPlugin()
        ctx = ToolContext(services={})
        result = asyncio.run(
            plugin.execute(ctx=ctx, params=FileReadParams(path="/home/user/doc.txt"))
        )
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert result.error_type == ErrorType.UNAVAILABLE
        assert result.error == "无桌面客户端连接"

    def test_bridge_unavailable_returns_unavailable(self):
        """bridge 存在但 is_available()=False → UNAVAILABLE，不触发委托。"""
        plugin = DesktopFileReadPlugin()
        bridge = MagicMock()
        bridge.is_available.return_value = False
        ctx = ToolContext(services={"desktop_bridge": bridge})
        result = asyncio.run(
            plugin.execute(ctx=ctx, params=FileReadParams(path="/home/user/doc.txt"))
        )
        assert result.success is False
        assert result.error_type == ErrorType.UNAVAILABLE
        bridge.request_and_wait.assert_not_called()

    def test_bridge_available_delegates(self):
        """bridge 可用 → request_and_wait 被调，call 含工具元数据 + arguments_hash。"""
        plugin = DesktopFileReadPlugin()
        bridge = MagicMock()
        bridge.is_available.return_value = True
        bridge.request_and_wait = AsyncMock(return_value=ToolResult.ok(
            name="desktop.file.read", content="文件内容", duration_ms=0.0,
        ))
        ctx = ToolContext(services={"desktop_bridge": bridge})
        params = FileReadParams(path="/home/user/doc.txt")
        result = asyncio.run(plugin.execute(ctx=ctx, params=params))

        assert result.success is True
        bridge.request_and_wait.assert_awaited_once()
        call = bridge.request_and_wait.await_args.kwargs["call"]
        assert call["tool_name"] == "desktop.file.read"
        assert call["tool_version"] == "1.0.0"
        assert call["runtime"] == "desktop"
        assert call["arguments"] == {"path": "/home/user/doc.txt"}
        assert call["arguments_hash"] == arguments_hash({"path": "/home/user/doc.txt"})
        assert call["risk_level"] == "medium"
        assert call["permission_required"] is True
        assert call["approval_authority"] == "client"
        # B-6-05：call dict 恒含 source_call_id/request_id（ctx.call_id / ctx.trace_id or None）
        assert call["source_call_id"] == ""          # 默认 ctx.call_id 空串
        assert call["request_id"] is None            # 默认 ctx.trace_id 空串 → None

    def test_bridge_available_delegates_id_passthrough(self):
        """call dict 透传 source_call_id/request_id（= ctx.call_id / ctx.trace_id or None）。"""
        plugin = DesktopFileReadPlugin()
        bridge = MagicMock()
        bridge.is_available.return_value = True
        bridge.request_and_wait = AsyncMock(return_value=ToolResult.ok(
            name="desktop.file.read", content="文件内容", duration_ms=0.0,
        ))
        ctx = ToolContext(
            services={"desktop_bridge": bridge},
            call_id="tc_llm_desktop", trace_id="trace_req_1",
        )
        params = FileReadParams(path="/home/user/doc.txt")
        result = asyncio.run(plugin.execute(ctx=ctx, params=params))

        assert result.success is True
        call = bridge.request_and_wait.await_args.kwargs["call"]
        assert call["source_call_id"] == "tc_llm_desktop"
        assert call["request_id"] == "trace_req_1"
