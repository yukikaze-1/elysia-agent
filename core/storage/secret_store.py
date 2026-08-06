"""Secret Store — 用户密钥本地存储。

模型 Profile 的用户密钥（API key）以 JSON 文件形式保存在服务器本地磁盘，
为批次 5 Profile 密钥读写提供基础能力。

安全要求：
  - 目录权限 0700、文件权限 0600（仅当前用户可读写）；
  - 原子写（tmp + os.replace），避免写一半损坏；
  - 损坏文件容错：读取时返回空 dict + warning 日志，不抛异常；
  - 密钥值永不打印（日志/API/WS 不泄露）。

数据格式（JSON 对象）:
    {
        "<service>": {"<name>": "<secret-value>", ...},
        ...
    }

Usage:
    from core.storage.secret_store import set_secret, get_secret

    set_secret("llm", "custom/openai", "sk-xxx")
    key = get_secret("llm", "custom/openai")
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from paths import ProjectPath

logger = logging.getLogger(__name__)

# 模块级锁：串行化 set/delete 的 load → 改 → save，防并发丢更新（TOCTOU）
_LOCK = threading.Lock()

# 默认存储目录（ProjectPath.DATA_SECRETS = "data/secrets"）
_DEFAULT_DIR: Path = ProjectPath.DATA_SECRETS.resolve()
# 默认存储文件名
_DEFAULT_FILE = "profiles.json"


def _resolve_path(path: Path | str | None) -> Path:
    """解析存储文件路径。

    path 为 None → 默认 `data/secrets/profiles.json`；
    path 为目录（已存在目录或无文件后缀）→ 目录下的 profiles.json；
    path 为文件路径（带后缀）→ 直接用该文件。
    """
    if path is None:
        return _DEFAULT_DIR / _DEFAULT_FILE
    p = Path(path)
    if p.is_dir() or not p.suffix:
        return p / _DEFAULT_FILE
    return p


def _load(file_path: Path) -> dict[str, dict[str, str]]:
    """读取密钥文件；文件不存在或损坏 → 返回空 dict（不抛异常）。"""
    if not file_path.exists():
        return {}
    try:
        raw: Any = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("SecretStore: 密钥文件损坏，按空处理 file=%s", file_path)
        return {}
    if not isinstance(raw, dict):
        logger.warning("SecretStore: 密钥文件结构非法，按空处理 file=%s", file_path)
        return {}
    data: dict[str, dict[str, str]] = {}
    for service, names in raw.items():
        if isinstance(names, dict):
            data[str(service)] = {str(name): str(value) for name, value in names.items()}
    return data


def _save(file_path: Path, data: dict[str, dict[str, str]]) -> None:
    """原子写：tmp 文件 + 目录/文件 0700/0600 + os.replace。

    tmp 文件与目标同目录，保证 os.replace 在同一文件系统内原子替换。
    """
    directory = file_path.parent
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, tmp_name = tempfile.mkstemp(
        prefix=file_path.name + ".", suffix=".tmp", dir=directory, text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # 写前 chmod 文件 0o600，即使 umask 宽松也保证仅属主可读写
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, file_path)
    except BaseException:
        # 清理临时文件，避免残留
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def get_secret(service: str, name: str, path: Path | str | None = None) -> str | None:
    """读取密钥；不存在 → None。"""
    file_path = _resolve_path(path)
    return _load(file_path).get(str(service), {}).get(str(name))


def set_secret(
    service: str, name: str, value: str, path: Path | str | None = None,
) -> None:
    """写入/覆盖密钥（原子写，模块级锁串行化防并发丢更新）。"""
    file_path = _resolve_path(path)
    with _LOCK:
        data = _load(file_path)
        data.setdefault(str(service), {})[str(name)] = str(value)
        _save(file_path, data)


def delete_secret(service: str, name: str, path: Path | str | None = None) -> None:
    """删除密钥；不存在时静默成功（模块级锁串行化防并发丢更新）。"""
    file_path = _resolve_path(path)
    with _LOCK:
        data = _load(file_path)
        svc = data.get(str(service))
        if svc is None or str(name) not in svc:
            return
        del svc[str(name)]
        if not svc:
            del data[str(service)]
        _save(file_path, data)


def has_secret(service: str, name: str, path: Path | str | None = None) -> bool:
    """该密钥是否已配置（值非空）。"""
    file_path = _resolve_path(path)
    value = _load(file_path).get(str(service), {}).get(str(name))
    return bool(value)


def all_configured(
    service: str, path: Path | str | None = None,
) -> set[str]:
    """返回该 service 下已配置密钥的 name 集合（值非空才算配置）。"""
    file_path = _resolve_path(path)
    return {
        name
        for name, value in _load(file_path).get(str(service), {}).items()
        if value
    }
