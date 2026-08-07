"""ToolManager 元数据导出 + tool_approval 版本门控测试（BE-040/042）。

覆盖:
  - registry_metadata(): 12 backend + 4 desktop、available 翻转、字段完整
  - definitions(filter_runtime): 运行时过滤（LLM schema 暴露控制，D-j）
  - plugin_list 元数据扩展（runtime/version/risk_level/permission_required/approval_authority/available）
  - tool_approval 版本门控（spec §7）：v1 收原样 wire、v2 收 enrich 版
"""

import pytest

from Module.tool.manager import ToolManager, ToolMetadata

_DESKTOP_NAMES = {
    "desktop.file.select",
    "desktop.file.read",
    "desktop.file.write",
    "desktop.screen.capture",
}


def _make_manager() -> ToolManager:
    """构造已注入全部插件的 ToolManager。

    同步直插 `_plugins`（{cls.name: cls()}），跳过 load_all 的异步 setup——
    各插件构造函数无 I/O，纯单元测试可用。
    """
    from core.tools import ALL_PLUGINS

    tm = ToolManager()
    tm._plugins = {cls.name: cls() for cls in ALL_PLUGINS}
    return tm


def _by_name(meta_list):
    return {m.name: m for m in meta_list}


@pytest.mark.unit
class TestRegistryMetadata:
    def test_returns_12_backend_and_4_desktop(self):
        tm = _make_manager()
        meta = tm.registry_metadata()
        assert len(meta) == 16
        by_runtime: dict[str, int] = {}
        for m in meta:
            by_runtime[m.runtime] = by_runtime.get(m.runtime, 0) + 1
        assert by_runtime == {"backend": 12, "desktop": 4}

    def test_metadata_fields_complete(self):
        tm = _make_manager()
        for m in tm.registry_metadata():
            assert isinstance(m, ToolMetadata)
            assert m.name and m.description and m.version
            assert m.runtime in ("backend", "desktop", "local_worker")
            assert m.risk_level in ("safe", "medium", "high")
            assert isinstance(m.permission_required, bool)
            assert m.approval_authority in ("backend", "client")
            assert isinstance(m.input_schema, dict)
            assert isinstance(m.output_schema, dict)
            assert isinstance(m.available, bool)

    def test_available_flips_for_desktop_backend_always_true(self):
        tm = _make_manager()
        off = _by_name(tm.registry_metadata(desktop_available=False))
        on = _by_name(tm.registry_metadata(desktop_available=True))
        for name in _DESKTOP_NAMES:
            assert off[name].available is False
            assert on[name].available is True
        for name, m in off.items():
            if name not in _DESKTOP_NAMES:
                assert m.available is True

    def test_desktop_still_listed_when_unavailable(self):
        tm = _make_manager()
        names = {m.name for m in tm.registry_metadata(desktop_available=False)}
        assert _DESKTOP_NAMES <= names

    def test_input_schema_reuses_param_model_path(self):
        tm = _make_manager()
        write_file = _by_name(tm.registry_metadata())["write_file"]
        assert write_file.input_schema["type"] == "object"
        assert "properties" in write_file.input_schema

    def test_derived_authority_and_permission(self):
        tm = _make_manager()
        by_name = _by_name(tm.registry_metadata())
        # 审批权映射：desktop → client；backend → backend
        assert by_name["desktop.file.read"].approval_authority == "client"
        assert by_name["write_file"].approval_authority == "backend"
        # high 风险 → permission_required=True
        assert by_name["write_file"].permission_required is True
        assert by_name["desktop.file.write"].permission_required is True


@pytest.mark.unit
class TestDefinitionsFilterRuntime:
    def test_no_filter_returns_all(self):
        tm = _make_manager()
        assert len(tm.definitions()) == 16

    def test_filter_excludes_desktop(self):
        tm = _make_manager()
        defs = tm.definitions(filter_runtime=lambda rt: rt != "desktop")
        names = {d["function"]["name"] for d in defs}
        assert len(defs) == 12
        assert not (_DESKTOP_NAMES & names)

    def test_filter_only_desktop(self):
        tm = _make_manager()
        defs = tm.definitions(filter_runtime=lambda rt: rt == "desktop")
        names = {d["function"]["name"] for d in defs}
        assert names == _DESKTOP_NAMES

    def test_default_definitions_unaffected(self):
        tm = _make_manager()
        assert len(tm.definitions("openai")) == 16


@pytest.mark.unit
class TestPluginListMetadata:
    def test_each_item_has_metadata_fields(self):
        tm = _make_manager()
        for item in tm.plugin_list:
            assert "runtime" in item
            assert "version" in item
            assert "risk_level" in item
            assert "permission_required" in item
            assert "approval_authority" in item
            assert "available" in item

    def test_desktop_available_flips_backend_always_true(self):
        tm = _make_manager()
        tm.set_desktop_available(True)
        by_name = {item["name"]: item for item in tm.plugin_list}
        for name in _DESKTOP_NAMES:
            assert by_name[name]["available"] is True
        for name, item in by_name.items():
            if name not in _DESKTOP_NAMES:
                assert item["available"] is True

        tm.set_desktop_available(False)
        by_name = {item["name"]: item for item in tm.plugin_list}
        for name in _DESKTOP_NAMES:
            assert by_name[name]["available"] is False
        for name, item in by_name.items():
            if name not in _DESKTOP_NAMES:
                assert item["available"] is True


# ============================================================
# tool_approval 版本门控（spec §7）
# ============================================================

class _FakeChannel:
    """模拟输出通道 — 仅携带 protocol_version。"""

    def __init__(self, protocol_version: int) -> None:
        self.protocol_version = protocol_version


class _FakeOutputManager:
    """模拟 OutputManager — 按 protocol_version 拆分通道并记录 command 调用。

    记录每个 command 调用的目标通道列表与 params（dict 拷贝，供断言 wire）。
    """

    def __init__(self, channels) -> None:
        self._channels = channels
        self.calls: list[tuple[list, dict]] = []

    def _split_versions(self):
        v1 = [c for c in self._channels if getattr(c, "protocol_version", 1) == 1]
        v2 = [c for c in self._channels if getattr(c, "protocol_version", 1) >= 2]
        return v1, v2

    async def command(self, cmd, *, params=None, channels=None, capability=None):
        targets = channels if channels is not None else self._channels
        self.calls.append((list(targets), dict(params)))


@pytest.mark.asyncio
@pytest.mark.unit
class TestToolApprovalVersionGating:
    async def test_v1_receives_raw_v2_receives_enriched(self):
        tm = _make_manager()
        v1 = _FakeChannel(1)
        v2 = _FakeChannel(2)
        om = _FakeOutputManager([v1, v2])
        tm.set_hitl_broadcast(om)

        params = {"path": "data/sandbox/x.txt", "content": "y" * 250}
        await tm._pipeline._broadcast_hitl("req_abc", "write_file", params)

        # 恰好两路广播：v1 原样 + v2 enrich
        assert len(om.calls) == 2
        by_channel = {}
        for channels, cmd_params in om.calls:
            assert len(channels) == 1
            assert channels[0] in (v1, v2)
            by_channel[channels[0]] = cmd_params

        # v1：逐字节原样（无 summary/runtime/version/riskLevel/permissionRequired/approvalAuthority）
        v1_params = by_channel[v1]
        assert v1_params == {
            "tool": "write_file",
            "params": str(params)[:200],
            "request_id": "req_abc",
        }
        for extra in (
            "summary", "runtime", "version",
            "riskLevel", "permissionRequired", "approvalAuthority",
        ):
            assert extra not in v1_params

        # v2：enrich 版（元数据取自 _plugins["write_file"]）
        v2_params = by_channel[v2]
        assert v2_params["tool"] == "write_file"
        assert v2_params["request_id"] == "req_abc"
        assert v2_params["params"] == str(params)[:200]
        assert v2_params["runtime"] == "backend"
        assert v2_params["version"] == "1.0.0"
        assert v2_params["riskLevel"] == "high"
        assert v2_params["permissionRequired"] is True
        assert v2_params["approvalAuthority"] == "backend"

        # summary：逐参数结构化摘要 {type, preview, truncated}
        path_summary = v2_params["summary"]["path"]
        assert path_summary["type"] == "str"
        assert path_summary["preview"] == "data/sandbox/x.txt"
        assert path_summary["truncated"] is False

        content_summary = v2_params["summary"]["content"]
        assert content_summary["type"] == "str"
        assert content_summary["truncated"] is True
        assert len(content_summary["preview"]) == 100
        assert content_summary["preview"] == str(params["content"])[:100]

    async def test_v1_only_channel_receives_raw(self):
        """仅 v1 通道：广播一次原样 wire，行为与改动前一致。"""
        tm = _make_manager()
        v1 = _FakeChannel(1)
        om = _FakeOutputManager([v1])
        tm.set_hitl_broadcast(om)

        params = {"path": "data/sandbox/x.txt"}
        await tm._pipeline._broadcast_hitl("req_1", "read_file", params)

        assert len(om.calls) == 1
        channels, cmd_params = om.calls[0]
        assert channels == [v1]
        assert cmd_params == {
            "tool": "read_file",
            "params": str(params)[:200],
            "request_id": "req_1",
        }
