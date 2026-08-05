"""Provider 注册表过滤 — 读 providers.yaml，组装 /api/providers 响应。

env 过滤规则（spec「env 过滤规则」）：
1. custom（meta-provider）与 models 为空的 provider → 跳过（不进下拉、不进 unconfigured）。
2. local: true → 恒显示（内置默认地址，无需配置）。
3. env_key 非空且 os.getenv 已设 → 已配置。
4. url_key 非空且 os.getenv 已设 → 已配置。
5. 否则 → 未配置，进 unconfigured[service]（env_vars 列出非空项）。

批次 5（Task 6）：ProviderEntry 增 runtime/credentialConfigured/capabilities——
内置条目 runtime 恒 backend + 空 capabilities + credentialConfigured（已配置恒 True）；
custom 条目 enrich credentialConfigured（Secret Store 直存 ∪ api_key_env env，D3 同源）
+ runtime/capabilities（缺省 backend/{}）。
"""
from __future__ import annotations

import os
from pathlib import Path

from Module.config.providers import load_provider_defaults
from core.storage import secret_store

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


def build_providers_response(
    custom_data: dict,
    secret_path: str | Path | None = None,
) -> dict:
    """组装 /api/providers 响应：已配置 + local 进 data[service]，未配置进 unconfigured。

    批次 5（Task 6）：内置条目带 credentialConfigured/runtime/capabilities 新字段
    （runtime 恒 backend + capabilities 空 + credentialConfigured 已配置恒 True）；
    custom 条目 enrich credentialConfigured（Secret Store all_configured ∪ api_key_env env）
    + runtime/capabilities（缺省 backend/{}）。

    Args:
        custom_data: custom_providers.load() 结果（{"llm": [...], "stt": [...], "tts": [...]}）。
        secret_path: Secret Store 存储路径，默认 None（生产用默认目录）。

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
                service_dict[name] = {
                    "models": cfg.get("models", []),
                    "credentialConfigured": True,
                    "runtime": "backend",
                    "capabilities": {},
                }
            else:
                unconf.append({"name": name, "env_vars": _missing_env_vars(cfg)})
        result[service] = service_dict
        result["unconfigured"][service] = unconf
    # custom 条目 enrich credentialConfigured（Secret Store 直存 ∪ api_key_env env）
    for service in _SERVICES:
        configured = secret_store.all_configured(service, path=secret_path)
        for entry in result["custom"].get(service, []):
            env_key = entry.get("api_key_env", "")
            env_configured = bool(env_key and os.getenv(env_key))
            entry["credentialConfigured"] = entry.get("name", "") in configured or env_configured
            entry.setdefault("runtime", "backend")
            entry.setdefault("capabilities", {})
    return result
