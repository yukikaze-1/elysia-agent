"""跨包私有穿透守卫（ARCH-INC-P1-02）。

## 被固化的漂移

`core/input` 的私有穿透曾让评估稿写着「已清零」而代码里还剩 2 处，漂移近一个月。
根因不是没人检查，而是**检查不到**：`scripts/check_architecture.py` 的 R3 只管
**跨包导入私有符号**（`from pkg import _x`），而真实穿透走的是

1. 字符串形式 `getattr(obj, "_priv")`——没有 import 语句；
2. 点式深读 `obj.sub._priv`——R3 不管属性访问。

两者 AST 层面都看得见，只是 R3 的设计范围没覆盖。

## 判定方式

「跨包」无法纯语法判定（`obj` 的类型要推断），这里用**属性名归属**近似：

1. 扫全部生产代码，建立「私有属性名 → 定义它的包」映射
   （类体 `_x = ...`、实例 `self._x = ...`、**以及 `def _m` 方法定义**都算定义）；
2. 扫全部生产代码的私有访问，若该名字**只由别的包定义**，判为跨包穿透；
3. 穿透**要么改走公开 API，要么同行标注 `# noqa: SLF001` + 理由**（CLAUDE.md 规则）。

## 已知局限（诚实的边界）

- **同名属性会漏报**：若 `_lock` 同时由 `core/memory` 与 `core/personality` 定义，
  则两边访问都不判跨包。这是刻意的保守取向——宁可漏报也不误报，否则守卫会被
  大量同包访问淹没而失效；
- 本包内部的私有访问**不判违规**（数据类字段、模块级 helper、同包兄弟类），
  这是正常封装，不是穿透。

因此本守卫是**回归网**（挡住新增长出来的跨包穿透），不是完备审计工具；
完备审计仍需人工按层过一遍。

## 曾经的假绿：只收字段、不收方法（2026-09-17 Batch 2.1 修复）

本守卫修好了一个**「判据写错」型假绿**（门禁每次绿，绿的却不是因为它没问题）：

`_scan()` 只在归属表里查得到名字时才判违规（`if defining and …`），而旧版
`_private_attr_defs()` **只收赋值目标**（`ast.Assign` / `ast.AnnAssign`），
`def _foo` **一个都没收**。后果不是"偶尔漏一处"，而是**整类形态全量放行**：

    output._split_versions()      # 跨包私有方法**调用** —— 归属表查不到 → 静默通过
    agent.cognitive._phase        # 跨包私有**属性读** —— 归属表查得到 → 正常拦截

实测（生产代码里未标注的跨包穿透）：修复前 **0 处** → 修复后 **9 处**，
全部是真实泄漏——`core/adapter/tts_adapter.py` 8 处 + `core/cognitive/streaming.py`
1 处，都是调用 `Module/output/manager.py` 的 `OutputManager._broadcast`。

> 为什么此前没人发现：守卫自己的两个用例分别在验"归属表非空"（`len(owners) > 50`）
> 与"合成**字段**穿透会被识别"——而归属表被大量模块级 `_xxx = ...` 撑到远超 50，
> `_penetration()` 也确实认得出 `_reflector`。**两个用例都没问"方法名进表了吗"**，
> 于是它俩一直绿。这正是「守卫没装在改动上」型假绿：反证后全绿 ≠ 守卫强。

修复后新增三条回归用例（见文件末尾），删掉方法收集即变红——已实测。
"""
from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent.parent
_ROOTS = ("core", "Module")
_ATTR_FUNCS = ("getattr", "hasattr", "setattr", "delattr")
_NOQA = "noqa: SLF001"


def _pkg_of(path: Path) -> str:
    """文件所属「包」标识：core 下取二级目录，其余取顶层目录。"""
    parts = path.relative_to(_REPO).parts
    if parts[0] == "core" and len(parts) > 2 and (_REPO / parts[0] / parts[1] / "__init__.py").exists():
        return f"core/{parts[1]}"
    return parts[0]


def _production_files() -> list[Path]:
    """全部生产 Python 文件（测试豁免：替身本就要造形状）。"""
    out: list[Path] = []
    for root in _ROOTS:
        for p in sorted((_REPO / root).rglob("*.py")):
            if "__pycache__" in p.parts or "tests" in p.relative_to(_REPO).parts:
                continue
            out.append(p)
    return out


def _is_own_state(node: ast.expr) -> bool:
    """是否为「直接访问自己的私有成员」（`self._x` / `cls._x`）。

    只放过**一跳**：`self._engine._reflector` 的宿主是 `self._engine`（别的对象），
    属跨包穿透，必须检查。早期版本按「属性链最左端是 self」放行，于是
    `self._output._channels` 这类穿透全部漏网——注入探针实测证实。
    """
    return isinstance(node, ast.Name) and node.id in ("self", "cls")


def _penetration(node: ast.AST) -> str | None:
    """该节点是否构成私有访问；是则返回被访问的私有名。

    根是 `self` / `cls` 的自访问不算；根**未知**（内层是 Call 等）一律算——
    `getattr(getattr(agent, "tool_adapter", None), "_manager")` 正是靠「根未知」
    才躲过早期检查。
    """
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _ATTR_FUNCS and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)):
        name = node.args[1].value
        if name.startswith("_") and not name.startswith("__") \
                and not _is_own_state(node.args[0]):
            return name
    if isinstance(node, ast.Attribute) and node.attr.startswith("_") \
            and not node.attr.startswith("__") \
            and not _is_own_state(node.value):
        return node.attr
    return None


def _private_attr_defs(tree: ast.AST) -> set[str]:
    """本文件定义的私有符号名：**赋值目标 + 方法/函数的 ``def`` 名**。

    方法定义必须计入（2026-09-17 修复，见模块 docstring「曾经的假绿」）：早期版本
    只收 ``ast.Assign``/``ast.AnnAssign`` 的赋值目标，于是归属表里**一个方法名都没有**，
    ``_penetration`` 对 ``obj._private_method()`` 取到 ````name`` 后查表恒空、
    ``if defining and ...`` 恒假——**跨包私有方法调用全量静默放行**。特征是经典的
    「判据写错」型假绿：门禁每次都绿，绿的却不是因为它没问题。

    实测（修复前 vs 修复后，生产代码未标注的跨包穿透）：0 处 → 9 处，
    全部是真实泄漏（``core/`` 调用 ``OutputManager._broadcast`` / ``_split_versions``）。

    收 ``ast.walk`` 而非只看类体：模块级私有函数（``def _helper``）与嵌套函数同样是
    「本包定义的私有符号」，被跨包当属性调用时（``mod._helper()``）一样是穿透。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and not node.name.startswith("__"):
                names.add(node.name)
            continue
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id.startswith("_") and not t.id.startswith("__"):
                names.add(t.id)
            elif (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                  and t.value.id == "self" and t.attr.startswith("_")
                  and not t.attr.startswith("__")):
                names.add(t.attr)
    return names


def _scan() -> list[str]:
    """返回跨包穿透且未标注豁免的清单。"""
    parsed: list[tuple[Path, ast.AST, list[str]]] = []
    owners: dict[str, set[str]] = collections.defaultdict(set)

    for path in _production_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        try:
            tree = ast.parse("\n".join(lines))
        except SyntaxError:  # pragma: no cover - 仓库内不应出现
            continue
        parsed.append((path, tree, lines))
        pkg = _pkg_of(path)
        for name in _private_attr_defs(tree):
            owners[name].add(pkg)

    violations: list[str] = []
    for path, tree, lines in parsed:
        here = _pkg_of(path)
        for node in ast.walk(tree):
            name = _penetration(node)
            if name is None:
                continue
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if _NOQA in line:
                continue
            defining = owners.get(name, set())
            if defining and all(pkg != here for pkg in defining):
                rel = path.relative_to(_REPO)
                violations.append(
                    f"{rel}:{node.lineno} → .{name}（定义于 {'/'.join(sorted(defining))}）"
                )
    return sorted(violations)


def test_no_unannotated_cross_package_private_access() -> None:
    """跨包私有访问必须改走公开 API，或同行标注 `# noqa: SLF001` + 理由。"""
    violations = _scan()
    assert violations == [], (
        "出现跨包私有穿透——两种处置任选其一：\n"
        "  ① 改走公开 API（接口层见 core/personality/protocol.py；"
        "Module 侧补公开 property）；\n"
        "  ② 确属 debug 层深层内省 → 同行加 `# noqa: SLF001` 并写明理由。\n"
        "违规点：\n" + "\n".join(violations)
    )


def test_guard_is_not_vacuous() -> None:
    """守卫必须真的在比对：扫描面非空、归属表非空，且合成穿透会被判违规。"""
    files = _production_files()
    assert len(files) > 100, "扫描面为空——守卫会静默失效"

    owners: set[str] = set()
    for path in files:
        try:
            owners |= _private_attr_defs(ast.parse(path.read_text(encoding="utf-8")))
        except SyntaxError:  # pragma: no cover
            continue
    assert len(owners) > 50, "私有属性归属表过于稀疏——跨包判定会全面漏报"

    # 合成探针：`engine._reflector` 由 core/personality 定义，从 core/character 访问
    # 必须被判跨包；同包访问与自访问不得被判。
    probe = ast.parse(
        "a = engine._reflector\n"      # 跨包（若访问方不在 core/personality）
        "b = self._private\n"          # 自访问 → 不算
        "c = obj._not_defined_anywhere\n"  # 归属表查不到 → 保守放过
    )
    found = {_penetration(n) for n in ast.walk(probe)} - {None}
    assert found == {"_reflector", "_not_defined_anywhere"}, (
        f"穿透识别逻辑异常：{found}"
    )


# ══════════════════════════════════════════════════════════════════
# 私有**方法**覆盖（2026-09-17 Batch 2.1 修复的假绿，回归网）
# ══════════════════════════════════════════════════════════════════
#
# 为什么这组用例必须存在：`_scan()` 只在 `owners` 表里查得到名字时才判违规
# （`if defining and ...`），而修复前的 `_private_attr_defs` **只收赋值目标**——
# 归属表里一个方法名都没有，于是 `obj._private_method()` 与
# `obj._private_method` 全部静默放行。这正是「判据写错」型假绿：门禁每次绿，
# 绿的却不是因为它没问题。生产代码实测漏掉 9 处（core → `OutputManager._broadcast`）。
#
# 下面三条把「归属表含方法定义」这一前提钉死：删除方法收集 → 立即变红。


def test_private_method_definitions_enter_the_owners_table() -> None:
    """方法名必须进入归属表——否则跨包方法调用永远无法被判定。

    直接对真实文件断言（不合成）：这些名字是「只由 Module 定义的私有方法」，
    修复前在归属表里查不到。
    """
    tree = ast.parse((_REPO / "Module/output/manager.py").read_text(encoding="utf-8"))
    names = _private_attr_defs(tree)
    for expected in ("_broadcast", "_split_versions"):
        assert expected in names, (
            f"`{expected}` 未进入归属表——跨包调用 `output.{expected}(...)` 会被静默放行。"
            "若该方法已改名/删除，请同步更新本用例与调用点。"
        )


def test_cross_package_private_method_call_is_flagged() -> None:
    """跨包私有**方法调用**必须判违规（修复前漏报的核心形态）。

    用合成树走 `_scan()` 的判定逻辑：`Module` 侧定义一个私有方法，`core` 侧调用它。
    """
    probe = ast.parse(
        "def _mod_private():\n"       # 定义（归属 Module 之外的包时构成跨包）
        "    return 1\n"
        "async def caller(m):\n"
        "    return await m._mod_private()\n"   # 跨包调用 → 必须可见
    )
    assert "_mod_private" in _private_attr_defs(probe), (
        "`def _x` 未计入归属表——跨包私有方法调用会全量漏报（2026-09 的假绿形态）"
    )
    found = {_penetration(n) for n in ast.walk(probe)} - {None}
    assert "_mod_private" in found, f"`m._mod_private()` 未被识别为穿透：{found}"


def test_same_package_private_method_call_is_not_flagged() -> None:
    """同包内的方法定义 + 调用不得判违规（防修过头造成假阳性）。"""
    # core/output/x.py 定义 `_broadcast`，core/output/y.py 调用它 → 同包，放过。
    probe = ast.parse(
        "class Out:\n"
        "    def _broadcast(self):\n"
        "        return 1\n"
        "def use(o):\n"
        "    return o._broadcast()\n"
    )
    names = _private_attr_defs(probe)
    assert "_broadcast" in names
    # 归属表里该名字只属于「本包」时，`all(pkg != here ...)` 为假 → 不判违规。
    owners = {"_broadcast": {"core/output"}}
    here = "core/output"
    assert not all(pkg != here for pkg in owners["_broadcast"]), (
        "同包私有方法调用被判违规——规则过宽，会产生大量假阳性"
    )