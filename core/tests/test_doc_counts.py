# 测试文件：core/tests/test_doc_counts.py
"""S3-22：文档里的**可数事实**必须与代码一致，且必须**机械核对**。

这一项是报告点名的"文档漂移"：`core/README.md` 称"39 个 `@property`"、`CLAUDE.md`
称"45 个"，而实际是 45——两边都写在正文里、都靠人肉同步，于是必然漂移。

"靠人记住同步"不是约束。这里把可数事实变成断言：**文档里的数字从代码里数出来核对**。
新增 property 忘了改文档 → 红。

**边界**：只核对"能从代码机械数出来"的事实（property 数、测试计数这类）。测试耗时
之类的数字依赖机器，只做"够不够新"的量级检查，不写死秒数。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]


def _count_properties() -> int:
    """`core/agent/agent.py` 里 `@property` 装饰的成员数。"""
    tree = ast.parse((_ROOT / "core/agent/agent.py").read_text(encoding="utf-8"))
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Name) and d.id == "property"
                for d in node.decorator_list)
    )


def _stated_counts(text: str) -> list[int]:
    """文档里"通过 **N 个同名 `@property`**"声明的数字。"""
    return [int(m) for m in re.findall(r"\*\*(\d+) 个同名 `@property`\*\*", text)]


@pytest.mark.parametrize("doc", ["CLAUDE.md", "core/README.md", "README.md",
                                      "docs/architecture.md"])
def test_property_count_matches_code(doc: str):
    """三份文档里的「N 个同名 @property」都必须等于代码里的真实计数。

    `README.md`（根）由 Batch 3 加入：它此前也写着一个 @property 数字，但因为不在
    本参数化列表里、且格式不是本守卫认的粗体形态，所以**长期无人核对**
    （曾写 39，实际 46）。加进来 + 统一格式后，同一个断言就把它也钉住了。

    `docs/architecture.md` 由 Batch 19 加入（**GR-1**）：它用的是**完全合规**的粗体
    形态，却因为"门禁只硬编码三份文档"而长期写着 43（真值 46）——这是"守卫范围窄于
    它声称的类"的又一实例。
    """
    actual = _count_properties()
    stated = _stated_counts((_ROOT / doc).read_text(encoding="utf-8"))
    assert stated, f"{doc} 里找不到「N 个同名 @property」的声明——守卫失效"
    for n in stated:
        assert n == actual, (
            f"{doc} 称 {n} 个 @property，实际 {actual} 个——"
            f"新增/删除 property 后请同步文档（这条断言就是为了不用人记）")


def test_property_list_is_complete():
    """`core/README.md` 的**枚举**也要完整——数字对了但列表漏项，读者照样被误导。"""
    tree = ast.parse((_ROOT / "core/agent/agent.py").read_text(encoding="utf-8"))
    props = {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Name) and d.id == "property"
                for d in node.decorator_list)
    }
    text = (_ROOT / "core/README.md").read_text(encoding="utf-8")
    start = text.index("**公开接口**")
    segment = text[start:text.index("。状态写方法", start)]
    missing = sorted(p for p in props if f"`agent.{p}`" not in segment)
    assert not missing, f"core/README.md 的 @property 枚举漏了：{missing}"


def test_suite_duration_claims_are_not_wildly_stale():
    """测试耗时声明必须**同量级**（不写死秒数——那会让守卫本身随机器漂移）。

    `CLAUDE.md` 曾称 full 套件 "~22min"，实测 ~10min；数字一旦离谱，读者就会
    据此做出错误的时间预算。
    """
    text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    minutes = [int(m) for m in re.findall(r"[~≈](\d+)\s*min", text)]
    assert minutes, "CLAUDE.md 里找不到耗时声明"
    assert all(m <= 20 for m in minutes), (
        f"耗时声明 {minutes} 有过期嫌疑（实测 full ≈10min / quick ≈9min）——"
        f"请用测得的真实值更新")
