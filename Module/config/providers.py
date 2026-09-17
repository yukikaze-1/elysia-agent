"""加载 providers.yaml，返回各模块的 provider 默认配置。

使用方式:
    from Module.config.providers import load_provider_defaults
    cfg = load_provider_defaults("tts")  # {"gpt-sovits": {...}, "custom": {...}, "minimax": {...}}
"""

from __future__ import annotations

from pathlib import Path

import yaml

_PROVIDERS: dict = {}
_LOADED = False


def _load() -> dict:
    global _PROVIDERS, _LOADED
    if not _LOADED:
        path = Path(__file__).parent / "providers.yaml"
        _PROVIDERS = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _LOADED = True
    return _PROVIDERS


def load_provider_defaults(module_name: str) -> dict:
    """返回指定模块的 provider 默认配置。

    module_name: "llm" | "tts" | "stt" | "embedding"
    返回: {provider_name: {base_url, env_key, ...}}
    """
    return dict(_load().get(module_name, {}))
