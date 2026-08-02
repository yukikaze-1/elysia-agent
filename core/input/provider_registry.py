"""Provider 注册表过滤 — 读 providers.yaml，组装 /api/providers 响应。

env 过滤规则（spec「env 过滤规则」）：
1. custom（meta-provider）与 models 为空的 provider → 跳过（不进下拉、不进 unconfigured）。
2. local: true → 恒显示（内置默认地址，无需配置）。
3. env_key 非空且 os.getenv 已设 → 已配置。
4. url_key 非空且 os.getenv 已设 → 已配置。
5. 否则 → 未配置，进 unconfigured[service]（env_vars 列出非空项）。
"""
from __future__ import annotations

import os

from Module.config.providers import load_provider_defaults

_SERVICES = ("llm", "stt", "tts")


def is_provider_configured(cfg: dict) -> bool:
    """判定 provider 是否已配置（local 恒 True；custom/空 models 由调用方跳过）。"""
    if cfg.get("local"):
        return True
    env_key = cfg.get("env_key", "")
    url_key = cfg.get("url_key", "")
    if env_key and os.getenv(env_key):
        return True
    if url_key and os.getenv(url_key):
        return True
    return False


def _missing_env_vars(cfg: dict) -> list[str]:
    """返回能让 provider 满足配置的非空 env_key/url_key 项（unconfigured 引导用）。"""
    out: list[str] = []
    for key in ("env_key", "url_key"):
        val = cfg.get(key, "")
        if val:
            out.append(val)
    return out


def build_providers_response(custom_data: dict) -> dict:
    """组装 /api/providers 响应：已配置 + local 进 data[service]，未配置进 unconfigured。

    Args:
        custom_data: custom_providers.load() 结果（{"llm": [...], "stt": [...], "tts": [...]}）。

    Returns:
        {"llm": {...}, "stt": {...}, "tts": {...}, "custom": custom_data, "unconfigured": {...}}。
    """
    result: dict = {"custom": custom_data, "unconfigured": {s: [] for s in _SERVICES}}
    for service in _SERVICES:
        providers = load_provider_defaults(service)
        service_dict: dict[str, dict] = {}
        unconf: list[dict] = []
        for name, cfg in providers.items():
            if name == "custom" or not cfg.get("models"):
                continue  # meta-provider / NotImplemented：不进下拉、不进 unconfigured
            if is_provider_configured(cfg):
                service_dict[name] = {"models": cfg.get("models", [])}
            else:
                unconf.append({"name": name, "env_vars": _missing_env_vars(cfg)})
        result[service] = service_dict
        result["unconfigured"][service] = unconf
    return result
