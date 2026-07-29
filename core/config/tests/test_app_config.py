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
