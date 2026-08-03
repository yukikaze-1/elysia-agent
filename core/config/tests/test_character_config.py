"""CharacterConfig YAML 加载测试。"""

import pytest
from pathlib import Path
from core.config.character_config import CharacterConfig

pytestmark = pytest.mark.unit


class TestCharacterConfigFromYAML:
    """从临时 YAML 文件加载角色配置"""

    @pytest.fixture
    def char_dir(self, tmp_path: Path):
        """创建最小角色目录：config.yaml + {name}_personality.md"""
        (tmp_path / "config.yaml").write_text(
            "name: 测试角色\n"
            "wake_words: [你好, 测试]\n"
            "personality:\n"
            "  traits: [温柔, 细心]\n"
            "  speaking_style: 轻声细语\n"
        )
        # 自动推导: {name}_personality.md → 测试角色_personality.md
        (tmp_path / "测试角色_personality.md").write_text("我是测试角色。")
        return tmp_path

    def test_load_name(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert cfg.name == "测试角色"

    def test_load_wake_words(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert "你好" in cfg.wake_words

    def test_traits_property(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert cfg.traits == ["温柔", "细心"]

    def test_speaking_style_property(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert cfg.speaking_style == "轻声细语"

    def test_loads_l1_personality_text(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert "测试角色" in cfg.base_personality

    def test_missing_personality_md_does_not_crash(self, tmp_path: Path):
        """personality.md 不存在时 L1 文本为空或默认值"""
        (tmp_path / "config.yaml").write_text("name: 无L1\nwake_words: []\n")
        cfg = CharacterConfig.from_yaml(tmp_path / "config.yaml")
        assert cfg.name == "无L1"
        # 不应抛异常

    def test_explicit_missing_personality_file_raises(self, tmp_path: Path):
        """显式指定 base_personality_file 但文件不存在 → 配置错误 fail-fast（O6）。

        未显式指定（旧 YAML 向后兼容）仍降级；显式指定是正式部署声明，
        缺失时以空 L1 运行会让 C1 无核心性格——应抛错而非静默。
        """
        (tmp_path / "config.yaml").write_text(
            "name: 测试\nbase_personality_file: missing.md\n"
        )
        with pytest.raises(FileNotFoundError):
            CharacterConfig.from_yaml(tmp_path / "config.yaml")

    def test_no_reference_dir_does_not_crash(self, char_dir):
        """无 reference/ 目录不报错"""
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert cfg.name == "测试角色"

    def test_missing_config_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            CharacterConfig.from_yaml(tmp_path / "nonexistent.yaml")
