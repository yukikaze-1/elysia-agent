"""Secret Store — 用户密钥本地存储。

模型 Profile 的用户密钥（API key）以 JSON 文件形式保存在服务器本地磁盘，
为批次 5 Profile 密钥读写提供基础能力。

安全要求：
  - 目录权限 0700、文件权限 0600（仅当前用户可读写）；
  - 原子写（tmp + os.replace），避免写一半损坏；
  - **损坏文件：读路径容错、写路径 fail-closed**（S2-B7b）：读时按"未配置"处理并打
    ERROR 日志（不让一个坏文件把启动/展示打断），但**拒绝写入**——`set_secret` 是
    load→改→整份 save，把损坏当空就会写出"只含本次一条"的新文件，**其余 provider 的
    密钥全部消失且没有任何副本**。写路径抛 `SecretStoreCorruptError`，现场原样保留。
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


class SecretStoreCorruptError(RuntimeError):
    """密钥文件损坏 → **拒绝写入**（S2-B7b）。

    为什么不能"当成空文件继续写"：`set_secret` 是 load → 改 → **整份 save**。把损坏
    当空 ⇒ 新文件只含本次写入的那一条，**其余 provider 的密钥全部消失**，而旧内容
    没有任何副本（`os.replace` 直接覆盖）——不可逆。故写路径 fail-closed：保留现场、
    抛异常，由人决定修还是删（这是"宁可留下来让调用方处理，也不静默丢数据"）。
    """

    def __init__(self, path: "Path", reason: str) -> None:
        super().__init__(
            f"密钥文件损坏，已拒绝写入以保护既有密钥：{path}（{reason}）。"
            "文件内容保持原样未改动；请人工确认后修复或移走该文件再重试。")
        self.path = path
        self.reason = reason


def _load(file_path: Path) -> dict[str, dict[str, str]]:
    """读取密钥文件。

    - 文件不存在 → `{}`
    - 正常 → 内容
    - **损坏 → 抛 `SecretStoreCorruptError`**：读路径自行降级（见 `_load_or_empty`），
      写路径**必须**让它冒泡（否则就是上面那条不可逆的静默覆盖）。
    """
    if not file_path.exists():
        return {}
    try:
        raw: Any = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SecretStoreCorruptError(file_path, f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SecretStoreCorruptError(
            file_path, f"顶层不是 JSON 对象（{type(raw).__name__}）")
    data: dict[str, dict[str, str]] = {}
    for service, names in raw.items():
        if isinstance(names, dict):
            data[str(service)] = {str(name): str(value) for name, value in names.items()}
    return data


def _load_or_empty(file_path: Path) -> dict[str, dict[str, str]]:
    """读路径专用：损坏 → 打 ERROR 并当"没有配置"。

    读路径容错是**有意**的：一个坏文件不该让 Profile 列表/启动流程整体失败；调用方
    看到"未配置"后重新填一次密钥，而写路径会拒绝覆盖并给出可操作的错误——失败方向
    是"要人来做一步"，不是"数据没了"。
    """
    try:
        return _load(file_path)
    except SecretStoreCorruptError as exc:
        logger.error("SecretStore: %s —— 读路径按「未配置」处理", exc)
        return {}


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
    """读取密钥；不存在 → None（文件损坏同样按"未配置"处理，但会打 ERROR）。"""
    file_path = _resolve_path(path)
    return _load_or_empty(file_path).get(str(service), {}).get(str(name))


def set_secret(
    service: str, name: str, value: str, path: Path | str | None = None,
) -> None:
    """写入/覆盖密钥（原子写，模块级锁串行化防并发丢更新）。

    Raises:
        SecretStoreCorruptError: 现有文件损坏——**拒绝写入**，见该异常的说明。
    """
    file_path = _resolve_path(path)
    with _LOCK:
        data = _load(file_path)
        data.setdefault(str(service), {})[str(name)] = str(value)
        _save(file_path, data)


def delete_secret(service: str, name: str, path: Path | str | None = None) -> None:
    """删除密钥；不存在时静默成功（模块级锁串行化防并发丢更新）。

    文件损坏时**不写、不抛**：本操作本来就不需要写（删一个读不出来的键），抛错只会
    让"取消配置"这个动作失败得更响；打 ERROR 即可，写路径仍由 `set_secret` 把关。
    """
    file_path = _resolve_path(path)
    with _LOCK:
        try:
            data = _load(file_path)
        except SecretStoreCorruptError as exc:
            logger.error("SecretStore: %s —— 删除操作不写文件，直接返回", exc)
            return
        svc = data.get(str(service))
        if svc is None or str(name) not in svc:
            return
        del svc[str(name)]
        if not svc:
            del data[str(service)]
        _save(file_path, data)


def has_secret(service: str, name: str, path: Path | str | None = None) -> bool:
    """该密钥是否已配置（值非空）。文件损坏 → 视为未配置。"""
    file_path = _resolve_path(path)
    value = _load_or_empty(file_path).get(str(service), {}).get(str(name))
    return bool(value)


def all_configured(
    service: str, path: Path | str | None = None,
) -> set[str]:
    """返回该 service 下已配置密钥的 name 集合（值非空才算配置）。文件损坏 → 空集。"""
    file_path = _resolve_path(path)
    return {
        name
        for name, value in _load_or_empty(file_path).get(str(service), {}).items()
        if value
    }
