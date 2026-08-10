#!/usr/bin/env python3
"""生成当前测试统计（F5：活跃文档手写测试数易漂移，改为脚本输出为唯一真相）。

用法:
  python scripts/print_test_stats.py [--marker M...]

默认打印全部四层 marker（unit/service/integration/slow）+ external + 全量收集数。
docs/testing.md 与 README 引用本脚本输出，不手写易漂移数字。
"""

from __future__ import annotations

import subprocess
import sys

_MARKERS = ("unit", "service", "integration", "slow", "external")


def _collect_count(marker_expr: str) -> int:
    """pytest --collect-only 对 marker 表达式收集数。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", marker_expr],
        capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    # 行形如 "3503 tests collected in 10.34s" / "36/3503 tests collected"
    for line in out.strip().splitlines():
        if "tests collected" in line:
            return int(line.strip().split()[0].split("/")[0])
    return -1


def main() -> int:
    total = _collect_count("")
    print(f"全量收集: {total}")
    for marker in _MARKERS:
        n = _collect_count(marker)
        print(f"  {marker}: {n}")
    # 合并门禁关键范围
    print(f"merge gate (not external): {_collect_count('not external')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
