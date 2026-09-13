#!/usr/bin/env python3
"""依赖锁漂移校验（F2 / S2-9）：requirements.txt 与 environment.lock.yml 的**版本比对**。

`environment.lock.yml`（`conda env export -n Elysia`）是精确可复现锁；requirements.txt
是 runtime 声明源。本脚本校验两件事：

1. **存在性**：requirements.txt 每个顶层包在锁的 pip 段有精确 pin；
2. **版本满足**（S2-9 补）：锁中的版本**确实满足**声明的 specifier。

为什么第 2 条必须有：此前只比包名——把 `pydantic>=2.0` 改成 `pydantic>=99.0` 门禁
照样通过（实测退出码 0），于是"声明的下限"与"实际装的版本"可以任意脱节；
真到环境重建时才发现装不上，而 CI 从没跑过那个组合。典型真实场景：在
requirements.txt 抬了下限却忘了 `make lock-env`。

用法: python scripts/check_env_lock.py
退出码: 0=一致；1=漂移（列出缺 pin 与版本不满足的包）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REQS = _PROJECT_ROOT / "requirements.txt"
_LOCK = _PROJECT_ROOT / "environment.lock.yml"


def parse_requirements(text: str) -> list[Requirement]:
    """requirements.txt → Requirement 列表（跳过注释/空行/选项行）。

    无法解析的行**显式告警**而不是静默忽略——写错格式的依赖声明会变成
    "门禁看不见的空洞"。
    """
    out: list[Requirement] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.split("#", 1)[0].strip()      # 去掉行内注释
        if not line:
            continue
        try:
            out.append(Requirement(line))
        except InvalidRequirement as e:           # pragma: no cover - 防御
            print(f"⚠ 无法解析的依赖声明（已跳过，请修正格式）: {raw!r} — {e}")
    return out


def parse_lock_pins(lock_text: str) -> dict[str, str]:
    """environment.lock.yml 的 pip 段 → {规范化包名: 精确版本}。"""
    pip_sec = lock_text.split("  - pip:")[-1] if "  - pip:" in lock_text else ""
    pins: dict[str, str] = {}
    for line in pip_sec.splitlines():
        m = re.match(r"^\s+- ([A-Za-z0-9_.\-]+)==(\S+)", line)
        if m:
            pins[canonicalize_name(m.group(1))] = m.group(2)
    return pins


def compare(reqs: list[Requirement], pins: dict[str, str]) -> list[str]:
    """返回问题描述列表（空 = 通过）。

    两条独立判据：缺 pin（存在性）与版本不满足（S2-9）。
    """
    problems: list[str] = []
    for req in reqs:
        name = canonicalize_name(req.name)
        locked = pins.get(name)
        if locked is None:
            problems.append(f"缺 pin: {req.name} 不在 environment.lock.yml 的 pip 段")
            continue
        if not req.specifier:
            continue                      # 未声明版本（如 `tzdata`）→ 只要求存在
        try:
            version = Version(locked)
        except InvalidVersion:
            problems.append(f"锁中版本无法解析: {req.name}=={locked}")
            continue
        if not req.specifier.contains(version, prereleases=True):
            problems.append(
                f"版本不满足: 声明 {req.name}{req.specifier} 但锁是 {locked}"
                f"——抬了下限后需 `make lock-env` 重生成锁")
    return problems


def main() -> int:
    reqs = parse_requirements(_REQS.read_text(encoding="utf-8"))
    pins = parse_lock_pins(_LOCK.read_text(encoding="utf-8"))

    problems = compare(reqs, pins)
    if not problems:
        print(f"依赖锁校验通过：requirements.txt {len(reqs)} 个顶层包"
              f"均在锁中且有精确 pin，版本满足声明")
        return 0
    print(f"依赖锁漂移（{len(problems)}）:")
    for p in problems:
        print(f"  - {p}")
    print("运行 `make lock-env`（conda env export -n Elysia）重新生成锁后提交")
    return 1


if __name__ == "__main__":
    sys.exit(main())
