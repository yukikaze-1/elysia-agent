"""resolve_api_key / resolve_base_url / inject_logger 单元测试。"""
import logging
import os

import pytest

from Module._common.registry_helpers import (
    inject_logger,
    resolve_api_key,
    resolve_base_url,
)

pytestmark = pytest.mark.unit


class TestResolveApiKey:
    def test_explicit_key_wins(self):
        assert resolve_api_key("ENV_KEY", {}, explicit_key="explicit") == "explicit"
        assert resolve_api_key("ENV_KEY", {"default_api_key": "cfg"}, explicit_key="explicit") == "explicit"

    def test_env_var_second(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "from-env")
        assert resolve_api_key("TEST_KEY", {"default_api_key": "from-cfg"}) == "from-env"

    def test_falls_back_to_default(self):
        assert resolve_api_key("MISSING_KEY", {"default_api_key": "from-cfg"}) == "from-cfg"

    def test_returns_empty_when_nothing_set(self):
        assert resolve_api_key("MISSING_KEY", {}) == ""


class TestResolveBaseUrl:
    def test_explicit_url_wins(self):
        assert resolve_base_url("ENV", {"base_url": "cfg"}, explicit_url="http://explicit") == "http://explicit"

    def test_explicit_none_skips(self):
        assert resolve_base_url("ENV", {"base_url": "http://cfg"}) == "http://cfg"

    def test_env_var_second(self, monkeypatch):
        monkeypatch.setenv("TEST_URL", "http://env")
        assert resolve_base_url("TEST_URL", {"base_url": "http://cfg"}) == "http://env"

    def test_falls_back_to_default(self):
        assert resolve_base_url("MISSING", {"base_url": "http://cfg"}) == "http://cfg"

    def test_returns_empty_when_nothing_set(self):
        assert resolve_base_url("MISSING", {}) == ""


class TestInjectLogger:
    def test_uses_explicit_logger(self):
        explicit = logging.getLogger("explicit")
        result = inject_logger("module", explicit_logger=explicit)
        assert result is explicit

    def test_creates_logger_when_none(self):
        result = inject_logger("my_module")
        assert isinstance(result, logging.Logger)
        assert result.name == "my_module"
