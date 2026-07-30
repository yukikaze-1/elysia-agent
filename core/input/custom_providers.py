"""自定义 provider 持久化 — 读写 custom_providers.yaml。"""

import logging
from pathlib import Path

import yaml
from paths import ProjectPath

logger = logging.getLogger(__name__)

_DEFAULT_PATH = ProjectPath.CUSTOM_PROVIDERS_YAML.resolve()

# 文件初始骨架
_SKELETON = {"llm": [], "stt": [], "tts": []}


def load(path: str | Path | None = None) -> dict:
    """读取自定义 provider 配置。文件不存在时返回空骨架。

    Args:
        path: YAML 配置文件路径，默认使用 _DEFAULT_PATH。

    Returns:
        dict: 包含 llm/stt/tts 三个键的 provider 配置字典。
    """
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
    """保存自定义 provider 配置到 YAML 文件。

    Args:
        data: 包含 llm/stt/tts 三个键的 provider 配置字典。
        path: YAML 配置文件路径，默认使用 _DEFAULT_PATH。
    """
    p = Path(path) if path else _DEFAULT_PATH
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 自定义接入端点 — 通过前端设置面板管理，不手动编辑\n")
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def add_provider(service: str, entry: dict, path: str | Path | None = None) -> dict:
    """添加一个自定义 provider，返回更新后的完整数据。

    Args:
        service: 服务类型，必须为 "llm"、"stt" 或 "ttt" 之一。
        entry: provider 配置项，格式为
            {"name": "siliconflow", "base_url": "...", "api_key_env": "...", "models": [...]}。
        path: YAML 配置文件路径，默认使用 _DEFAULT_PATH。

    Returns:
        dict: 更新后的完整配置数据。

    Raises:
        ValueError: service 不在已知服务列表中。
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
    """删除一个自定义 provider，返回更新后的完整数据。

    Args:
        service: 服务类型，必须为 "llm"、"stt" 或 "ttt" 之一。
        name: 要删除的 provider 名称。
        path: YAML 配置文件路径，默认使用 _DEFAULT_PATH。

    Returns:
        dict: 更新后的完整配置数据。

    Raises:
        ValueError: service 不在已知服务列表中。
    """
    data = load(path)
    if service not in data:
        raise ValueError(f"Unknown service: {service}")
    data[service] = [e for e in data[service] if e.get("name") != name]
    save(data, path)
    return data
