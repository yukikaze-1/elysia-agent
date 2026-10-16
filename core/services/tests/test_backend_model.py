"""`core/services/capabilities.py::_backend_model` 的四种情形（marker=unit）。

## 为什么单独立一个文件

这个函数是**运行期韧性层**与**开发期契约层**的分界线，两边的判据必须同时可见：

- 它**必须**对"后端没实现协议成员"降级（`server_info` 是协议必须帧，不能被一个
  字段拖死）——端到端效果由
  `core/input/tests/test_v2_ws.py::TestServerInfoProtocolRegression::
  test_server_info_survives_tts_backend_missing_model` 钉住；
- 它**不得**顺手把"getter 自己炸了"这类真实实现 bug 一起静默降级——那才是本文件
  在盯的部分。

四种情形用**不同日志级别**区分（缺成员 WARNING / 非字符串 WARNING /
getter 抛异常 ERROR + traceback），所以"有没有被掩盖"是可直接断言的。
"""

from __future__ import annotations

import logging

import pytest

from core.services.capabilities import _backend_model

pytestmark = pytest.mark.unit

_LOGGER = "core.services.capabilities"


def _records(caplog, level=logging.WARNING):
    return [r for r in caplog.records if r.name == _LOGGER and r.levelno >= level]


class TestMissingMember:
    """情形 1：成员**不存在** —— 契约违规，降级 + WARNING。"""

    def test_duck_typed_backend_without_model_degrades(self, caplog):
        class _NoModel:
            """故意违反 Module/tts/protocol.py。"""

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _backend_model(_NoModel(), "tts") is None

        warns = _records(caplog)
        assert len(warns) == 1 and warns[0].levelno == logging.WARNING
        # 日志必须能定位：哪个服务 + 哪个后端类型 + 哪个协议成员
        text = warns[0].getMessage()
        assert "TTS" in text and "_NoModel" in text and ".model" in text

    def test_none_backend_is_not_a_violation(self, caplog):
        """服务未挂载（None）不是协议违规——不该产生任何告警。"""
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _backend_model(None, "stt") is None
        assert _records(caplog) == []


class TestLegalValues:
    """情形 2/3：协议允许的取值。"""

    def test_empty_string_is_legal_and_silent(self, caplog):
        """ABC 的默认实现在 `_model` 未设时就是空串——合法值，不报。"""

        class _Empty:
            model = ""

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _backend_model(_Empty(), "tts") == ""
        assert _records(caplog) == []

    def test_provider_qualified_string_passes_through(self, caplog):
        class _Ok:
            model = "minimax/speech-2.8-hd"

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _backend_model(_Ok(), "tts") == "minimax/speech-2.8-hd"
        assert _records(caplog) == []

    def test_non_string_value_degrades_with_warning(self, caplog):
        """返回 None 与协议 `-> str` 不符——降级为 null，但要说出来（原实现是静默的）。"""

        class _NoneModel:
            model = None

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _backend_model(_NoneModel(), "stt") is None

        warns = _records(caplog)
        assert len(warns) == 1 and warns[0].levelno == logging.WARNING
        assert "NoneType" in warns[0].getMessage()

    def test_dynamic_getattr_attribute_is_not_a_false_negative(self, caplog):
        """经 `__getattr__` 动态提供的 `model` 是**可用**的，不能误判为"缺成员"。

        （旧实现用 `getattr(..., _MISSING)` 时这条路也通；改判据时必须保住它。）
        """

        class _Proxy:
            def __getattr__(self, name: str) -> str:
                if name == "model":
                    return "proxy/dynamic"
                raise AttributeError(name)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _backend_model(_Proxy(), "tts") == "proxy/dynamic"
        assert _records(caplog) == []


class TestBrokenGetter:
    """情形 4：getter **自己抛异常** —— 真实实现 bug，ERROR + traceback。

    这一组是"`_backend_model` 不会吞掉不该吞的 backend bug"的机器判据。
    """

    def test_property_raising_attribute_error_is_not_reported_as_missing(self, caplog):
        """历史踩坑：`getattr(obj, "model", default)` 会把它也变成 default。

        于是 getter 内部的 AttributeError 被误报成"后端没实现协议成员"——同一行
        WARNING，与真正的契约违规无法区分。现在必须走 ERROR 分支。
        """

        class _BuggyGetter:
            @property
            def model(self) -> str:
                return self._absent.thing  # type: ignore[attr-defined]  # 内部 AttributeError

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _backend_model(_BuggyGetter(), "tts") is None

        errs = _records(caplog, logging.ERROR)
        assert len(errs) == 1, caplog.text
        assert errs[0].levelno == logging.ERROR
        assert errs[0].exc_info is not None, "实现 bug 必须带 traceback，不能只留一句文案"
        message = errs[0].getMessage()
        assert "AttributeError" in message and "实现错误" in message
        assert "未实现" not in message, "不得与「缺协议成员」用同一措辞"

    def test_property_raising_other_exception_is_also_error(self, caplog):
        class _Boom:
            @property
            def model(self) -> str:
                raise RuntimeError("底层客户端已销毁")

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _backend_model(_Boom(), "stt") is None

        errs = _records(caplog, logging.ERROR)
        assert len(errs) == 1 and errs[0].exc_info is not None

    def test_missing_member_and_broken_getter_are_distinguishable(self, caplog):
        """同一轮里两种情形必须给出**不同级别**的日志——这是分层的可观测判据。"""

        class _Missing:
            pass

        class _Broken:
            @property
            def model(self) -> str:
                raise AttributeError("内部字段没了")

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _backend_model(_Missing(), "tts")
            _backend_model(_Broken(), "tts")

        levels = sorted(r.levelno for r in caplog.records if r.name == _LOGGER)
        assert levels == [logging.WARNING, logging.ERROR], caplog.text
