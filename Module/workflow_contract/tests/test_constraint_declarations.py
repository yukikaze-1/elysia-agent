# 测试文件：Module/workflow_contract/tests/test_constraint_declarations.py
"""`ConstraintConfig` 的**声明必须与执行现实一致**（#45，marker=unit）。

背景（RC-9，报告称之为最危险的一类偏离）：`ConstraintConfig` 的文档把一串字段标成
"（硬）"，但其中两个**根本没有执行点**。危害不是"少了个校验"，而是**让读者基于错误
前提做决策**——`DAGNode.max_duration_s` 与 6 个 `WorkflowErrorCode` 就是这样长期
无人发现的（它们后来被 `make check-contract` 登记进 `_PENDING_WRITERS`）。

本文件把"标注 = 现实"钉住：
- `max_context_tokens` 必须显式标成**预留/无执行点**；
- `max_mutations` 必须显式标成**软**（只进 prompt）；
- `max_retries_per_node` 标了硬，就**必须**能在生产代码里找到执行点（反向校验：
  声明"硬"却无执行点 → 本用例红）。
"""

from pathlib import Path

import pytest

from Module.workflow_contract.schema import ConstraintConfig

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]


def _doc() -> str:
    return ConstraintConfig.__doc__ or ""


def test_max_context_tokens_is_declared_as_reserved_not_hard():
    """无执行点的字段不得标"（硬）"——否则读者会以为 prompt 真被截断过。"""
    line = next(ln for ln in _doc().splitlines() if "max_context_tokens" in ln)
    assert "预留" in line or "无执行点" in line, line
    assert "（硬）" not in line, f"仍宣称硬约束：{line}"


def test_max_mutations_is_declared_soft():
    """`max_mutations` 只进 prompt（planner/delegation），执行期不强制 → 必须标"软"。"""
    line = next(ln for ln in _doc().splitlines() if "max_mutations" in ln)
    assert "软" in line, line
    assert "（硬）" not in line, f"仍宣称硬约束：{line}"


def test_max_retries_per_node_hard_claim_has_an_enforcement_site():
    """标"硬"就必须找得到执行点——声明与现实的**双向**校验。"""
    line = next(ln for ln in _doc().splitlines() if "max_retries_per_node" in ln)
    assert "硬" in line, line
    execution = (_ROOT / "Module/workflow_engine/core/execution.py").read_text(encoding="utf-8")
    assert "constraints.max_retries_per_node" in execution, \
        "声明为硬约束，但 execution.py 里找不到执行点（声明又跑到了实现前面）"


def test_reserved_field_is_registered_in_the_contract_gate():
    """预留字段必须留在门禁登记里（否则"忘了它不是真约束"下次还会重演）。"""
    gate = (_ROOT / "scripts/check_contract_reachability.py").read_text(encoding="utf-8")
    assert "ConstraintConfig.max_context_tokens" in gate
    assert "预留" in gate
