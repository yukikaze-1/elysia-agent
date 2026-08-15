"""AgentServer._print_startup_banner 测试。"""

from unittest.mock import MagicMock
import pytest
from core.agent.server import AgentServer

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
