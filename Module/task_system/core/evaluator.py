"""Evaluator — 双重判定：对 NodeResult 产出 NodeResultEvaluation。

区分代码状态（ExecutionStatus：工具是否正常返回？）和
业务状态（BusinessStatus：Plan 中声明的期望结果是否达成？）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    ExecutionStatus,
    BusinessStatus,
    NodeResult,
    NodeResultEvaluation,
)

if TYPE_CHECKING:
    from Module.task_system.schema import DAGNode, Plan

logger = logging.getLogger(__name__)


class Evaluator:
    """双重判定——ExecutionStatus + BusinessStatus。

    ExecutionStatus（代码层面，5 态）：
        ok / timeout / error / permission_denied / cancelled
        工具是否正常返回？HTTP 200？无异常？

    BusinessStatus（业务层面，3 态）：
        goal_achieved / goal_partial / goal_failed
        Plan 中声明的期望结果是否达成？

    例：搜索返回 "没有找到相关结果" →
        ExecutionStatus="ok"（HTTP 200）、BusinessStatus="goal_failed"（没拿到信息）

    输出 NodeResultEvaluation（包裹原始 NodeResult + 双重判定 + 建议动作）。
    """

    # TODO: __init__ 接收 LLM client（评估可能调 LLM）
    # TODO: output_contract 逐条校验逻辑（对比 json_schema + description）

    async def evaluate(
        self,
        result: NodeResult,
        node: DAGNode,
        plan: Plan,
    ) -> NodeResultEvaluation:
        """评估节点结果——Executor 产出 → Evaluator 双重判定。

        Args:
            result: Executor 返回的节点执行结果（artifact_refs + error + attempt + duration_ms）
                   实际产物从 ArtifactStore 按引用取。
            node:   原始 DAGNode（含 output_contract，用于 business_status 判定）
            plan:   当前 Plan（含上下文，用于判断该节点在整个任务中的位置）

        Returns:
            NodeResultEvaluation（execution_status + business_status + reason + suggested_action + result）

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("Evaluator.evaluate() 尚未实现")
