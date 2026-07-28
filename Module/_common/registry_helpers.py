"""Registry 共享工具：api_key 解析、base_url 解析、logger 注入。"""
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
