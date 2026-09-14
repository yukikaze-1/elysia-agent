#!/usr/bin/env python3
"""测试集合代数门禁（CI 性能专项，2026-09-14）。

## 为什么需要

CI push 曾同时跑两个 job：

    Quick     pytest -m "unit or service"                        （4912 条）
    Normal    pytest -m "not slow and not external"              （4986 条）

`Quick ⊆ Normal`，于是 **4912/4986 = 98.5% 的 Normal 是 Quick 的逐字重复**——
一次 push 把同一批用例跑了两遍（runner 侧实测：Quick 5m34s + Normal 5m05s 串行）。
整改方式是把 Normal 拆成与 Quick **互斥**的余集：

    Quick       -m "unit or service"
    remainder   -m "not slow and not external and not unit and not service"

正确性要求两条集合恒等式（本脚本就是它们的可执行判据）：

    Quick ∪ remainder == Normal          （没有测试被漏掉）
    Quick ∩ remainder == ∅               （没有测试被跑两遍）

## 为什么这两条不是"新约定"，而是既有标记纪律的推论

设 A = (unit ∨ service)、B = ¬(slow ∨ external)。则

    Quick ∩ remainder = A ∧ B ∧ ¬A = ∅                       —— 恒真（代数）
    Quick ∪ remainder = A ∨ (B ∧ ¬A) = A ∨ B = B ⟺ A ⊆ B

即两条恒等式**等价于** `docs/testing.md`「标记纪律」里早已写下的那条不变量：

    (slow | integration | external) and (unit | service) 必须为空

所以本脚本的真正判据是「没有测试同时带快门禁 marker 与慢/外部 marker」，
「无 marker 测试」与「快/慢 marker 交叠」这两条**不在本脚本里查**——它们由
根 `conftest.py::pytest_collection_modifyitems` 在**每个** pytest 进程（含每个
xdist worker）内、`-m` 过滤**之前**断言（零额外收集成本）。本脚本只做需要独立
收集才能验证的「表达式代数」。

## 判据必须"拒答"而不是"假绿"（这一条踩过一次）

CLAUDE.md「门禁的三种假绿」③：环境不满足时门禁必须非零退出 + 不打印通过标记。
**集合代数有一个天然假绿面**：若三次收集都因为拼错 marker 名而返回空集，
`∅ ∪ ∅ == ∅` 平凡成立，门禁会报绿而实际一条都没跑。

两条防线：

1. **非退化**：三次收集的 pytest 自报"总收集数"必须一致且 > 0
   （`-q --collect-only` 会打印 `N/M tests collected`，M 即总数），
   否则退出 2 拒答；
2. **逐行数与自报数一致**：从 stdout 数出的 nodeid 行数必须等于自报选中数，
   否则输出格式可能已变、判据不可信，退出 2 拒答。

> 注：`--strict-markers` **不能**防 `-m` 表达式里的 marker 拼写错误
> （实测 `-m "unitt or service"` 在 `--strict-markers` 下仍静默只选中 service），
> 所以这条防线必须由本脚本自己承担。

用法:
    python scripts/check_test_sets.py            # 人类可读报告
    python scripts/check_test_sets.py --quiet    # 只打印结论行

退出码: 0=恒等式成立；1=集合代数被破坏；2=无法判定（拒答）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 表达式单源——与 Makefile / .github/workflows/ci.yml 必须一致。
# 改这里也要改那两处。
EXPR_QUICK = "unit or service"
EXPR_NORMAL = "not slow and not external"
EXPR_REMAINDER = "not slow and not external and not unit and not service"

_COLLECT_TIMEOUT_S = 600
"""单次收集上限——收集本身约 10s；600s 只用于把"卡死"变成可判定的拒答。"""

_COUNTED_RE = re.compile(r"^(\d+)(?:/(\d+))? tests collected")
_DESELECTED_RE = re.compile(r"^(\d+) deselected")
_NO_TESTS_RE = re.compile(r"^no tests collected \((\d+) deselected\)")


def _collect(marker_expr: str) -> tuple[set[str], int, str]:
    """跑一次 `pytest --collect-only -q -m <expr>`。

    Returns:
        (选中 nodeid 集合, 本次收集的总测试数, 错误说明)。
        错误说明非空表示**无法判定**（调用方必须以退出码 2 拒答，不得当作通过）。
    """
    cmd = [
        sys.executable, "-m", "pytest",
        "--collect-only", "-q",
        "-p", "no:cacheprovider",
    ]
    if marker_expr:
        cmd += ["-m", marker_expr]
    try:
        proc = subprocess.run(
            cmd, cwd=_PROJECT_ROOT, capture_output=True, text=True,
            timeout=_COLLECT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return set(), 0, f"收集超时（>{_COLLECT_TIMEOUT_S}s）：-m {marker_expr!r}"

    out = proc.stdout + proc.stderr
    # 退出码 5 = "no tests collected"：对余集表达式而言这是**正常数据**（设计上就
    # 可能只有个位数），不是无法判定；把它当错误会让门禁在健康状态下拒答。
    if proc.returncode not in (0, 5):
        tail = "\n".join(out.strip().splitlines()[-15:])
        return set(), 0, f"pytest 退出码 {proc.returncode}：-m {marker_expr!r}\n{tail}"

    nodeids = {ln.strip() for ln in out.splitlines() if "::" in ln}
    selected, total = _reported_counts(out)
    if selected is None or total is None:
        return set(), 0, f"无法从输出解析 'N/M tests collected'：-m {marker_expr!r}"
    # 反假绿：逐行数与自报选中数必须一致，否则"集合"本身不可信。
    if selected != len(nodeids):
        return set(), 0, (
            f"逐行 nodeid 数（{len(nodeids)}）与 pytest 自报收集数（{selected}）不一致："
            f"-m {marker_expr!r}——输出格式可能已变，判据不可信"
        )
    return nodeids, total, ""


def _reported_counts(out: str) -> tuple[int | None, int | None]:
    """解析 pytest 的自报计数，返回 (选中数, 总收集数)。

    三种输出形态：
      - `N/M tests collected (K deselected)` —— 有 `-m` 过滤且选中 > 0
      - `M tests collected`                  —— 无 `-m` 过滤（selected == total）
      - `no tests collected (M deselected)`  —— 有 `-m` 过滤但选中 = 0
    """
    for line in out.splitlines():
        stripped = line.strip()
        m = _NO_TESTS_RE.match(stripped)
        if m:
            return 0, int(m.group(1))
        m = _COUNTED_RE.match(stripped)
        if m:
            selected = int(m.group(1))
            total = int(m.group(2)) if m.group(2) else selected
            return selected, total
        m = _DESELECTED_RE.match(stripped)
        if m:
            return 0, int(m.group(1))
    return None, None


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    args = argv if argv is not None else sys.argv[1:]
    quiet = "--quiet" in args

    if "--print-expressions" in args:
        print(f"quick={EXPR_QUICK}")
        print(f"normal={EXPR_NORMAL}")
        print(f"remainder={EXPR_REMAINDER}")
        return 0

    collected = {}
    for label, expr in (
        ("quick", EXPR_QUICK),
        ("normal", EXPR_NORMAL),
        ("remainder", EXPR_REMAINDER),
    ):
        nodeids, total, err = _collect(expr)
        if err:
            print(f"测试集合门禁无法判定（拒答）：\n  {err}")
            return 2
        collected[label] = (nodeids, total)

    quick, total_q = collected["quick"]
    normal, total_n = collected["normal"]
    remainder, total_r = collected["remainder"]

    # ── 判据 0：非退化——三次收集必须看到同一个非空全量
    if total_q != total_n or total_n != total_r or total_n == 0:
        print("测试集合门禁无法判定（拒答）：三次收集的总测试数不一致或为 0"
              f"（quick={total_q} normal={total_n} remainder={total_r}）"
              "——集合代数在退化输入上会平凡成立，不能据此报绿")
        return 2

    problems: list[str] = []

    # ── 判据 1：Quick ∪ remainder == Normal（无遗漏）
    union = quick | remainder
    if union != normal:
        lost = sorted(normal - union)
        extra = sorted(union - normal)
        problems.append(
            f"Quick ∪ remainder ≠ Normal：遗漏 {len(lost)} 条、多出 {len(extra)} 条")
        problems.extend(f"  遗漏: {n}" for n in lost[:10])
        problems.extend(f"  多出: {n}" for n in extra[:10])

    # ── 判据 2：Quick ∩ remainder == ∅（无重复）
    both = quick & remainder
    if both:
        problems.append(f"Quick ∩ remainder ≠ ∅：{len(both)} 条被跑两遍")
        problems.extend(f"  重复: {n}" for n in sorted(both)[:10])

    # ── 判据 3：「无 marker 测试」与「快/慢 marker 交叠」不在这里查
    # 它们由根 `conftest.py::pytest_collection_modifyitems` 在**每个** pytest 进程
    # （含每个 xdist worker）内、`-m` 过滤之前断言——零额外收集成本，且覆盖全量。
    # 本脚本只负责"表达式代数"这一件需要独立收集才能验证的事。

    if problems:
        print("测试集合门禁失败：")
        for p in problems:
            print(f"  {p}")
        return 1

    if quiet:
        print(
            f"测试集合门禁通过：Quick {len(quick)} + remainder {len(remainder)} "
            f"= Normal {len(normal)}（全量 {total_n}），交集 0")
    else:
        print("测试集合门禁通过：")
        print(f"  |Quick|      = {len(quick)}   ({EXPR_QUICK})")
        print(f"  |remainder|  = {len(remainder)}   ({EXPR_REMAINDER})")
        print(f"  |Normal|     = {len(normal)}   ({EXPR_NORMAL})")
        print(f"  |Quick ∩ remainder| = {len(both)}（互斥）")
        print(f"  |Quick ∪ remainder| = {len(union)} == |Normal|")
        print(f"  全量收集 = {total_n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
