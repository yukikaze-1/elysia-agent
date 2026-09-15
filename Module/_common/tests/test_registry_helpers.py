"""resolve_api_key / resolve_base_url / inject_logger 单元测试。"""
import logging
import os

import pytest

from Module._common import registry_helpers
from Module._common.registry_helpers import (
    get_caller_logger,
    inject_logger,
    resolve_api_key,
    resolve_base_url,
)
from Module._common.tests import _logger_hop_probe as probe

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
        """⚠️ [**非判别**] 本用例自称守护 hop 数，实际咬不住——真防线见下方跨模块用例。

        为什么恒绿：``_inner`` 与 ``_outer`` **同住本测试模块**，单跳取 ``_inner`` 的
        ``f_globals["__name__"]``、双跳取 ``_outer`` 的，而两者是同一个 ``__name__``；
        把 ``get_caller_logger`` 改成 ``frame.f_back.f_back`` 本用例照样通过
        （2026-09-15 注入实测：本用例 GREEN，``TestGetCallerLoggerHopCount`` RED）。
        故它只等价于「返回的名字里有本模块名」这条弱断言，**不构成** hop-count 防线；
        按「不删覆盖」原则保留，判别断言迁到跨模块用例。

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


class TestGetCallerLoggerHopCount:
    """**跨模块** hop-count 判别测试（CODE_REVIEW_REPORT #46 / 缺口行 #14 的真防线）。

    生产契约（`registry_helpers.get_caller_logger` docstring 原文）：

        「返回**调用方模块**的 logger——模块名经调用栈自动探测，无需调用方传
        ``__name__``。」「**单跳 ``f_back``，不要改成双跳。** 调用点都是直接调用、
        中间没有工厂层，双跳会把 logger 名归属到调用方的调用者。」

    判别原理：返回名取自**直接调用帧所在模块**。探针 `_logger_hop_probe` 与本测试
    模块是两个不同模块，于是从探针函数里调用时：

    - 单跳（``f_back``）= 探针模块名 —— 契约期望值；
    - 双跳（``f_back.f_back``）= 本测试模块名（探针函数的调用者）。

    两个名字不同 ⇒ 断言可判别。旧 ``test_single_hop_not_double_hop`` 把两个包装
    函数都放在本模块内，两种实现同值 ⇒ 恒绿（假绿），这就是本类存在的理由。
    """

    def test_probe_is_a_different_module(self):
        """判别力的前提：探针必须住在**另一个**模块。

        若哪天有人把探针内联回本模块，判别力立刻归零（退化成旧测试），故先用
        元断言把前提钉死。
        """
        assert probe.PROBE_MODULE_NAME != __name__
        assert probe.PROBE_MODULE_NAME.endswith("_logger_hop_probe")
        assert registry_helpers.__name__ == "Module._common.registry_helpers"

    def test_called_from_test_module_returns_test_module_logger(self):
        """从本测试模块直接调用 → 得到本测试模块的 logger。"""
        logger = get_caller_logger()
        assert logger.name == __name__
        assert logger is logging.getLogger(__name__)

    def test_called_from_probe_module_returns_probe_module_logger(self):
        """从探针模块调用 → 得到**探针模块**的 logger（而非本测试模块）。

        这一条与上一条合起来才是判别断言：双跳会让本用例拿到本测试模块名。
        """
        logger = probe.caller_logger()
        assert logger.name == probe.PROBE_MODULE_NAME
        assert logger is logging.getLogger(probe.PROBE_MODULE_NAME)

    def test_two_call_sites_yield_two_distinct_names(self):
        """两个调用点必须产出**两个不同**的模块名——旧测试缺的正是这条。"""
        from_test_module = get_caller_logger().name
        from_probe_module = probe.caller_logger_name()
        assert from_test_module != from_probe_module
        assert from_test_module == __name__
        assert from_probe_module == probe.PROBE_MODULE_NAME

    def test_wrapped_call_is_same_module_and_therefore_not_discriminating(self):
        """对照：探针模块内的两层包装仍解析到探针模块（与旧测试同形，不判别 hop 数）。"""
        assert probe.wrapped_caller_logger_name() == probe.PROBE_MODULE_NAME

    def test_never_returns_registry_helpers_logger_itself(self):
        """不得返回 registry_helpers 自己的 logger——那说明帧走反了/走多了。"""
        helpers_logger_name = registry_helpers.__name__
        assert get_caller_logger().name != helpers_logger_name
        assert probe.caller_logger_name() != helpers_logger_name

    def test_logger_cache_identity_same_name_same_object(self):
        """`logging.getLogger` 语义：同名必同对象（`log_setup` 靠名字挂 handler）。"""
        assert logging.getLogger(__name__) is logging.getLogger(__name__)
        assert get_caller_logger() is logging.getLogger(__name__)
        assert probe.caller_logger() is logging.getLogger(probe.PROBE_MODULE_NAME)


class TestInjectLoggerContract:
    """`inject_logger` 与 `get_caller_logger` 的分工（跨模块核实）。"""

    def test_name_is_exactly_the_argument(self):
        assert inject_logger("x.y").name == "x.y"
        assert inject_logger("x.y") is logging.getLogger("x.y")

    def test_argument_wins_over_caller_module(self):
        """从探针模块调用 inject_logger("x.y") 仍得 "x.y"——它**不**看调用方模块。"""
        assert probe.explicit_logger_name("x.y") == "x.y"
        assert probe.explicit_logger_name("x.y") != probe.caller_logger_name()

    def test_does_not_behave_like_get_caller_logger(self):
        """两者只在调用方显式传 ``__name__`` 时才等价——这正是本仓库两种注入风格的分工。"""
        assert inject_logger("x.y").name != get_caller_logger().name
        assert inject_logger(__name__) is get_caller_logger()


class TestRealConsumersLoggerName:
    """真实消费方（`Module/output` 的两处 `get_caller_logger()` 注入点）的端到端归属。

    两处注入点（`self._logger = logger or get_caller_logger()`）**物理上就写在消费方
    自己模块内**：`Module/output/manager.py:56`、`Module/output/websocket.py:72`。
    单跳契约因此等价于「谁**定义**了调用点，就记在谁名下」——期望 logger 名恒为
    ``Module.output.manager`` / ``Module.output.websocket``，与「谁构造了对象」无关。
    双跳则退化成构造方（本测试模块 / 探针模块，生产里是 `core/agent/agent.py:158`
    构造 `OutputManager()`、`core/input/ws_handler.py:280` 构造 `WebSocketChannel`）。

    构造两个消费方都极轻（`OutputManager.__init__` 只建两个空容器；
    `WebSocketChannel.__init__` 只把 ws 存起来，不在构造期触碰），故无需 mock、
    无需 aiohttp 连接即真实覆盖到注入点。读 ``_logger`` 是因为两个类都未公开
    logger 访问器（先例：`Module/llm/tests/test_llm.py` 断言 `backend._logger.name`）。
    """

    def test_output_manager_logs_under_its_own_module(self):
        from Module.output.manager import OutputManager

        # 从测试模块构造：应得 manager 自己的模块名，而不是测试模块名
        assert OutputManager()._logger.name == "Module.output.manager"
        assert OutputManager()._logger is logging.getLogger("Module.output.manager")
        # 从探针模块构造：同一个期望值——构造方不影响归属（双跳会在这里露馅）
        assert probe.output_manager_logger_name() == "Module.output.manager"

    def test_websocket_channel_logs_under_its_own_module(self):
        from Module.output.websocket import WebSocketChannel

        assert WebSocketChannel(object())._logger.name == "Module.output.websocket"
        assert probe.websocket_channel_logger_name() == "Module.output.websocket"

    def test_constructed_logger_overrides_auto_detection(self):
        """显式传 logger 时不得再自动探测（`logger or get_caller_logger()` 短路语义）。"""
        from Module.output.manager import OutputManager

        explicit = logging.getLogger("explicit.consumer")
        assert OutputManager(logger=explicit)._logger is explicit


class TestEmptyBaseUrlDoesNotOverrideDefaults:
    """S2-H7：空 `base_url` 不得顶掉内置默认值（三层：显式/环境/provider 默认）。

    原状：`providers.yaml` 里所有 provider 的 `base_url` 都是空串，且 `resolve_base_url`
    对**空字符串**的环境变量也照原样返回 ⇒ 后端拿到 `base_url=""`（SDK 只对 None 回退）
    ⇒ 静默降级成"连不上"，而从配置上看不出哪里错了。STT 早已按"空 → 内置默认"修过
    （`36f11449`），LLM/TTS 没有。
    """

    def test_provider_default_is_used_when_nothing_set(self, monkeypatch):
        from Module._common.registry_helpers import resolve_base_url

        monkeypatch.delenv("ELYSIA_TEST_URL", raising=False)
        cfg = {"base_url": "https://declared.example.com"}
        assert resolve_base_url("ELYSIA_TEST_URL", cfg) == "https://declared.example.com"

    def test_empty_env_does_not_override_provider_default(self, monkeypatch):
        """**空串视为未设置**——这是本条的核心（原先 `is not None` 就直接返回空串）。"""
        from Module._common.registry_helpers import resolve_base_url

        monkeypatch.setenv("ELYSIA_TEST_URL", "")
        cfg = {"base_url": "https://declared.example.com"}
        assert resolve_base_url("ELYSIA_TEST_URL", cfg) == "https://declared.example.com"

    def test_nonempty_env_still_wins(self, monkeypatch):
        """回归网：非空环境变量照旧优先（别把"空"的处理做成"环境变量失效"）。"""
        from Module._common.registry_helpers import resolve_base_url

        monkeypatch.setenv("ELYSIA_TEST_URL", "https://from-env.example.com")
        cfg = {"base_url": "https://declared.example.com"}
        assert resolve_base_url("ELYSIA_TEST_URL", cfg) == "https://from-env.example.com"

    def test_explicit_still_wins(self, monkeypatch):
        from Module._common.registry_helpers import resolve_base_url

        monkeypatch.setenv("ELYSIA_TEST_URL", "https://from-env.example.com")
        cfg = {"base_url": "https://declared.example.com"}
        assert resolve_base_url(
            "ELYSIA_TEST_URL", cfg, "https://explicit.example.com"
        ) == "https://explicit.example.com"


class TestShippedDefaultsAreNonEmpty:
    """配置层的守卫：`providers.yaml` 的内置 provider 必须声明**非空**默认地址。

    留空就是"配置上写了 provider，实际连不上"——本仓已为此修过三次（STT/LLM/TTS）。
    `gemini` 与 `custom` 例外：前者未实现（工厂直接 raise），后者必须由调用方提供。
    """

    #: 允许为空的例外——每一项都必须写清理由（"留空"要么是刻意的、要么已被别的机制兜住）
    _MAY_BE_EMPTY = {
        "gemini",   # 未实现：LLMFactory 直接 raise NotImplementedError
        "custom",   # 必须由调用方提供 base_url（否则工厂 raise）
        "minimax",  # 云端端点由运营商提供；为空时 Module/tts/registry.py 抛可操作错误
        "mimo",     # 同上，且 LLMFactory 现在也改为**建连即报错**（见 registry.py）
    }

    def test_llm_and_tts_defaults_are_declared(self):
        import yaml
        from pathlib import Path

        cfg = yaml.safe_load(
            Path("Module/config/providers.yaml").read_text(encoding="utf-8"))
        empty: list[str] = []
        for service in ("llm", "tts"):
            for name, entry in (cfg.get(service) or {}).items():
                if name in self._MAY_BE_EMPTY:
                    continue
                if not (entry or {}).get("base_url"):
                    empty.append(f"{service}/{name}")
        assert not empty, f"这些 provider 的 base_url 是空的（会静默降级成连不上）：{empty}"
