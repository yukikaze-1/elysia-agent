"""自定义 provider 持久化 — 读写 custom_providers.yaml。"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("custom_providers.yaml")

# 文件初始骨架
_SKELETON = {"llm": [], "stt": [], "tts": []}


def load(path: str | Path | None = None) -> dict:
    """读取自定义 provider 配置。文件不存在时返回空骨架。"""
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return {k: list(v) for k, v in _SKELETON.items()}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.warning("Failed to load %s, using empty config", p, exc_info=True)
        return {k: list(v) for k, v in _SKELETON.items()}
    result = {}
    for key in _SKELETON:
        result[key] = list(data.get(key, []))
    return result


def save(data: dict, path: str | Path | None = None) -> None:
    """保存自定义 provider 配置到 YAML 文件。"""
    p = Path(path) if path else _DEFAULT_PATH
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 自定义接入端点 — 通过前端设置面板管理，不手动编辑\n")
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def add_provider(service: str, entry: dict, path: str | Path | None = None) -> dict:
    """添加一个自定义 provider，返回更新后的完整数据。

    entry 格式: {"name": "siliconflow", "base_url": "...", "api_key_env": "...", "models": [...]}
    """
    data = load(path)
    if service not in data:
        raise ValueError(f"Unknown service: {service}")
    # 同名覆盖
    existing = [e for e in data[service] if e.get("name") == entry["name"]]
    if existing:
        data[service].remove(existing[0])
    data[service].append(entry)
    save(data, path)
    return data


def remove_provider(service: str, name: str, path: str | Path | None = None) -> dict:
    """删除一个自定义 provider，返回更新后的完整数据。"""
    data = load(path)
    if service not in data:
        raise ValueError(f"Unknown service: {service}")
    data[service] = [e for e in data[service] if e.get("name") != name]
    save(data, path)
    return data
