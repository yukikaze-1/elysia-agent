"""Replanner — 失败修复：诊断 → DAGPatch（局部修复）或 Plan（全量重建）。"""

from __future__ import annotations

import json
import logging

from typing import TYPE_CHECKING

from Module.task_system.utils import format_tools, strip_markdown_fence

from Module.task_system.schema import (
    DAGPatch,
    NodeId,
    NodeState,
    NodeStatus,
    Plan,
    Tool,
)

from . import prompts
from .parser import call_llm, parse_plan_from_dict, parse_patch_from_dict, PlanParseError

if TYPE_CHECKING:
    from Module.task_system.infra import AgentInfra
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class Replanner:
    """失败修复——一次 LLM 调用，产出 DAGPatch（局部修复）或 Plan（全量重建）。

    replan() 流程:
        1. 格式化节点状态（成功/失败/未执行） + 拓扑 + 失败诊断
        2. LLM 调用（json_object 模式）
        3. 解析 JSON → DAGPatch（局部修复）或 Plan（全量重建）
        4. SubAgent 根据返回类型走增量路径或全量重建路径
    """

    def __init__(
        self,
        infra: AgentInfra,
        token_ledger: TokenLedger | None = None,
    ) -> None:
        """初始化 Replanner。

        Args:
            infra: Agent 基础设施，提供 LLM 模型引用。
            token_ledger: Token 账本，用于记录 token 消耗；可选。
        """
        self._infra = infra
        self._token_ledger = token_ledger

    async def replan(
        self,
        original_plan: Plan,
        failed_node_ids: list[NodeId],
        failure_diagnosis: str,
        node_results: dict[str, NodeState],
        intent: str = "",
        tools: list[Tool] | None = None,
        max_nodes: int = 20,
        max_mutations: int = 10,
    ) -> DAGPatch | Plan:
        """Replan——失败诊断 → 增量 DAGPatch 或全量重建 Plan。

        Args:
            original_plan:     当前 DAG（含 nodes + edges）
            failed_node_ids:   失败节点列表（并行执行时可能多个）
            failure_diagnosis: Orchestrator/Evaluator 产出的失败原因
            node_results:      所有节点的 NodeState（含 status/summary/error/evaluation）
            intent:            原始任务意图（用于理解上下文）
            tools:             可用工具列表
            max_nodes:         生效的 DAG 节点数上限
            max_mutations:     生效的敏感操作上限

        Returns:
            DAGPatch — 局部修复
            Plan     — 全量重建

        Raises:
            PlanParseError: LLM 返回的 JSON 无法解析
        """
        if tools is None:
            tools = []

        # ── 格式化节点状态 ──
        successful = self._format_nodes_by_status(
            node_results, original_plan, {NodeStatus.SUCCESS, NodeStatus.SKIPPED},
        )
        failed = self._format_nodes_by_status(
            node_results, original_plan,
            {NodeStatus.FAILED, NodeStatus.UNRECOVERABLE},
        )
        pending = self._format_nodes_by_status(
            node_results, original_plan,
            {NodeStatus.PENDING, NodeStatus.RUNNING},
        )

        # ── 格式化拓扑 ──
        edges_text = "\n".join(
            f"  {from_id} → {to_id}"
            for from_id, to_id in original_plan.dag.edges
        ) if original_plan.dag.edges else "（无依赖关系——所有节点并行）"

        # ── 构建 prompt ──
        tools_desc = format_tools(tools, include_capabilities=False)
        system = prompts.REPLAN_SYSTEM_PROMPT.format(
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            tools=tools_desc,
        )
        user = prompts.REPLAN_USER_PROMPT.format(
            intent=intent or "（未知）",
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            successful_nodes=successful or "（无）",
            failed_nodes=failed or "（无）",
            pending_nodes=pending or "（无）",
            edges=edges_text,
        )
        user += f"\n\n## 失败诊断\n{failure_diagnosis}"

        # ── LLM 调用 ──
        logger.debug("Replanner system prompt:\n%s", system)
        logger.debug("Replanner user prompt:\n%s", user)
        raw = await call_llm(self._infra, self._token_ledger, system, user)
        logger.debug("Replanner LLM 响应:\n%s", raw)
        return self._parse_replan_response(raw)

    # ── 内部：节点状态格式化 ───────────────────────────────────────────────

    @staticmethod
    def _format_nodes_by_status(
        node_results: dict[str, NodeState],
        plan: Plan,
        statuses: set[NodeStatus],
    ) -> str:
        """将指定状态的节点格式化为 prompt 可读文本。

        Args:
            node_results: node_id → NodeState 的映射。
            plan: 当前计划，包含所有 DAG 节点。
            statuses: 要筛选的节点状态集合。

        Returns:
            格式化后的节点状态文本，每行一个节点。
        """
        lines: list[str] = []
        for node in plan.dag.nodes:
            nid_str = str(node.node_id)
            ns = node_results.get(nid_str)
            if ns is None or ns.status not in statuses:
                continue

            parts = [f"- {nid_str}: {node.description}"]
            if ns.summary:
                summary = ns.summary[:200] + "..." if len(ns.summary) > 200 else ns.summary
                parts.append(f"  产物摘要: {summary}")
            if ns.error:
                parts.append(f"  错误: {ns.error}")
            if ns.evaluation is not None:
                ev = ns.evaluation
                parts.append(
                    f"  评估: execution={ev.execution_status.value}, "
                    f"business={ev.business_status.value}, "
                    f"action={ev.suggested_action.value}"
                )

            lines.append("\n".join(parts))

        return "\n".join(lines)

    # ── 内部：replan 响应解析 ─────────────────────────────────────────────

    def _parse_replan_response(self, raw: str) -> DAGPatch | Plan:
        """解析 replan LLM 响应 → DAGPatch 或 Plan。

        Args:
            raw: LLM 原始返回文本。

        Returns:
            DAGPatch（局部修复）或 Plan（全量重建）。

        Raises:
            PlanParseError: JSON 不合法或缺少必要字段。
        """
        text = strip_markdown_fence(raw)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PlanParseError(f"Replan JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise PlanParseError(f"期望 JSON object，收到 {type(data).__name__}")

        full_replan = data.get("full_replan", False)

        if full_replan:
            if "patch_type" in data or "replace_nodes" in data:
                logger.warning(
                    "Replanner: full_replan=true 但响应包含 DAGPatch 字段 "
                    "(patch_type/replace_nodes)——这些字段将被忽略，仅使用 nodes/edges"
                )
        else:
            if "nodes" in data or "edges" in data:
                logger.warning(
                    "Replanner: full_replan=false 但响应包含全量重建字段 "
                    "(nodes/edges)——这些字段将被忽略，仅使用 DAGPatch 字段。"
                    "请检查 Planner prompt 是否充分强调格式互斥。"
                )

        if full_replan:
            return parse_plan_from_dict(data)

        return parse_patch_from_dict(data)
