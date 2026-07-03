"""ProfileConfig 单元测试。"""

import tempfile
from pathlib import Path

import pytest
from Module.prompt import ProfileConfig
from Module.prompt import ProfileValidationError


class TestFromYaml:
    def test_load_valid_profile(self):
        """从 YAML 加载正常 profile。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "default.yaml"
            yaml_path.write_text(
                "name: default\n"
                "description: 完整系统 prompt\n"
                "enabled:\n"
                "  - identity\n"
                "  - sensory\n"
                "  - output_format\n"
            )
            prof = ProfileConfig.from_yaml(str(yaml_path))
            assert prof.name == "default"
            assert prof.description == "完整系统 prompt"
            assert prof.enabled == ["identity", "sensory", "output_format"]

    def test_load_profile_with_variants(self):
        """enabled 列表可含 section@variant 引用。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "experimental.yaml"
            yaml_path.write_text(
                "name: experimental\n"
                "description: A/B test\n"
                "enabled:\n"
                "  - identity@soft\n"
                "  - sensory\n"
                "  - output_format\n"
            )
            prof = ProfileConfig.from_yaml(str(yaml_path))
            assert "identity@soft" in prof.enabled

    def test_enabled_set_property(self):
        """enabled_set 返回纯 section 名的集合（去掉 @variant 后缀）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "test.yaml"
            yaml_path.write_text(
                "name: test\n"
                "description: test\n"
                "enabled:\n"
                "  - identity@soft\n"
                "  - sensory\n"
                "  - voice\n"
            )
            prof = ProfileConfig.from_yaml(str(yaml_path))
            assert prof.enabled_set == {"identity", "sensory", "voice"}

    def test_empty_enabled_list(self):
        """enabled 为空列表 — 渲染结果为空。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "empty.yaml"
            yaml_path.write_text(
                "name: empty\n"
                "description: 空 profile\n"
                "enabled: []\n"
            )
            prof = ProfileConfig.from_yaml(str(yaml_path))
            assert prof.enabled == []
            assert prof.enabled_set == set()
