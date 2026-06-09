"""Evaluator — 双重判定：检查节点结果是否达成期望目标。

区分代码状态（工具是否正常返回？）和业务状态（Plan 中声明的期望结果是否达成？）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import NodeResult

if TYPE_CHECKING:
    from Module.task_system.schema import DAGNode, Plan

logger = logging.getLogger(__name__)


class Evaluator:
    """双重判定——ExecutionStatus + BusinessStatus。

    ExecutionStatus（代码层面）：
        ok / timeout / error / permission_denied
        工具是否正常返回？HTTP 200？无异常？

    BusinessStatus（业务层面）：
        goal_achieved / goal_partial / goal_failed
        Plan 中声明的期望结果是否达成？

    例：搜索返回 "没有找到相关结果" →
        ExecutionStatus="ok"（HTTP 200）、BusinessStatus="goal_failed"（没拿到信息）

    输出 action：
        continue / retry / replan / ask_user / abort
    """

    # TODO: __init__ 接收 LLM client（评估可能调 LLM）
    # TODO: output_contract 逐条校验逻辑

    async def evaluate(
        self,
        result: NodeResult,
        node: DAGNode,
        plan: Plan,
    ) -> dict:
        """评估节点结果。

        Args:
            result: Executor 返回的节点执行结果
            node:   原始 DAGNode（含 expected_outcome 和 output_contract）
            plan:   当前 Plan（含上下文，用于判断该节点在整个任务中的位置）

        Returns:
            {
                "action": "continue" | "retry" | "replan" | "ask_user" | "abort",
                "reason": str,
                "issues": list[dict],
                "suggested_fix": dict | None,   # retry 时的修复建议
            }

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("Evaluator.evaluate() 尚未实现")
