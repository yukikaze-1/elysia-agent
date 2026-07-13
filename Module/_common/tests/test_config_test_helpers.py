"""make_config_test_classes 单元测试。"""
from dataclasses import dataclass, field

import pytest

from Module.config import ConfigField
from Module._common.config_test_helpers import make_config_test_classes

pytestmark = pytest.mark.unit


@dataclass
class FakeConfig:
    host: str = ConfigField(default="localhost")
    port: int = ConfigField(default=8080)


TestDefaults, TestFromConfig = make_config_test_classes(
    FakeConfig,
    defaults={"host": "localhost", "port": 8080},
    overrides={"host": "override-host", "port": 9090},
    env_key="",
    yaml_key="Fake",
)


class TestGeneratedDefaults(TestDefaults):
    pass


class TestGeneratedFromConfig(TestFromConfig):
    pass
