"""reload_plugin 测试共享 fixture — tmp_path 写插件模块 + sys.modules 清理。

importlib.reload 按模块缓存里的 __file__ 重执行——跨测试复用
``reload_plugins.simple`` 名时，若不清 sys.modules，当前测试改写的新文件
不会被读到（读到陈旧实现）。因此 fixture 在 setup/teardown 两侧都清理缓存。
"""

import sys

import pytest


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """tmp_path 下建 reload_plugins 包 + sys.path + sys.modules 清理。

    importlib.reload 按模块缓存 __file__ 重执行——跨测试必须清 sys.modules
    防读到上一测试的文件。
    """
    src = tmp_path / "reload_plugins"
    src.mkdir()
    (src / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    for m in list(sys.modules):
        if m == "reload_plugins" or m.startswith("reload_plugins."):
            del sys.modules[m]
    yield src
    for m in list(sys.modules):
        if m == "reload_plugins" or m.startswith("reload_plugins."):
            del sys.modules[m]


@pytest.fixture
def write_simple():
    """写 simple.py——含 execute 返回带版本标记的 ToolResult。

    setup_raises=True 时 setup() 抛 RuntimeError（新实例 setup 失败回滚用例）。
    """
    def _write(src, *, setup_raises=False, suffix="v1"):
        setup_line = "def setup(self, ctx): raise RuntimeError('boom')\n    " if setup_raises else ""
        body = f"""from Module.tool.protocol import ToolPlugin, SafetyProfile, ToolResult, ToolContext
class SimplePlugin(ToolPlugin):
    name = "simple"
    description = "desc-{suffix}"
    param_model = None
    capabilities = set()
    safety = SafetyProfile()
    {setup_line}async def execute(self, ctx: ToolContext, **params):
        return ToolResult.ok(name="simple", content="exec-{suffix}", duration_ms=0.0)
"""
        (src / "simple.py").write_text(body)
    return _write


@pytest.fixture
def write_config():
    """写插件目录 config.yaml（覆盖 SafetyProfile 字段）。"""
    def _write(src, **overrides):
        lines = [f"{k}: {v}\n" for k, v in overrides.items()]
        (src / "config.yaml").write_text("".join(lines))
    return _write
