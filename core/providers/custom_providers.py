"""自定义 provider 持久化 — 读写 custom_providers.yaml + Secret Store 密钥（api_key 不入 YAML）。

CORE-MOD-P1-01 从 core/input/custom_providers.py 迁入 core/providers。
"""

import logging
import os
from pathlib import Path

import yaml
from core.storage.secret_store import delete_secret, get_secret, set_secret
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


def add_provider(
    service: str,
    entry: dict,
    path: str | Path | None = None,
    secret_path: str | Path | None = None,
) -> dict:
    """添加/更新自定义 provider，返回更新后的完整数据。

    api_key 非空 → 直存 Secret Store（**不入 YAML**）；同名 upsert 不带 api_key → 保留
    既有 secret（upsert 不丢密钥）；runtime 切到 desktop/local → 清除该条目 secret（D18）。

    Args:
        service: 服务类型，必须为 "llm"、"stt" 或 "tts" 之一。
        entry: provider 配置项，格式为
            {"name": ..., "base_url": ..., "api_key": ..., "api_key_env": ...,
             "runtime": ..., "capabilities": ..., "models": [...]}。
        path: YAML 配置文件路径，默认使用 _DEFAULT_PATH。
        secret_path: Secret Store 存储路径，默认 None（生产用默认目录）。

    Returns:
        dict: 更新后的完整配置数据。

    Raises:
        ValueError: service 不在已知服务列表中。
    """
    data = load(path)
    if service not in data:
        raise ValueError(f"Unknown service: {service}")
    name = entry["name"]
    # 同名覆盖
    existing = [e for e in data[service] if e.get("name") == name]
    if existing:
        data[service].remove(existing[0])
    runtime = entry.get("runtime", "backend")
    api_key = entry.get("api_key") or ""
    if runtime == "backend" and api_key:
        # 仅 backend 由后端执行：api_key 直存 Secret Store，永不入 YAML
        set_secret(service, name, api_key, path=secret_path)
    elif runtime != "backend":
        # D18：runtime 切到 desktop/local，后端不再执行该 Profile → 清除既有密钥
        delete_secret(service, name, path=secret_path)
    # 写入 YAML 时剥离 api_key（密钥只存 Secret Store）
    entry_to_save = {k: v for k, v in entry.items() if k != "api_key"}
    data[service].append(entry_to_save)
    save(data, path)
    return data


def remove_provider(
    service: str,
    name: str,
    path: str | Path | None = None,
    secret_path: str | Path | None = None,
) -> dict:
    """删除一个自定义 provider，返回更新后的完整数据。同时清除 Secret Store 对应密钥。

    Args:
        service: 服务类型，必须为 "llm"、"stt" 或 "tts" 之一。
        name: 要删除的 provider 名称。
        path: YAML 配置文件路径，默认使用 _DEFAULT_PATH。
        secret_path: Secret Store 存储路径，默认 None（生产用默认目录）。

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
    # 删除条目同时清除 Secret Store 密钥（D9）
    delete_secret(service, name, path=secret_path)
    return data


def resolve_model_params(
    service: str,
    model_id: str,
    secret_path: str | Path | None = None,
) -> dict:
    """解析切换/测试模型所需参数（switch + /api/providers/test 共用）。

    custom 条目返回 {"base_url", "api_key"?, "runtime"}；api_key 解析顺序 =
    Secret Store 直存 > api_key_env env 变量（D3）；内置 provider 保持返回 {}。

    Args:
        service: 服务类型，"llm"/"stt"/"tts"。
        model_id: 完整模型标识，如 "custom/myllm" 或 "deepseek/deepseek-v4-flash"。
        secret_path: Secret Store 存储路径，默认 None（生产用默认目录）。

    Returns:
        dict: "custom/name" → 查 custom_providers.yaml[name]，返回
            {"base_url": ..., "api_key": ..., "runtime": ...}（api_key 为空时跳过，
            runtime 缺省 "backend"）；内置 provider → {}。
    """
    if "/" not in model_id or model_id.split("/", 1)[0] != "custom":
        return {}
    name = model_id.split("/", 1)[1]
    for entry in load()[service]:
        if entry.get("name") == name:
            params = {
                "base_url": entry.get("base_url", ""),
                "runtime": entry.get("runtime", "backend"),
            }
            # api_key 解析：Secret Store 直存优先，api_key_env env 兜底（D3）
            api_key = get_secret(service, name, path=secret_path)
            if api_key is None:
                env_name = entry.get("api_key_env", "")
                api_key = os.getenv(env_name, "") if env_name else ""
            # 空 api_key 不传——避免 "" 泄漏进 switch/test 调用（assert_called_once_with 严格相等会失败）
            if api_key:
                params["api_key"] = api_key
            return params
    return {}
