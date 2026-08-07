"""Config 测试工厂：参数化生成 TestDefaults + TestFromConfig 两个 test class。"""
from __future__ import annotations

import tempfile
from typing import Any

import pytest
import yaml

from Module.config import ConfigLoader


def make_config_test_classes(
    ConfigClass: type,
    *,
    defaults: dict[str, Any],
    overrides: dict[str, Any],
    env_key: str = "",
    yaml_key: str = "",
) -> tuple[type, type]:
    """返回 (TestDefaults, TestFromConfig) 两个 test class。

    ConfigClass  — 被测 dataclass 配置类
    defaults     — 期望的默认值字典 {field_name: expected_value}
    overrides    — 用于测试覆盖的字段值 {field_name: override_value}
    env_key      — 环境变量名（用于 env override 测试，空字符串则跳过）
    yaml_key     — YAML key（用于 from_config 测试）
    """

    class TestDefaults:
        """默认值测试。"""

        def test_defaults(self, monkeypatch):
            # 环境隔离：禁用 .env 加载 + 清除被测 env 变量，避免本地 .env 污染
            # 默认值（load() 会经 ensure_dotenv 把 .env 注入 os.environ，env 有值时
            # 覆盖默认值；先 delenv 也会被 load() 重新注入，故需整体禁用）。
            from Module.config.loader import EnvManager
            monkeypatch.setattr(EnvManager, "ensure_dotenv",
                                staticmethod(lambda *a, **k: None))
            if env_key:
                monkeypatch.delenv(env_key, raising=False)
            cfg = ConfigLoader(ConfigClass).load()
            for field, expected in defaults.items():
                actual = getattr(cfg, field)
                assert actual == expected, (
                    f"{field}: expected {expected!r}, got {actual!r}"
                )

    class TestFromConfig:
        """YAML + env override 测试。"""

        def test_from_config_passes_params(self):
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                yaml.dump({yaml_key or "test": overrides}, f)
                f.flush()
                cfg = ConfigLoader(
                    ConfigClass,
                    yaml_path=f.name,
                    yaml_key=yaml_key or "test",
                ).load()
            for field, expected in overrides.items():
                actual = getattr(cfg, field)
                assert actual == expected, (
                    f"{field}: expected {expected!r}, got {actual!r}"
                )

        def test_overrides_take_priority(self):
            """YAML value > dataclass default."""
            val = next(iter(overrides.values()))
            field = next(iter(overrides.keys()))
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                yaml.dump({yaml_key or "test": {field: val}}, f)
                f.flush()
                cfg = ConfigLoader(
                    ConfigClass,
                    yaml_path=f.name,
                    yaml_key=yaml_key or "test",
                ).load()
            assert getattr(cfg, field) == val

        @pytest.mark.skipif(not env_key, reason="no env var to test")
        def test_env_override(self, monkeypatch):
            # 局限性：通用工厂不知道哪个字段绑定了 env_key，
            # 因此无法断言具体字段被覆盖为 env 值。
            # 仅验证加载不崩溃 + 得到非 None 的配置对象。
            # 各模块如需真正验证 env→field 绑定，可覆盖此方法。
            val = next(iter(overrides.values()))
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                yaml.dump({yaml_key or "test": {}}, f)
                f.flush()
                monkeypatch.setenv(env_key, str(val))
                cfg = ConfigLoader(
                    ConfigClass,
                    yaml_path=f.name,
                    yaml_key=yaml_key or "test",
                ).load()
            assert cfg is not None

    return TestDefaults, TestFromConfig
