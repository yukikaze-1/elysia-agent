#!/usr/bin/env python3
"""超时预算门禁（#47 ①）——把"外层预算 ≥ 内层"从注释变成可执行断言。

## 为什么需要它

本次全仓审查找到 **4 处「外层预算 < 内层」**，每处都是同一个形状：外层 `wait_for`
在预算耗尽时取消内层，把内层的收尾（cancel 传播、孤儿清理、semaphore 释放）**从中间
掐断**。最典型的是 S2-A3：

    core/agent/_lifecycle_task_system.py:  wait_for(task_system.stop(), timeout=5.0)
    Module/workflow_engine/workflow_engine.py: stop() 内部 10 + 3 + 3 + 1 ≈ 17s

外层 5s < 内层 17s —— 只要有任意任务仍在 RUNNING（后台任务引擎的常态），外层**必然**
先超时。当时两条预算的一致性只写在注释里（"改内层任何一段都要同步改这里"），
**注释不是机制**：下一次改内层的人不会看见它。

## 三条规则

| 规则 | 内容 | 形态 |
|---|---|---|
| R1 | **词法嵌套**：嵌套的 `wait_for`/`wait`/`timeout` 预算必须内 ≤ 外 | 全自动，违规即红 |
| R2 | **登记的不变量**：`_INVARIANTS` 里每条 `(外层, 内层)` 必须 `外层 ≥ 内层`，且两侧都要解析得到（陈旧条目 → 红） | 显式登记 |
| R3 | **外层预算必须表态**：任何**具名预算常量**被当作超时实参使用时，必须以"外层"身份出现在 `_INVARIANTS`，或在 `_LEAF_BUDGETS` 登记"它没有内层预算"及理由 | 覆盖守卫 |

R3 是这套门禁的关键：R1 与 R2 都只在有人**写下来**之后才生效，而"忘了写"正是缺陷的
成因。R3 让每个新的具名超时常量**必须回答**"我覆盖谁"，回答不了就得写明"我是叶子"。

## 引用（ref）语法

    <相对路径>::<NAME>               模块级常量
    <相对路径>::<Class>.<NAME>        类级常量
    <相对路径>::<Class>.<method>      方法 —— 预算 = 其函数体内所有可解析
    <相对路径>::<function>            函数    超时预算之和 + `timeout` 形参默认值

函数形的"内层预算"取**求和**而非最大值：顺序等待的最坏耗时是各段之和，取 max 会低估。

## 解析边界（诚实声明）

- 只认**静态可解析**的预算：数字字面量、模块/类常量、由 `+ - * /`、`min`/`max` 组成的
  表达式。运行期变量（`self._timeout`、函数参数 `timeout`、`node.max_duration_s`）解析
  不到 → 计入 `--list` 的"未解析"报告，**不**当作违规（否则门禁会被噪声淹没而被人关掉）。
- **不做跨模块调用图推断**：仓库里 `stop`/`close`/`ping` 这类方法名大量重名，按名解析
  歧义极高（实测：全仓 `wait_for` 目标里只有 1 对能唯一解析）。所以跨函数的预算关系
  靠 R2 显式登记——这是**有意的取舍**，不是没做完。

用法:

    python scripts/check_timeout_budget.py           # 违规 → 退出码 1
    python scripts/check_timeout_budget.py --list    # 附带列出全部具名预算与未解析项
"""

from __future__ import annotations

import argparse
import re
import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("core", "Module")

# ── R2：已登记的预算不变量 ────────────────────────────────────────────────────
# (外层 ref, 内层 ref, 理由)。**新增外层预算时在这里登记它覆盖谁。**
_INVARIANTS: list[tuple[str, str, str]] = [
    (
        "core/agent/_lifecycle_task_system.py::_LifecycleTaskSystem._TASK_SYSTEM_STOP_BUDGET",
        "Module/workflow_engine/workflow_engine.py::WorkflowEngine.stop",
        "S2-A3：外层必须 ≥ WorkflowEngine.stop 的整条最坏路径（等自然完成 + cancel 后"
        "二次等待 + 孤儿工具 + cancel 落地），否则 stop() 被从中间掐断，"
        "运行中任务在已关闭的依赖上继续 await、semaphore 永不释放",
    ),
    (
        "core/tool_runtime/scheduler.py::_DISPATCH_TIMEOUT",
        "core/tool_runtime/scheduler.py::_CALL_BUDGET",
        "S3-02：dispatch 的整体等待上限必须 ≥ 单调用最坏预算，否则会掐断**设计内**的"
        "单工具最坏路径（1 次首发 + max_retries 次重试 + 退避）",
    ),
    (
        "core/tool_runtime/scheduler.py::_CALL_BUDGET",
        "core/tool_runtime/scheduler.py::_MAX_TOOL_TIMEOUT",
        "S2-G2：单调用预算必须 ≥ 单次尝试的**上界**（= 插件可声明的最大预算 + 管道余量）。"
        "用 60s 下限作基数会让声明 120s 的插件在第 3 次重试中途被整体预算掐断——"
        "「外层 < 内层」换个位置复发",
    ),
    (
        "core/tool_runtime/scheduler.py::_MAX_TOOL_TIMEOUT",
        "core/tool_runtime/scheduler.py::_TOOL_TIMEOUT",
        "S2-G2：单次尝试的上界必须 ≥ 默认下限（否则上限会小于无条件生效的默认值）",
    ),
]

# ── R3：具名预算的"叶子"登记 ──────────────────────────────────────────────────
# 键 = 常量名（`NAME` 或 `Class.NAME`）。值 = 为什么它没有仓库内的内层预算。
_LEAF_BUDGETS: dict[str, str] = {
    "_DEFERRED_TEARDOWN_WAIT": "等的是自家协程收尾（非仓库内带预算的函数）",
    "_SSE_KEEPALIVE_INTERVAL": "SSE keepalive 心跳间隔，等的是队列取数",
    "_HEALTH_TIMEOUT": "探针超时，目标是 health factory（外部 I/O）",
    "STT_TIMEOUT": "单次 STT 调用超时，目标是 STT 后端（外部 provider）",
    "_CLOSE_FLUSH_BUDGET_S": "关帧前刷音频缓冲，目标是 aiohttp 发送（外部 I/O）",
    "_TOOL_TIMEOUT": "单次工具执行超时，目标是插件（外部副作用），是预算链的最内层",
    "_TASK_RESULT_STORE_TIMEOUT": "任务终态结果的一次 Mongo 读写（#6），目标是驱动"
                                  "（外部 I/O），仓库内没有更内层的预算；它同时是关停"
                                  "路径的**活性**上界——驱动默认 serverSelectionTimeout 30s，"
                                  "网络黑洞下一条落库能把停机拖住 30s（超时即降级为不落库）",
    "_CHANNEL_SEND_TIMEOUT": "输出通道单帧写预算，目标是 WS 客户端/终端（外部 I/O）；\n                              它同时是**活性**上界：客户端不读数据时背压会让 send_json 永久挂起，\n                              而 _broadcast 是所有帧的必经之路（S2-H1）",
}

# `--list` 报告用：这些是"运行期才知道"的预算，静态解析不到，不算违规。
_UNRESOLVED_NOTE = "运行期值（形参/实例属性/契约字段），静态不可解析——见 --list"

# ── R4：插件声明的执行预算必须 ≤ 调度器容许的上界（S2-G2）──────────────────────
# 背景：`SafetyProfile.request_timeout` 是**插件自己声明的**单次执行预算，而调度器的
# 单次尝试上界是 `_MAX_TOOL_TIMEOUT`。两者是"两套独立声明、无人校验大小关系"的典型
# ——desktop/vision 按设计声明 120s（≥ 桥超时），而调度器一律 60s 硬切，设计的路径
# 在 60s 被悄悄截断。**R1–R3 都看不见它**：120 是经 `SafetyProfile(request_timeout=…)`
# **字段**传进管道的，不是"具名常量直接当 timeout 实参"，所以 R3 的判据覆盖不到。
_CEILING_REF = "core/tool_runtime/scheduler.py::_MAX_PLUGIN_REQUEST_TIMEOUT"
_PLUGIN_RELPATH = re.compile(r"^core/tools/[^/]+/plugin\.py$")


# ── AST 工具 ──────────────────────────────────────────────────────────────────

def _const_eval(node: ast.AST | None, consts: dict[str, float]) -> float | None:
    """静态求值：数字字面量、常量名、`+ - * /`、`min`/`max`。解析不到返回 None。"""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return consts.get(f"{node.value.id}.{node.attr}")
    if isinstance(node, ast.BinOp):
        op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}.get(type(node.op))
        if op is None:
            return None
        left, right = _const_eval(node.left, consts), _const_eval(node.right, consts)
        if left is None or right is None:
            return None
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if right == 0:
            return None
        return left / right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("min", "max"):
        vals = [_const_eval(a, consts) for a in node.args]
        if not vals or any(v is None for v in vals):
            return None
        return min(vals) if node.func.id == "min" else max(vals)  # type: ignore[type-var]
    return None


def _collect_consts(tree: ast.Module) -> dict[str, float]:
    """模块级 + 类级数值常量（类级以 `Class.NAME` 入表）。"""
    out: dict[str, float] = {}

    def walk(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                value = _const_eval(node.value, out)
                if value is not None:
                    out[prefix + node.targets[0].id] = value
            elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                    and node.value is not None):
                value = _const_eval(node.value, out)
                if value is not None:
                    out[prefix + node.target.id] = value
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")

    walk(tree.body)
    return out


def _wait_kind(call: ast.Call) -> str | None:
    """是等待/超时调用则返回种类名（wait_for / wait / timeout）。"""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in ("wait_for", "wait", "timeout"):
        return func.attr
    return None


def _timeout_node(call: ast.Call, kind: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == "timeout":
            return kw.value
    if kind == "timeout" and call.args:
        return call.args[0]
    return None


def _waits_in(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[ast.Call, str, ast.AST | None]]:
    """函数体内的等待调用——**不含嵌套函数**（它们有自己的预算域）。"""
    out: list[tuple[ast.Call, str, ast.AST | None]] = []

    def rec(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                kind = _wait_kind(child)
                if kind is not None:
                    out.append((child, kind, _timeout_node(child, kind)))
            rec(child)

    rec(fn)
    return out


class _Module:
    """一个生产模块的索引。"""

    def __init__(self, path: Path, rel: str) -> None:
        self.path = path
        self.rel = rel
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.consts = _collect_consts(self.tree)
        self.funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[node.name] = node
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.methods[f"{node.name}.{item.name}"] = item

    def find_fn(self, dotted: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        return self.funcs.get(dotted) or self.methods.get(dotted)


def _scan(root: Path | None = None) -> dict[str, _Module]:
    """扫描生产代码。`root` 可指向合成 fixture 仓库（供单测用，见 scripts/tests/）。"""
    base_root = root or _ROOT
    out: dict[str, _Module] = {}
    for base in _SCAN_DIRS:
        for path in sorted((base_root / base).rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(base_root).as_posix()
            out[rel] = _Module(path, rel)
    return out


def _fn_budget(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, consts: dict[str, float],
) -> tuple[float, list[str]]:
    """函数的总预算 = `timeout` 形参默认值 + 函数体内全部可解析等待预算之和。"""
    total = 0.0
    parts: list[str] = []
    args = fn.args
    defaults = list(args.defaults)
    if defaults:
        named = [a.arg for a in args.args][-len(defaults):]
        for name, default in zip(named, defaults, strict=True):
            if "timeout" not in name:
                continue
            value = _const_eval(default, consts)
            if value is not None:
                total += value
                parts.append(f"{name}={value:g}")
    for call, _kind, tnode in _waits_in(fn):
        value = _const_eval(tnode, consts)
        if value is None:
            continue
        total += value
        parts.append(f"L{call.lineno}:{value:g}")
    return total, parts


def _resolve_ref(ref: str, mods: dict[str, _Module]) -> float | None:
    """解析 `<path>::<NAME|Class.NAME|Class.method|function>` → 秒数。"""
    if "::" not in ref:
        return None
    rel, dotted = ref.split("::", 1)
    mod = mods.get(rel)
    if mod is None:
        return None
    if dotted in mod.consts:
        return mod.consts[dotted]
    fn = mod.find_fn(dotted)
    if fn is None:
        return None
    total, _parts = _fn_budget(fn, mod.consts)
    return total


# ── 规则 ──────────────────────────────────────────────────────────────────────

def _check_lexical(mods: dict[str, _Module]) -> list[str]:
    """R1：词法嵌套的等待预算必须内 ≤ 外。"""
    bad: list[str] = []
    for rel, mod in mods.items():
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Call):
                continue
            outer_kind = _wait_kind(node)
            if outer_kind is None:
                continue
            outer = _const_eval(_timeout_node(node, outer_kind), mod.consts)
            if outer is None:
                continue
            for inner in ast.walk(node):
                if inner is node or not isinstance(inner, ast.Call):
                    continue
                inner_kind = _wait_kind(inner)
                if inner_kind is None:
                    continue
                inner_val = _const_eval(_timeout_node(inner, inner_kind), mod.consts)
                if inner_val is None:
                    continue
                if inner_val > outer:
                    bad.append(
                        f"{rel}:{node.lineno} 外层 "
                        f"{outer_kind}({outer:g}) < 内层 L{inner.lineno} "
                        f"{inner_kind}({inner_val:g})——外层会先超时并把内层从中间掐断")
    return bad


def _check_invariants(mods: dict[str, _Module]) -> list[str]:
    """R2：登记的 (外层, 内层) 必须外层 ≥ 内层，且两侧都要解析得到。"""
    bad: list[str] = []
    for outer_ref, inner_ref, note in _INVARIANTS:
        outer = _resolve_ref(outer_ref, mods)
        inner = _resolve_ref(inner_ref, mods)
        if outer is None:
            bad.append(f"陈旧登记：外层 {outer_ref} 解析不到（已改名/删除？）")
            continue
        if inner is None:
            bad.append(f"陈旧登记：内层 {inner_ref} 解析不到（已改名/删除？）")
            continue
        if outer < inner:
            bad.append(
                f"预算不变量被破坏：外层 {outer_ref} = {outer:g}s < "
                f"内层 {inner_ref} = {inner:g}s\n     理由（登记时写明）：{note}")
    return bad


def _check_outer_registered(mods: dict[str, _Module]) -> tuple[list[str], list[str]]:
    """R3：具名预算常量必须表态（登记为外层 或 登记为叶子）。

    Returns:
        (违规列表, `--list` 报告行)。
    """
    bad: list[str] = []
    report: list[str] = []
    registered_outer = {ref.split("::", 1)[1] for ref, _inner, _n in _INVARIANTS}
    for rel, mod in mods.items():
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Call):
                continue
            kind = _wait_kind(node)
            if kind is None:
                continue
            tnode = _timeout_node(node, kind)
            if tnode is None:
                continue
            name: str | None = None
            if isinstance(tnode, ast.Name):
                name = tnode.id
            elif isinstance(tnode, ast.Attribute) and isinstance(tnode.value, ast.Name):
                # `self.X` / `node.field` 是实例属性或契约字段（运行期值）；`Class.X`
                # 才是类级常量（首字母大写不是可靠判据，但 `self`/`cls` 是）。
                if tnode.value.id in ("self", "cls"):
                    bare = tnode.attr
                    if bare in registered_outer or bare in _LEAF_BUDGETS:
                        report.append(
                            f"  [已登记] {rel}:{node.lineno} {kind}({tnode.value.id}.{bare})")
                    else:
                        report.append(
                            f"  [未解析] {rel}:{node.lineno} {kind}({tnode.value.id}.{bare})"
                            f" —— {_UNRESOLVED_NOTE}")
                    continue
                if tnode.value.id.islower():
                    report.append(
                        f"  [未解析] {rel}:{node.lineno} {kind}({tnode.value.id}.{tnode.attr})"
                        f" —— {_UNRESOLVED_NOTE}")
                    continue
                name = f"{tnode.value.id}.{tnode.attr}"
            if name is None:
                continue
            if name in registered_outer or name in _LEAF_BUDGETS:
                report.append(f"  [已登记] {rel}:{node.lineno} {kind}({name})")
                continue
            if name not in mod.consts and "." not in name:
                report.append(
                    f"  [未解析] {rel}:{node.lineno} {kind}({name}) —— 不是本模块的数值常量"
                    f"（{_UNRESOLVED_NOTE}）")
                continue
            bad.append(
                f"{rel}:{node.lineno} 具名预算 {name} 未表态——它覆盖谁？\n"
                f"     若它包着仓库内带预算的函数/调用：在 _INVARIANTS 登记 "
                f"(外层=它, 内层=被覆盖者)；\n"
                f"     若它是预算链的最内层：在 _LEAF_BUDGETS 登记名字 + 理由")
    return bad, report


def _check_plugin_request_timeouts(mods: dict[str, _Module]) -> list[str]:
    """R4：每个插件的 `SafetyProfile(request_timeout=…)` 必须 ≤ 调度器上界。

    值必须**静态可解析**（字面量或本文件内的数值常量）。解析不了就报违规——
    在这里"无法判定"等于"可能已悄悄超过上界"，**拒答**而不是放行
    （CLAUDE.md「门禁的三种假绿」③）。
    """
    ceiling = _resolve_ref(_CEILING_REF, mods)
    if ceiling is None:
        return [f"R4 无法判定：{_CEILING_REF} 解析不到——调度器的上界常量被改名或删除？"
                f"（拒答，不报绿）"]
    bad: list[str] = []
    seen = 0
    for rel, mod in sorted(mods.items()):
        if not _PLUGIN_RELPATH.match(rel):
            continue
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "SafetyProfile"):
                continue
            for kw in node.keywords:
                if kw.arg != "request_timeout":
                    continue
                seen += 1
                value = _const_eval(kw.value, mod.consts)
                if value is None:
                    bad.append(f"R4 [{rel}:{node.lineno}] `request_timeout` 静态解析不到"
                               f"（拒答）——改用字面量或本文件的数值常量，"
                               f"否则无法校验它是否超过上界 {ceiling:g}s")
                elif value > ceiling:
                    bad.append(f"R4 [{rel}:{node.lineno}] 插件声明 request_timeout="
                               f"{value:g}s > 调度器上界 {ceiling:g}s——"
                               f"要么在 core/tool_runtime/scheduler.py 抬"
                               f"`_MAX_PLUGIN_REQUEST_TIMEOUT`（并同步 _CALL_BUDGET/"
                               f"_DISPATCH_TIMEOUT），要么调小插件预算")
    if seen == 0:
        bad.append("R4 未发现任何 `SafetyProfile(request_timeout=…)` 声明——"
                   "扫描面失效（插件目录改名？），拒答而不是报绿")
    return bad


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：违规 → 退出码 1。"""
    parser = argparse.ArgumentParser(description="超时预算门禁（#47 ①）")
    parser.add_argument("--list", action="store_true", help="列出全部具名预算与未解析项")
    parser.add_argument("--root", default=None, help="仓库根（默认脚本所在仓库）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else None
    mods = _scan(root)
    lexical = _check_lexical(mods)
    invariants = _check_invariants(mods)
    outer_bad, report = _check_outer_registered(mods)
    plugin_bad = _check_plugin_request_timeouts(mods)
    bad = lexical + invariants + outer_bad + plugin_bad

    if args.list:
        print("具名预算盘点：")
        for line in sorted(set(report)):
            print(line)
        print(f"\n登记的不变量（{len(_INVARIANTS)} 条）：")
        for outer_ref, inner_ref, _note in _INVARIANTS:
            print(f"  {outer_ref}  ≥  {inner_ref}  "
                  f"= {_resolve_ref(outer_ref, mods)} / {_resolve_ref(inner_ref, mods)}")
        print(f"叶子预算（{len(_LEAF_BUDGETS)} 条）：" + ", ".join(sorted(_LEAF_BUDGETS)))
        print()

    if bad:
        print(f"超时预算违规 {len(bad)} 项：")
        for item in bad:
            print(f"  - {item}")
        print("\n背景：外层预算 < 内层时，外层超时会取消内层，把内层的收尾"
              "（cancel 传播 / 孤儿清理 / semaphore 释放）从中间掐断（S2-A3）。")
        return 1

    print(f"超时预算门禁通过：R1 词法嵌套递减 / R2 {len(_INVARIANTS)} 条登记不变量 / "
          f"R3 具名预算全部表态（叶子 {len(_LEAF_BUDGETS)} 条）/ "
          f"R4 插件声明预算 ≤ {_resolve_ref(_CEILING_REF, mods)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
