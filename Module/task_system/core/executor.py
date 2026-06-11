"""Executor — v1 单工具直接调用 + Worker Agent 调度。

接收 DAGNode 列表 → 匹配工具 → 分配 Worker Agent → 收集 NodeResult。
v1 不做 DBT 生成——每个 DAGNode 对应一次工具调用。
NodeResult 仅含 ArtifactStore 引用 + 错误码 + 耗时，不含业务判定。
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

    v1 不做 DBT 生成——每个 DAGNode 直接匹配工具并调用。
    v2 考虑 DBT：Sequence/Selector/Parallel/Action/Condition 行为树。

    职责：
        1. 为每个 DAGNode 匹配工具（优先模板匹配，匹配不上调 LLM）
        2. 就绪节点 → 分配 Worker Agent
        3. 审批风暴检测：执行前扫描所有节点 → 汇总 MUTATION 步骤
        4. 上下文传递（push 模式）：下游节点的 context 含上游产物引用
        5. 收集所有节点结果，返回 NodeResult 列表

    不负责：
        - 找 ready nodes（Orchestrator）
        - 评估结果（Evaluator —— 产出 NodeResultEvaluation）
        - 生成 DAG / Replan（Planner）
    """

    # TODO: __init__ 接收 LLM client、Worker Agent 池、ToolManager、可用工具列表
    # TODO: 工具匹配逻辑（模板匹配 → LLM fallback）
    # TODO: 审批风暴检测 + 批量审批预览

    async def execute(
        self,
        nodes: list[DAGNode],
        state: TaskState,
    ) -> list[NodeResult]:
        """执行就绪节点——返回 Executor 层纯结果。

        Evaluator 后续包裹 NodeResultEvaluation 做双重判定。
        所有产物统一存 ArtifactStore，NodeResult 只持有引用。

        Args:
            nodes: 依赖满足、状态为 pending 的节点列表
            state:  当前 TaskState（含已完成节点的 NodeResultEvaluation，用于上下文传递）

        Returns:
            每个节点的 NodeResult（artifact_refs + error + attempt + duration_ms）

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("Executor.execute() 尚未实现")
