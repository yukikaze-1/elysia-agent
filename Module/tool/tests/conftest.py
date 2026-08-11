"""reload_plugin 测试共享 fixture — tmp_path 写插件模块 + sys.modules 清理。

importlib.reload 按模块缓存里的 __file__ 重执行——跨测试复用
``reload_plugins.simple`` 名时，若不清 sys.modules，当前测试改写的新文件
不会被读到（读到陈旧实现）。因此 fixture 在 setup/teardown 两侧都清理缓存。

pyc 失效：importlib.reload 按 ``int(source_mtime)`` + size 校验 __pycache__ 的
预编译缓存——同一整数秒内改写磁盘文件且字节数不变时，reload 会命中陈旧 pyc、
读到上一版本实现。因此每次改写模块文件后都必须清掉该模块的 pyc 缓存。
"""

import shutil
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


def _invalidate_pycache(plugin_dir):
    """清掉插件包 __pycache__——防 reload 命中同秒+同大小的陈旧 pyc。

    importlib.reload 的 pyc 校验用 ``int(source_mtime)`` + size：同一整数秒内
    改写模块文件且字节数不变时，预编译缓存看似仍有效，reload 读到上一版本。
    """
    pyc_dir = plugin_dir / "__pycache__"
    if pyc_dir.exists():
        shutil.rmtree(pyc_dir)


@pytest.fixture
def write_simple():
    """写 simple.py——含 execute 返回带版本标记的 ToolResult。

    setup_raises=True 时 setup() 抛 RuntimeError（新实例 setup 失败回滚用例）。
    每次改写后清 pyc 缓存（见 _invalidate_pycache）。
    """
    def _write(src, *, setup_raises=False, suffix="v1"):
        setup_line = "def setup(self, ctx): raise RuntimeError('boom')\n    " if setup_raises else ""
        body = f"""from Module.tool.protocol import ToolPlugin, ToolResult, ToolContext
from Module.tool.safety import SafetyProfile
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
        _invalidate_pycache(src)
    return _write


@pytest.fixture
def write_config():
    """写插件目录 config.yaml（覆盖 SafetyProfile 字段）。

    config.yaml 非 Python 源码、不经 import 编译，无需 pyc 失效。
    """
    def _write(src, **overrides):
        lines = [f"{k}: {v}\n" for k, v in overrides.items()]
        (src / "config.yaml").write_text("".join(lines))
    return _write
