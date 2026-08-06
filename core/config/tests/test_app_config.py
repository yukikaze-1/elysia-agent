"""AppConfig 嵌套 dataclass 默认值验证。"""

import pytest
from core.config.app_config import AppConfig, DatabaseConfig

pytestmark = pytest.mark.unit


class TestAppConfigNesting:
    """AppConfig 嵌套 dataclass 默认值"""

    def test_database_section_exists(self):
        cfg = AppConfig()
        assert isinstance(cfg.database, DatabaseConfig)

    def test_all_top_level_sections_have_defaults(self):
        """验证 10 个顶层段都可无参实例化"""
        cfg = AppConfig()
        assert cfg.agent is not None
        assert cfg.llm is not None
        assert cfg.memory is not None
        assert cfg.server is not None
        assert cfg.tts is not None
        assert cfg.stt is not None
        assert cfg.wake_word is not None
        assert cfg.live2d is not None
        assert cfg.vision is not None
        assert cfg.database is not None

    def test_database_nested_default_value(self):
        cfg = AppConfig()
        assert cfg.database.mongodb_uri == "mongodb://localhost:27017"

    def test_memory_section_defaults(self):
        cfg = AppConfig()
        assert cfg.memory.session_max_messages == 20
        assert cfg.memory.ltm_max_entries == 10000


class TestServerConfigBatch1:
    """批次 1 新增：access_token / max_ws_connections / max_request_bytes。"""

    def test_access_token_env_binding(self):
        """access_token 绑定 env AGENT_ACCESS_TOKEN 且 secret=True（防落盘）。"""
        import dataclasses
        from core.config.app_config import ServerConfig
        f = next(f for f in dataclasses.fields(ServerConfig) if f.name == "access_token")
        assert f.metadata["_config_env"] == "AGENT_ACCESS_TOKEN"
        assert f.metadata["_config_secret"] is True

    def test_access_token_default_empty(self):
        from core.config.app_config import ServerConfig
        cfg = ServerConfig()
        assert cfg.access_token == ""

    def test_max_ws_connections_default(self):
        from core.config.app_config import ServerConfig
        assert ServerConfig().max_ws_connections == 64

    def test_max_request_bytes_default(self):
        """批次 7 提到 64MB（既有 8MB 会把 10MB 图片截住，I3）。"""
        from core.config.app_config import ServerConfig
        assert ServerConfig().max_request_bytes == 64 * 1024 * 1024

    def test_attachment_sig_secret_default_empty_and_env_binding(self):
        """批次 7：attachment_sig_secret 缺省空 + 绑定 env ATTACHMENT_SIG_SECRET（secret）。"""
        import dataclasses
        from core.config.app_config import ServerConfig
        assert ServerConfig().attachment_sig_secret == ""
        f = next(f for f in dataclasses.fields(ServerConfig) if f.name == "attachment_sig_secret")
        assert f.metadata["_config_env"] == "ATTACHMENT_SIG_SECRET"
        assert f.metadata["_config_secret"] is True
