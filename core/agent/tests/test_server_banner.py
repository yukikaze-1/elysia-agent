"""AgentServer._print_startup_banner 测试。"""

from unittest.mock import MagicMock
import pytest
from aiohttp import web
from core.agent.server import AgentServer, _route_registered

pytestmark = pytest.mark.service


class TestStartupBanner:
    def _setup_banner_attrs(self, agent):
        """设置 _print_startup_banner 需要的实例属性。"""
        agent._config.tts.model = "test-tts/model"
        agent._character.name = "测试角色"
        agent._character.base_personality = "我是测试人格文本"
        agent._llm.model = "test-llm/model"
        agent._long_term.count = 0
        agent._tool_adapter.execute_now = MagicMock(return_value=["echo", "web_search"])

    def test_both_databases_ok(self, fake_agent):
        self._setup_banner_attrs(fake_agent)
        banner = AgentServer._print_startup_banner(
            fake_agent, mongo_ok=True, milvus_ok=True)
        assert "MongoDB" in banner
        assert "Milvus" in banner
        assert "测试角色" in banner
        assert "test-llm/model" in banner
        assert "2 个已加载" in banner

    def test_mongo_offline(self, fake_agent):
        self._setup_banner_attrs(fake_agent)
        banner = AgentServer._print_startup_banner(
            fake_agent, mongo_ok=False, milvus_ok=True)
        assert "Milvus" in banner

    def test_both_offline_shows_in_memory(self, fake_agent):
        self._setup_banner_attrs(fake_agent)
        banner = AgentServer._print_startup_banner(
            fake_agent, mongo_ok=False, milvus_ok=False)
        assert "in-memory" in banner.lower()

    def test_truncates_long_personality(self, fake_agent):
        self._setup_banner_attrs(fake_agent)
        fake_agent._character.base_personality = "A" * 100
        banner = AgentServer._print_startup_banner(
            fake_agent, mongo_ok=True, milvus_ok=True)
        assert "A" * 60 in banner
        assert "..." in banner


class TestSttBannerStatus:
    """AgentServer._stt_banner_status 测试——STT 行在 run_server 内联不可直测，抽此助手。"""

    def test_connected_shows_model(self, fake_agent):
        backend = MagicMock()
        backend.model = "sensevoice/sense-voice"
        fake_agent._stt = backend
        status = AgentServer._stt_banner_status(fake_agent)
        assert status == "已连接 (model=sensevoice/sense-voice)"

    def test_disconnected(self, fake_agent):
        fake_agent._stt = None
        status = AgentServer._stt_banner_status(fake_agent)
        assert status == "未连接"


class TestRunServerAppFactory:
    """run_server 必须由装配入口注入 app_factory，AgentServer 不反向依赖 core.input。"""

    @pytest.mark.asyncio
    async def test_missing_app_factory_fails_fast(self, fake_agent):
        with pytest.raises(RuntimeError, match="app_factory"):
            await AgentServer.run_server(fake_agent)


class TestDebugBannerLine:
    """启动横幅的 /debug 行必须反映**实际注册结果**。

    历史缺陷（2026-09-12 修正）：该行无条件写「/debug 仪表盘可用」，而该路由只在
    ``static/debug.html`` 存在时注册（Vite 构建产物，本机常年未构建）——于是横幅
    在仪表盘恰好 404 的时候宣称它可用。
    """

    @staticmethod
    async def _noop(_request):
        return web.Response(text="ok")

    def _app_without_debug(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._noop)
        return app

    def _app_with_debug(self) -> web.Application:
        app = self._app_without_debug()
        app.router.add_get("/debug", self._noop)
        return app

    def test_route_registered_false_when_absent(self):
        assert _route_registered(self._app_without_debug(), "/debug") is False

    def test_route_registered_true_when_present(self):
        assert _route_registered(self._app_with_debug(), "/debug") is True

    def test_route_registered_ignores_prefix_match(self):
        """/debug/api 存在不等于 /debug 页面存在（模糊匹配会重演原缺陷）。"""
        app = web.Application()
        app.router.add_get("/debug/api", self._noop)
        assert _route_registered(app, "/debug") is False

    def test_line_says_unbuilt_when_route_absent(self):
        line = AgentServer._debug_banner_line(self._app_without_debug(), "127.0.0.1", 8765)
        assert "未构建" in line
        assert "8765" not in line          # 不得给出一个打不开的地址
        assert "可用" not in line

    def test_line_gives_url_when_route_present(self):
        line = AgentServer._debug_banner_line(self._app_with_debug(), "127.0.0.1", 8765)
        assert line.endswith("http://127.0.0.1:8765/debug")
        assert "未构建" not in line
