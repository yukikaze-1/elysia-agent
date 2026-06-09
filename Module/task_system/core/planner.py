"""Planner — LLM 调用：委派上下文 → DAG 骨架。

TaskSystem 编排循环的起点。首次规划产出 Plan，Replan 产出 DAGPatch。
"""

from __future__ import annotations

import logging

from Module.task_system.schema import (
    ConstraintConfig,
    DAGPatch,
    DelegationContext,
    ExperienceRecord,
    NodeId,
    NodeResult,
    Plan,
)

logger = logging.getLogger(__name__)


class Planner:
    """一次 LLM 调用，生成 DAG 骨架（首次）或增量 Patch（Replan）。

    输入 DelegationContext（DelegationPreparer 已筛选工具、约束、上下文摘要），
    产出结构化的执行计划。不填 DBT——那是 Executor 的事。

    两条路径：
        plan()   —— 首次规划，产出完整 Plan
        replan() —— 失败后修复，产出增量 DAGPatch（不动已完成节点）
    """

    # TODO: __init__ 接收 LLM client + planner prompt template
    # TODO: 经验记忆查询（Milvus）由调用方注入 experience_records，
    #       Planner 本身不访问数据库

    async def plan(
        self,
        context: DelegationContext,
        global_constraints: ConstraintConfig,
        experience_records: list[ExperienceRecord] | None = None,
    ) -> Plan:
        """首次规划——DelegationContext → DAG 骨架。

        Args:
            context:            委派上下文（intent/hints/capabilities/task_constraints/…）
            global_constraints: 全局约束天花板，与 task_constraints 取 min
            experience_records: 已知失败模式（可为 None / 空列表，Milvus 不可用时跳过）

        Returns:
            Plan（DAG 节点列表 + 依赖关系 + 期望结果）
            Planner 不负责校验 DAG 合法性——PlanValidator 来做。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("Planner.plan() 尚未实现")

    async def replan(
        self,
        original_plan: Plan,
        failed_node_id: NodeId,
        failure_diagnosis: str,
        node_results: dict[str, NodeResult],
    ) -> DAGPatch:
        """Replan——失败诊断 → 增量 DAG 修改。

        只修改失败/未执行的部分，已完成节点不动。
        full_replan 是兜底——补丁式修改无法覆盖时退回全量重规划。

        Args:
            original_plan:      当前生效的 Plan（含已完成节点的结果）
            failed_node_id:     哪个节点失败了
            failure_diagnosis:  Evaluator 的诊断（为什么失败、建议如何修复）
            node_results:       所有已完成节点的结果

        Returns:
            DAGPatch——增量修改。调用方（Orchestrator）负责 apply。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("Planner.replan() 尚未实现")
