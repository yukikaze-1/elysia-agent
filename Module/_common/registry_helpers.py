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
    """解析 API key：env var > explicit > provider 默认值。"""
    if explicit_key:
        return explicit_key
    return os.getenv(env_key, provider_cfg.get("default_api_key", ""))


def resolve_base_url(
    env_key: str,
    provider_cfg: dict,
    explicit_url: str | None = None,
) -> str:
    """解析 base URL：explicit > env var > provider 默认值。

    注意：explicit_url 用 is not None 检查（而非 truthiness），
    以区分"未传入"和"显式传入空字符串"。
    """
    if explicit_url is not None:
        return explicit_url
    env_val = os.getenv(env_key)
    if env_val is not None:
        return env_val
    return provider_cfg.get("base_url", "")


def inject_logger(
    module_name: str,
    explicit_logger: "Logger | None" = None,
) -> "Logger":
    """返回调用方 logger，允许显式覆盖。"""
    if explicit_logger is not None:
        return explicit_logger
    return logging.getLogger(module_name)
