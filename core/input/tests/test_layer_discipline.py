"""输入层不得穿透 `ToolAdapter` 私有成员（ARCH-INC-P1-02 回归守卫）。

## 被固化的漂移

批次 A（`5f841510`）用 `ToolAdapter` 管理门面清掉 **9 处**
`agent.tool_adapter._manager` 穿透，但 health 探针与 `/pending` 路由这 **2 处**
不在那 9 处里（分别来自更早的批次 D 与批次 3）。评估稿因此一度写着
「`_manager` 穿透已清零」，而代码里其实还剩 2 处——文档与代码不符近一个月。

## 为什么既有门禁看不见

两处用的是 `getattr(obj, "_manager", None)` 的**字符串形式**；
`scripts/check_architecture.py` 的 R3 只看 import 语句，AST 层面看不见这个字符串。

## 检测口径

用 `tokenize` 只认**代码里的字符串字面量**，注释与 docstring 中提及该名字不算违规
（否则一句「原为 getattr(x, "_manager") 穿透」的说明注释就会误报——本守卫初版按行
扫描时正是这样误伤了自己）。
"""
from __future__ import annotations

import tokenize
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_INPUT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _INPUT_DIR.parent.parent

#: 字符串形式的私有成员名——只有 `getattr(x, "_manager")` 这类写法会命中。
_FORBIDDEN_LITERALS = frozenset({'"' + "_manager" + '"', "'" + "_manager" + "'"})


def _penetration_lines(path: Path) -> list[int]:
    """返回文件中「字符串字面量形式的私有成员名」所在行号（忽略注释）。"""
    hits: list[int] = []
    with path.open("rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type == tokenize.STRING and tok.string in _FORBIDDEN_LITERALS:
                hits.append(tok.start[0])
    return hits


def _production_files() -> list[Path]:
    """`core/input` 下的生产文件（测试替身代表调用方，不受此约束）。"""
    return [
        p for p in sorted(_INPUT_DIR.rglob("*.py"))
        if "tests" not in p.relative_to(_INPUT_DIR).parts
    ]


def test_input_layer_does_not_penetrate_tool_manager() -> None:
    """core/input 生产代码不得经字符串形式访问 `ToolAdapter._manager`。"""
    offenders: list[str] = []
    for path in _production_files():
        for lineno in _penetration_lines(path):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")

    assert offenders == [], (
        "输入层出现 ToolAdapter 私有成员穿透——请改走 ToolAdapter 公开门面；"
        "门面缺方法就补到 core/adapter/tool_adapter.py 的管理门面段：\n"
        + "\n".join(offenders)
    )


def test_guard_is_not_vacuous(tmp_path: Path) -> None:
    """守卫必须真的能命中：扫描面非空，且合成穿透被检出、注释提及不误报。"""
    assert _production_files(), "扫描面为空——守卫会静默失效"

    probe = tmp_path / "probe.py"
    probe.write_text(
        "# 说明注释：原写法 getattr(adapter, " + '"_manager"' + ") 已清除\n"
        'tm = getattr(getattr(agent, "tool_adapter", None), "_manager", None)\n'
        'other = getattr(agent, "_other", None)\n',
        encoding="utf-8",
    )
    assert _penetration_lines(probe) == [2], "既未命中真实穿透，或把注释/无关属性也算进去了"
