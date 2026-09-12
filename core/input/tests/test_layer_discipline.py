"""输入层私有穿透守卫（ARCH-INC-P1-02 回归）。

## 被固化的漂移

批次 A（`5f841510`）用 `ToolAdapter` 管理门面清掉 **9 处**
`agent.tool_adapter._manager` 穿透，但 health 探针与 `/pending` 路由这 **2 处**
不在那 9 处里（分别来自更早的批次 D 与批次 3）。评估稿因此一度写着
「`_manager` 穿透已清零」，而代码里其实还剩 2 处——文档与代码不符近一个月。

## 为什么既有门禁看不见

穿透有**两种形态**，`scripts/check_architecture.py` 的 R3 都看不见：

1. 字符串形式 `getattr(obj, "_priv")`——R3 只看 import 语句，AST 层面看不见字符串；
2. 点式深读 `agent.output._channels`——R3 只管**跨包导入私有符号**，不管属性访问。

## 本文件的规则（CLAUDE.md 的可执行化）

> 跨模块消费禁止 `agent._xxx`，一律走公开接口；debug 层深度内省用
> `# noqa: SLF001` 显式豁免。

即：输入层生产代码里每一处私有穿透，**要么改走公开 API，要么显式标注豁免**。
未标注即红——「漏改一处」不再能悄悄通过。

## 检测口径

用 AST 判定（`self` / `cls` 开头的自访问不算穿透），再按**同一行**是否带
`noqa: SLF001` 判定豁免。注释与 docstring 里提及私有名不算违规——初版按行扫描时
正是这样误伤了自己写的说明注释。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_INPUT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _INPUT_DIR.parent.parent

_ATTR_FUNCS = ("getattr", "hasattr", "setattr", "delattr")
_NOQA = "noqa: SLF001"


def _is_own_state(node: ast.expr) -> bool:
    """是否为「直接访问自己的私有成员」（`self._x` / `cls._x`）。

    只放过**一跳**。两点都重要：

    - 根**未知**（如 `getattr(getattr(agent, "tool_adapter", None), "_manager")`
      的内层是 `Call`）不能放行——放行它正是当初那处漂移躲过所有检查的原因；
    - `self._engine._reflector` 的宿主是 `self._engine`（别的对象），同属穿透，
      早期按「属性链最左端是 self」放行，`self._output._channels` 这类全部漏网。
    """
    return isinstance(node, ast.Name) and node.id in ("self", "cls")


def _penetration(node: ast.AST) -> str | None:
    """该 AST 节点是否构成跨模块私有穿透；是则返回被访问的私有名。"""
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


def _scan_source(source: str) -> list[tuple[int, str]]:
    """返回 (行号, 私有名)；只报**未标注豁免**的穿透。"""
    lines = source.splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        name = _penetration(node)
        if name is None:
            continue
        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        if _NOQA not in line:
            hits.append((node.lineno, name))
    return sorted(hits)


def _production_files() -> list[Path]:
    """`core/input` 下的生产文件（测试替身代表调用方，不受此约束）。"""
    return [
        p for p in sorted(_INPUT_DIR.rglob("*.py"))
        if "tests" not in p.relative_to(_INPUT_DIR).parts
    ]


def test_input_layer_has_no_unannotated_private_penetration() -> None:
    """输入层每一处私有穿透都必须改走公开 API 或标注 `# noqa: SLF001`。"""
    offenders: list[str] = []
    for path in _production_files():
        for lineno, name in _scan_source(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno} → {name}")

    assert offenders == [], (
        "输入层出现**未标注**的私有穿透——两种处置，任选其一：\n"
        "  ① 改走公开 API（缺接口就补到对应子系统：Module/output 的 channels/ws/send_json、"
        "Agent 的只读 property、CognitiveLoop.process_lock 都是这么补出来的）；\n"
        "  ② 确属 debug 层深层内省 → 同行加 `# noqa: SLF001` 并写明理由。\n"
        "违规点：\n" + "\n".join(offenders)
    )


def test_guard_is_not_vacuous(tmp_path: Path) -> None:
    """守卫必须真的能命中：扫描面非空，且两种穿透形态都检出、标注与自访问不误报。"""
    assert _production_files(), "扫描面为空——守卫会静默失效"

    probe = tmp_path / "probe.py"
    probe.write_text(
        'tm = getattr(getattr(agent, "tool_adapter", None), "_manager", None)\n'
        "chans = agent.output._channels\n"
        "ok_annotated = engine._version_repo  # noqa: SLF001 — debug 深读\n"
        "mine = self._private\n"
        "other = getattr(obj, 'normal_name', None)\n"
        "# 注释里提到 agent.output._channels 与 \"_manager\" 都不该算违规\n",
        encoding="utf-8",
    )
    assert _scan_source(probe.read_text(encoding="utf-8")) == [
        (1, "_manager"), (2, "_channels"),
    ], "两种穿透形态必须都检出；标注行、self 自访问、注释提及必须都不报"
