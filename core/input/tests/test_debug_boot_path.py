# 测试文件：core/input/tests/test_debug_boot_path.py
"""启动路径回归（2026-09-12）：`/debug` 路由与「无订阅者不轮询」。

守两件事，都是「启动横幅/启动行为宣称了一件不成立的事」这类缺陷：

1. **`/debug` 路由存在的充要条件是 `static/debug.html` 存在**，且启动横幅如实
   反映注册结果（此前横幅无条件宣称「/debug 仪表盘可用」，而该路由是条件注册的
   ——Vite 构建产物本机常年不存在，于是横幅在页面 404 时说它可用）。
2. **应用启动不再拉起 WorkflowDebugAPI 的 500ms 轮询**——它现在按需启动
   （首个 SSE 订阅者到达才跑）。此前 `on_startup` 无条件拉起，形成「2Hz 常驻成本、
   零观测收益」。
"""

from unittest.mock import MagicMock

import pytest
from aiohttp import web

from core.agent.server import AgentServer, _route_registered
from core.agent import RunState
from core.input import server as server_module
from core.input.debug_routes import _on_shutdown, _on_startup
from core.input.server import create_app
from Module.workflow_engine.debug_api import WorkflowDebugAPI


def _fake_agent() -> MagicMock:
    agent = MagicMock()
    agent._state = RunState.RUNNING
    agent.user_name = "tester"
    return agent


class _FakeWorkflowEngine:
    """最小 WorkflowEngine 桩。"""

    def __init__(self) -> None:
        self.snapshot_calls = 0

    def _get_debug_snapshot(self) -> dict:
        self.snapshot_calls += 1
        return {"tasks": []}


@pytest.mark.service
class TestDebugRouteRegistration:
    """`/debug` 只在 `static/debug.html` 存在时注册。"""

    def test_absent_debug_html_registers_no_route(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server_module, "_STATIC_DIR", tmp_path)
        app = create_app(_fake_agent(), serve_static=False)
        assert _route_registered(app, "/debug") is False

    def test_present_debug_html_registers_route(self, tmp_path, monkeypatch):
        (tmp_path / "debug.html").write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(server_module, "_STATIC_DIR", tmp_path)
        app = create_app(_fake_agent(), serve_static=False)
        assert _route_registered(app, "/debug") is True


@pytest.mark.service
class TestBannerMatchesRouteReality:
    """横幅那一行必须与路由表一致——这是原缺陷的直接回归守卫。"""

    def _banner_line(self, tmp_path, monkeypatch, *, built: bool) -> str:
        if built:
            (tmp_path / "debug.html").write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(server_module, "_STATIC_DIR", tmp_path)
        app = create_app(_fake_agent(), serve_static=False)
        return AgentServer._debug_banner_line(app, "127.0.0.1", 8765)

    def test_unbuilt_says_unbuilt(self, tmp_path, monkeypatch):
        line = self._banner_line(tmp_path, monkeypatch, built=False)
        assert "未构建" in line
        assert "8765" not in line

    def test_built_gives_url(self, tmp_path, monkeypatch):
        line = self._banner_line(tmp_path, monkeypatch, built=True)
        assert line.endswith("http://127.0.0.1:8765/debug")


@pytest.mark.service
class TestStartupDoesNotPoll:
    """on_startup 不得拉起轮询；订阅才拉起，退订即停止。"""

    @pytest.mark.asyncio
    async def test_on_startup_leaves_poll_stopped(self):
        ts = _FakeWorkflowEngine()
        api = WorkflowDebugAPI(ts)
        app = web.Application()
        app["task_debug_api"] = api

        await _on_startup(app)
        assert api._poll_task is None
        assert ts.snapshot_calls == 0

    @pytest.mark.asyncio
    async def test_subscribe_after_startup_starts_polling(self, monkeypatch):
        from Module.workflow_engine import debug_api as debug_api_module
        monkeypatch.setattr(debug_api_module, "_POLL_INTERVAL", 0.01)

        ts = _FakeWorkflowEngine()
        api = WorkflowDebugAPI(ts)
        app = web.Application()
        app["task_debug_api"] = api

        await _on_startup(app)
        _sid, _q = await api.subscribe()
        assert api._poll_task is not None

        await _on_shutdown(app)
        assert api._poll_task is None

    @pytest.mark.asyncio
    async def test_shutdown_without_subscribers_is_safe(self):
        ts = _FakeWorkflowEngine()
        api = WorkflowDebugAPI(ts)
        app = web.Application()
        app["task_debug_api"] = api
        await _on_startup(app)
        await _on_shutdown(app)
        assert api._poll_task is None
