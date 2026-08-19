"""ToolManager.runtime_of 单元测试（Task 3 B-6-03 发射点反查 runtime）。"""

import pytest

from Module.tool.manager import ToolManager


@pytest.mark.unit
class TestRuntimeOf:
    def test_runtime_of_registered_plugins(self):
        """已注册插件：runtime_of 返回插件声明的 runtime（backend/desktop）。"""
        from core.tools import ALL_PLUGINS

        tm = ToolManager()
        tm._plugins = {cls.name: cls() for cls in ALL_PLUGINS}
        # 12 backend + 4 desktop（与 test_metadata 同构）；抽查两档
        desktop = [cls.name for cls in ALL_PLUGINS if cls.runtime == "desktop"]
        assert desktop, "应存在 desktop 插件"
        assert tm.runtime_of("get_time") == "backend"
        for name in desktop:
            assert tm.runtime_of(name) == "desktop"

    def test_runtime_of_unknown_returns_none(self):
        """未注册工具（submit_task/幻觉名）：runtime_of 返回 None。"""
        tm = ToolManager()
        assert tm.runtime_of("submit_task") is None
        assert tm.runtime_of("not_registered") is None
