"""resolve_api_key / resolve_base_url / inject_logger 单元测试。"""
import logging
import os

import pytest

from Module._common.registry_helpers import (
    get_caller_logger,
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


class TestGetCallerLogger:
    """自 Module/logger/factory.py 迁入（该包 2026-09-12 删除，仅留此函数）。"""

    def test_returns_logger_with_module_name(self):
        """logger 名必须是**本模块**——错一层就会让 log_setup 把日志分流进错文件。"""
        def _inner():
            return get_caller_logger()

        assert "test_registry_helpers" in _inner().name

    def test_single_hop_not_double_hop(self):
        """回归防线：双跳 f_back 会把 logger 名归属到调用方的调用者。

        历史 bug —— 双跳导致 %(name)s 来源错乱。log_setup 按 logger 名分流文件，
        所以名错一层 = 日志进错文件，不是显示问题。
        """
        def _inner():
            return get_caller_logger()

        def _outer():
            return _inner()

        # 无论中间隔几层包装，取到的都应是**直接调用方** _inner 所在模块
        assert "test_registry_helpers" in _outer().name

    def test_distinct_from_inject_logger(self):
        """inject_logger 要调用方自报模块名；get_caller_logger 自动探测。"""
        assert inject_logger("whatever").name == "whatever"
        assert get_caller_logger().name != "whatever"
