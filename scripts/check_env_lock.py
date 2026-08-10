#!/usr/bin/env python3
"""依赖锁漂移校验（F2）：requirements.txt 顶层包必须出现在 environment.lock.yml。

`environment.lock.yml`（`conda env export -n Elysia`）是精确可复现锁；requirements.txt
是 runtime 声明源。本脚本校验：requirements.txt 每个顶层包在锁的 pip 段有精确 pin——
新增依赖必须同步重新生成锁（`make lock-env`），否则 CI 红（防声源与锁漂移）。

用法: python scripts/check_env_lock.py
退出码: 0=一致；1=漂移（列出缺 pin 的包）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REQS = _PROJECT_ROOT / "requirements.txt"
_LOCK = _PROJECT_ROOT / "environment.lock.yml"


def main() -> int:
    reqs = _REQS.read_text(encoding="utf-8")
    lock = _LOCK.read_text(encoding="utf-8")

    # requirements.txt 顶层包名（跳过注释/空行/env 标记行）
    req_pkgs: set[str] = set()
    for line in reqs.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 形式: pkg>=X / pkg>=X,<Y / pkg[extra]>=X / pkg~=X ——取包名（允许 [extra]）
        m = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)", line)
        if m:
            req_pkgs.add(m.group(1).split("[")[0].lower().replace("_", "-"))

    # 锁的 pip 段精确 pin 集合（pkg==ver）
    pip_sec = lock.split("  - pip:")[-1] if "  - pip:" in lock else ""
    locked: set[str] = set()
    for line in pip_sec.splitlines():
        m = re.match(r"^\s+- ([A-Za-z0-9_.\-]+)==(\S+)", line)
        if m:
            locked.add(m.group(1).lower().replace("_", "-"))

    missing = sorted(p for p in req_pkgs if p not in locked)
    if not missing:
        print(f"依赖锁校验通过：requirements.txt {len(req_pkgs)} 个顶层包均在锁中")
        return 0
    print(f"requirements.txt 有但 environment.lock.yml 缺 pin（{len(missing)}）: {', '.join(missing)}")
    print("运行 `make lock-env`（conda env export -n Elysia）重新生成锁后提交")
    return 1


if __name__ == "__main__":
    sys.exit(main())
