"""Registry 共享工具：api_key 解析、base_url 解析、logger 注入。"""
import inspect
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger


def resolve_api_key(
    env_key: str,
    provider_cfg: dict,
    explicit_key: str | None = None,
) -> str:
    """解析 API key：env var > explicit > provider 默认值。

    Args:
        env_key: 环境变量名（如 "DEEPSEEK_API_KEY"）。
        provider_cfg: provider 默认值字典（含 "default_api_key" 键）。
        explicit_key: 显式传入的 API key，优先级最高。

    Returns:
        str: 解析后的 API key 字符串。
    """
    if explicit_key:
        return explicit_key
    return os.getenv(env_key, provider_cfg.get("default_api_key", ""))


def resolve_base_url(
    env_key: str,
    provider_cfg: dict,
    explicit_url: str | None = None,
    config_key: str = "base_url",
) -> str:
    """解析 base URL：explicit > env var > provider 默认值。

    注意：explicit_url 用 is not None 检查（而非 truthiness），
    以区分"未传入"和"显式传入空字符串"。

    Args:
        env_key: 环境变量名（如 "DEEPSEEK_BASE_URL"）。
        provider_cfg: provider 默认值字典。
        explicit_url: 显式传入的 URL，优先级最高。
        config_key: provider_cfg 中的回退键名（默认 "base_url"，beta url 用 "beta_url"）。

    Returns:
        str: 解析后的 base URL 字符串。
    """
    if explicit_url is not None:
        return explicit_url
    env_val = os.getenv(env_key)
    if env_val is not None:
        return env_val
    return provider_cfg.get(config_key, "")


def inject_logger(
    module_name: str,
    explicit_logger: "Logger | None" = None,
) -> "Logger":
    """返回调用方 logger，允许显式覆盖。

    Args:
        module_name: 调用方模块名（如 "Module.llm.deepseek"）。
        explicit_logger: 显式传入的 Logger 实例，传入非 None 时直接返回。

    Returns:
        Logger: 最终的 logger 实例。
    """
    if explicit_logger is not None:
        return explicit_logger
    return logging.getLogger(module_name)


def get_caller_logger() -> logging.Logger:
    """返回**调用方模块**的 logger——模块名经调用栈自动探测，无需调用方传 `__name__`。

    与 :func:`inject_logger` 的分工：那个要求调用方自己给出模块名；本函数用于
    ``__init__`` 内部这种拿不到显式名称的注入点（``Module/output/manager.py`` /
    ``websocket.py`` 的 ``logger or get_caller_logger()``）。

    **单跳 ``f_back``，不要改成双跳。** 调用点都是直接调用、中间没有工厂层，
    双跳会把 logger 名归属到调用方的调用者。这不是洁癖：``core/utils/log_setup.py``
    按 **logger 名**把日志分流到不同文件（``core.storage.*`` → mongo.log、
    ``core.memory.long_term`` → milvus.log、其余 → app.log），名字错一层，日志就进错文件。

    （本函数原先住在 ``Module/logger/factory.py``；该模块的 ``LoggerFactory`` 类
    经核实从未在生产激活，已于 2026-09-12 随整个 ``Module/logger/`` 包删除，
    只留本函数迁到此处。）
    """
    frame = inspect.currentframe()
    try:
        caller = frame.f_back  # type: ignore[union-attr]
        module_name = caller.f_globals.get("__name__", "__main__")  # type: ignore[union-attr]
        return logging.getLogger(module_name)
    finally:
        del frame
