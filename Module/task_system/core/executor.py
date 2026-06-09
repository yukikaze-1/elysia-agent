"""Executor — DBT 生成 + Worker Agent 调度。

接收 DAGNode 列表 → 为每个节点生成 DBT → 分配 Worker Agent → 收集 NodeResult。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import DAGNode, NodeResult

if TYPE_CHECKING:
    from Module.task_system.schema import TaskState

logger = logging.getLogger(__name__)


class Executor:
    """DAG 节点的执行引擎。

    职责：
        1. 为每个 DAGNode 生成 DBT（优先模板匹配，匹配不上调 LLM）
        2. 拓扑排序 ready nodes → 分配 Worker Agent
        3. 审批风暴检测：执行前扫描所有节点 → 汇总 MUTATION 步骤
        4. 上下文传递（push 模式）：下游节点的 context 包含上游 NodeResult
        5. 收集所有节点结果，返回 NodeResult 列表

    不负责：
        - 找 ready nodes（Orchestrator）
        - 评估结果（Evaluator）
        - 生成 DAG（Planner）
    """

    # TODO: __init__ 接收 LLM client、Worker Agent 池、ToolManager
    # TODO: DBT 模板匹配逻辑（4 种预定义模板）
    # TODO: 审批风暴检测 + 批量审批预览

    async def execute(
        self,
        nodes: list[DAGNode],
        state: TaskState,
    ) -> list[NodeResult]:
        """执行就绪节点。

        Args:
            nodes: 依赖满足、状态为 pending 的节点列表
            state: 当前 TaskState（含已完成节点的 NodeResult，用于上下文传递）

        Returns:
            每个节点的 NodeResult（execution_status + business_status 由 Evaluator 填充）

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("Executor.execute() 尚未实现")
