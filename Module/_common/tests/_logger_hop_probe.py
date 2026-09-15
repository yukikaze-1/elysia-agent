"""`get_caller_logger()` 单跳契约的**跨模块**探针（CODE_REVIEW_REPORT #46 / 缺口行 #14）。

## 为什么探针必须住在另一个模块

`Module/_common/registry_helpers.get_caller_logger()` 的契约是「返回**直接调用方**
所在模块的 logger」，模块名取自调用帧的 ``f_globals["__name__"]``——也就是
**函数定义所在模块**。于是判别力完全取决于「包装函数住在哪」：

- 包装函数若与断言同住一个模块（旧 ``test_single_hop_not_double_hop`` 的写法），
  单跳（``f_back``）与双跳（``f_back.f_back``）解析到的是**同一个** ``__name__``，
  两种实现产出恒等的结果，断言不可判别——这正是那道假绿防线（S3-J2）；
- 包装函数住在**本模块**里，从测试模块调用时，单跳 = 本模块名、双跳 = 测试模块名，
  两个名字不同，断言才咬得住回归。

同样的判别力也用在**真实消费方**上（`output_manager_logger_name` /
`websocket_channel_logger_name`）：那两个类的调用点在它们自己的模块里，单跳恒得
``Module.output.*``；双跳则退化成「谁构造它们」——从本模块构造得本模块名，从测试模块
构造得测试模块名，于是断言同样可分。

本模块只做转发与轻量构造，**不含任何断言**；判别断言全部在
``test_registry_helpers.py``。
"""
import logging

from Module._common.registry_helpers import get_caller_logger, inject_logger

PROBE_MODULE_NAME = __name__
"""本探针模块的 ``__name__``——单跳实现下 `get_caller_logger()` 的**期望**返回值。"""


def caller_logger() -> logging.Logger:
    """直接调用 `get_caller_logger()` 并原样返回 logger 对象。

    本函数是 `get_caller_logger()` 的**直接调用方**，故期望拿到的正是本模块的 logger。

    Returns:
        Logger: 本探针模块的 logger（单跳实现下）。
    """
    return get_caller_logger()


def caller_logger_name() -> str:
    """返回 `get_caller_logger()` 探测到的 logger 名（期望 = 本模块名）。

    Returns:
        str: 探测到的 logger 名。
    """
    return get_caller_logger().name


def wrapped_caller_logger_name() -> str:
    """同模块内的两层包装——**故意保留的不可判别形态**（对照用）。

    单跳与双跳在本模块内都解析到本模块名，因此本函数与 ``caller_logger_name``
    一样**无法**区分 hop 数；它模拟旧测试的写法，供测试标注「非判别断言」。

    Returns:
        str: 探测到的 logger 名。
    """
    def _inner() -> str:
        return get_caller_logger().name

    return _inner()


def explicit_logger_name(module_name: str) -> str:
    """调用 `inject_logger(module_name)`——名字由**实参**决定，与调用方模块无关。

    Args:
        module_name: 显式给定的 logger 名。

    Returns:
        str: `inject_logger` 返回的 logger 名（恒等于实参）。
    """
    return inject_logger(module_name).name


def output_manager_logger_name() -> str:
    """构造真实消费方 ①（`Module/output/manager.py::OutputManager`）并返回其 logger 名。

    ``OutputManager.__init__`` 内部是 ``self._logger = logger or get_caller_logger()``
    ——**调用点就在 manager 模块自己**，所以单跳实现的期望值是
    ``"Module.output.manager"``，且**与谁构造它无关**（从本模块构造、从测试模块构造
    必须同值）。若改成双跳，本函数拿到的会变成「构造方」模块名（本探针模块名），
    测试模块拿到的则变成测试模块名——正是判别点。

    构造极轻（两个空容器），无需 mock。

    Returns:
        str: 自动探测到的 logger 名（期望 ``"Module.output.manager"``）。
    """
    from Module.output.manager import OutputManager

    # 测试代码读取私有字段：OutputManager 未公开 logger 访问器，
    # 而本测试的唯一目的就是核实它探测到的名字（先例：Module/llm/tests/test_llm.py）。
    return OutputManager()._logger.name


def websocket_channel_logger_name() -> str:
    """构造真实消费方 ②（`Module/output/websocket.py::WebSocketChannel`）并返回其 logger 名。

    ``__init__`` 只把 ``ws`` 存进 ``self._ws``（不在构造期触碰），故传 ``object()``
    占位即可，不必起 aiohttp 连接。期望值与构造方无关，恒为
    ``"Module.output.websocket"``。

    Returns:
        str: 自动探测到的 logger 名（期望 ``"Module.output.websocket"``）。
    """
    from Module.output.websocket import WebSocketChannel

    # 同上：未公开 logger 访问器，只能读私有字段以核实探测结果。
    return WebSocketChannel(object())._logger.name
