"""Compose 网络与凭据安全策略静态门禁（P1-19，2026-08-10 全项目 review）。

防止 docker-compose.yml 把无鉴权数据库服务暴露到宿主网络、或内置默认凭据。
对数据服务（mongodb/milvus/minio/etcd）与全部凭据环境变量检查：

1. 宿主端口只允许绑定 127.0.0.1（或 ::1）；`0.0.0.0:...`、裸 `27017:27017`、
   随机端口映射都会把服务暴露到所有接口，判定违规。
2. 凭据类环境变量（键含 PASSWORD/SECRET/TOKEN/ACCESS_KEY，大小写不敏感）必须是
   `${VAR:?error}` 强制引用或非空字面量——`""`、`minioadmin`、`root`、`password`
   等默认/空值判定违规（含 "空 token"）。
3. 用户名类环境变量（键含 ROOT_USER/USERNAME）的值不得字面等于 `root`。
4. 文件全文不得出现 `minioadmin`（历史默认凭据，防止出现在注释/示例中）。
5. 生产 profile 服务不得发布数据库管理端口（MinIO API/console 9000/9001、
   Milvus metrics 9091）。

本脚本只做静态结构检查（yaml.safe_load，不解析 `${VAR}` 插值）——即使插值后在宿主
环境能解析出真实值，文件层面也不允许 0.0.0.0 端口/默认密码/空 token。

用法：
    python scripts/check_compose_security.py [compose.yml]   # 默认 docker-compose.yml
                                                            # 违规打印并退出码 1
    python -c "from scripts.check_compose_security import check_compose_file; ..."
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_COMPOSE = "docker-compose.yml"

# 数据服务识别关键字（服务名包含任一关键字即视为数据服务）
_DATA_SERVICE_KEYWORDS = ("mongo", "milvus", "minio", "etcd")

# 凭据类环境变量键标记（大小写不敏感）
_SECRET_KEY_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "ACCESS_KEY")

# 用户名类环境变量键标记（值不得字面是 root）
_USERNAME_KEY_MARKERS = ("ROOT_USER", "USERNAME")

# 生产 profile 禁止发布的数据库管理端口：MinIO S3 API(9000) / MinIO console(9001) / Milvus metrics(9091)
_PRODUCTION_FORBIDDEN_PORTS = {9000, 9001, 9091}

# 常见默认弱凭据（字面命中即违规）
_BANNED_CRED_VALUES = {"minioadmin", "root", "password", "changeme", "admin"}

# ${VAR:?error} 强制引用（变量名 + 可选中英文错误消息）
_REQUIRED_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:\?[^}]*\}$")


def _is_data_service(name: str) -> bool:
    """服务名是否命中数据服务关键字。"""
    low = name.lower()
    return any(k in low for k in _DATA_SERVICE_KEYWORDS)


def _is_required_ref(value: Any) -> bool:
    """是否为 ${VAR:?error} 强制引用（缺 secret 时 compose 拒绝启动）。"""
    return isinstance(value, str) and bool(_REQUIRED_REF_RE.match(value))


def _iter_env(env: Any) -> list[tuple[str, str]]:
    """把 environment 的 dict/list 两种形态归一为 (key, value_str) 列表。

    Compose 允许 `environment:` 用映射（`KEY: value`）或列表
    （`- KEY=value`）两种写法，统一处理后便于后续检查。
    """
    if not env:
        return []
    if isinstance(env, dict):
        return [(str(k), str(v)) for k, v in env.items()]
    pairs: list[tuple[str, str]] = []
    for item in env:
        text = str(item)
        key, sep, value = text.partition("=")
        if sep:
            pairs.append((key.strip(), value.strip()))
    return pairs


def _iter_published_ports(ports: Any) -> list[dict[str, Any]]:
    """把 ports 列表归一为 [{host_ip, published, target, raw}]。

    兼容字符串（`127.0.0.1:27017:27017`）与映射（`{target, published, host_ip}`）
    两种写法；`published`/`host_ip` 缺失表示裸端口映射/随机端口。
    """
    result: list[dict[str, Any]] = []
    for entry in ports or []:
        if isinstance(entry, dict):
            result.append({
                "host_ip": entry.get("host_ip"),
                "published": entry.get("published"),
                "target": entry.get("target"),
                "raw": str(entry),
            })
            continue
        text = str(entry)
        parts = text.split(":")
        if len(parts) == 1:
            result.append({"host_ip": None, "published": None, "target": parts[0], "raw": text})
        elif len(parts) == 2:
            result.append({"host_ip": None, "published": parts[0], "target": parts[1], "raw": text})
        else:
            result.append({"host_ip": parts[0], "published": parts[1], "target": parts[2], "raw": text})
    return result


def _is_loopback(host_ip: Any) -> bool:
    """端口 host_ip 是否绑定到 loopback（127.0.0.1 / ::1 / localhost）。"""
    if host_ip is None:
        return False
    return str(host_ip).strip() in {"127.0.0.1", "::1", "localhost"}


def check_compose_text(compose_text: str) -> list[str]:
    """检查 compose 文本的网络暴露与凭据策略，返回违规列表（空 = 通过）。

    Args:
        compose_text: docker-compose.yml 文件文本。

    Returns:
        违规描述列表；为空表示无违规。
    """
    violations: list[str] = []
    if "minioadmin" in compose_text:
        violations.append("文件出现历史默认凭据 'minioadmin'")

    try:
        data = yaml.safe_load(compose_text) or {}
    except yaml.YAMLError as exc:
        return [f"Compose YAML 解析失败: {exc}"]

    services: dict[str, Any] = data.get("services", {}) or {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue

        # 1. 数据服务宿主端口只允许 loopback
        if _is_data_service(name):
            for p in _iter_published_ports(svc.get("ports")):
                if not _is_loopback(p["host_ip"]):
                    violations.append(
                        f"数据服务 '{name}' 发布端口 {p['raw']} 未绑定 127.0.0.1 —— "
                        "会对所有接口暴露；请改为 '127.0.0.1:<port>:<port>' 或去掉宿主端口")

        # 2/3. 凭据与用户名环境变量
        for key, value in _iter_env(svc.get("environment")):
            upper = key.upper()
            if any(m in upper for m in _SECRET_KEY_MARKERS):
                # 允许: ${VAR:?} 强制引用，或非空且非默认值的字面量（人工保证强随机）
                allowed = _is_required_ref(value) or (
                    value != "" and value.strip().lower() not in _BANNED_CRED_VALUES
                )
                if not allowed:
                    violations.append(
                        f"服务 '{name}' 凭据 {key} 使用默认值/空值 '{value}' —— "
                        "必须用 ${VAR:?} 强制引用或非空强随机值")
            if any(m in upper for m in _USERNAME_KEY_MARKERS):
                if value.strip().lower() == "root":
                    violations.append(
                        f"服务 '{name}' 用户名 {key} 字面等于 'root' —— "
                        "默认 root 用户是常见弱凭据，请改用独立用户名")

        # 5. 生产 profile 服务不发布数据库管理端口
        profiles = svc.get("profiles", []) or []
        if "production" in profiles:
            for p in _iter_published_ports(svc.get("ports")):
                target = p["target"]
                if target is None:
                    continue
                try:
                    if int(str(target)) in _PRODUCTION_FORBIDDEN_PORTS:
                        violations.append(
                            f"生产 profile 服务 '{name}' 发布数据库管理端口 {target} —— "
                            "production 不发布 MinIO/Milvus 管理端口")
                except ValueError:
                    # 非数字 target（如范围映射）不在管理端口清单内，忽略
                    pass
    return violations


def check_compose_file(path: str | Path) -> list[str]:
    """读取并检查一个 compose 文件，返回违规列表（空 = 通过）。

    Args:
        path: compose YAML 文件路径。

    Returns:
        违规描述列表；为空表示无违规。
    """
    return check_compose_text(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：检查 compose 文件，违规退出码 1。

    Args:
        argv: 命令行参数（可选 compose 文件路径）。

    Returns:
        0 表示通过，1 表示违规。
    """
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else _DEFAULT_COMPOSE
    violations = check_compose_file(path)
    if violations:
        print(f"Compose 安全策略检查失败：{path}")
        for v in violations:
            print(f"  - {v}")
        return 1
    print(f"Compose 安全策略检查通过：{path}（数据服务仅 loopback 端口、无默认凭据、无空 token、生产不发布管理端口）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
