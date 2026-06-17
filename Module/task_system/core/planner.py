"""Planner — LLM 调用：DelegationContext → DAG 骨架。

TaskSystem 编排循环的起点。首次规划产出 Plan，Replan 产出 DAGPatch 或 Plan（全量重建）。
"""

from __future__ import annotations

import json
import logging

from typing import TYPE_CHECKING

from Module.task_system.utils import format_tools, strip_markdown_fence

from Module.task_system.schema import (
    ArtifactId,
    ConstraintConfig,
    DAG,
    DAGNode,
    DAGPatch,
    DAGPatchType,
    DelegationContext,
    ExecutionMode,
    ExperienceRecord,
    NodeId,
    NodeState,
    NodeStatus,
    OutputContract,
    Plan,
    TokenComponentType,
    Tool,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.infra import AgentInfra
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)

# ── LLM 输出 JSON Schema（嵌入 prompt 中，不直接用 response_format json_schema）──
# 用 json_object 模式 + prompt 描述，兼容 DeepSeek / OpenAI / Ollama

_OUTPUT_SCHEMA_DESCRIPTION = """{
  "nodes": [
    {
      "node_id": "string (唯一标识，如 search_weather)",
      "description": "string (这一步做什么，给 Executor 看的自然语言任务描述)",
      "output_contract": {
        "description": "string (期望产出什么，给 Evaluator 做业务判定用)"
      },
      "required": true,
      "execution_mode": "tool_call 或 llm_direct（按上述规则）",
      "input_bindings": ["output:search_weather"],
      "output_as": "output:send_email"
    }
  ],
  "edges": [["from_node_id", "to_node_id"]]
}"""

# ── System Prompt 模板 ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是任务规划器。根据用户意图、可用工具和约束，将任务拆解为有向无环图（DAG）。

## DAG 构建规则
1. 每个节点有唯一的 node_id（短名，如 "search_weather"、"send_email"）
2. edges 定义执行顺序：(from, to) 表示 from 完成后 to 才能开始
3. input_bindings 引用上游节点的 output_as，声明数据依赖
4. output_as 用 "output:<node_id>" 格式
5. 可选步骤（失败后不影响整体）设 required: false
6. output_contract.description 是自然语言期望——Evaluator 用它判断步骤是否成功
7. **多节点任务必须有边**——存在 >1 个节点时，edges 不能为空。
   每个节点要么依赖上游节点，要么被下游节点依赖。
   所有独立并行节点必须汇聚到一个最终汇总/报告/输出节点。
8. **始终包含一个最终输出节点**（如 compile_report、compose_answer）——
   它汇总上游所有节点的结果，生成用户可读的最终产物。

## execution_mode 字段
每个节点必须标注 execution_mode：
- **tool_call**（默认）: 节点需要调用外部工具（搜索、读文件、发邮件等）。Executor 会匹配工具并执行。
- **llm_direct**: 节点不需要外部工具。Executor 直接调 LLM 处理上游数据。
  **仅在以下情况使用 llm_direct**：
  · 汇总/编译/总结上游搜索结果
  · 改写/润色/翻译已有文本
  · 综合多个上游产物生成报告
  · 注意：llm_direct 节点仍可引用 input_bindings 读取上游数据

**关键规则**：
- 搜索/查询/调用工具 → tool_call（即使是"搜索 X"也标注 tool_call）
- 汇总/编写/整理报告 → llm_direct（禁止为这类节点标注 tool_call）
- 最终输出节点（compile_report、compose_answer、summarize 等）→ 一律 llm_direct

## 可用工具
{tools}

## 约束
- DAG 节点数上限: {max_nodes}
- 敏感操作上限: {max_mutations}

## 输出格式
严格返回以下 JSON（只返回 JSON，不要 markdown 代码块）：
{output_schema}"""

_USER_PROMPT = """## 任务意图
{intent}

## 提示
{hints}

## 上下文摘要
{context}"""

# ── Replan System Prompt ────────────────────────────────────────────────────
# 独立于 plan() 的 prompt——角色从"规划器"切换为"修复器"，聚焦失败诊断。

_REPLAN_SYSTEM_PROMPT = """你是任务修复器。一个 DAG 执行过程中有节点失败了。根据失败诊断和已成功节点的产物，决定修复策略。

## 可用策略
1. **局部修复**（推荐，大多数情况适用）：
   - replace_nodes: 替换失败节点——改描述/改工具/拆分任务
   - add_nodes: 在失败节点之前插入缺失的步骤（如先获取时间再搜索）
   - remove_nodes: 移除不必要的可选节点
   - 配合 add_edges / remove_edges 调整拓扑
2. **全量重建**（仅当 DAG 结构根本有问题——如规划逻辑错误、依赖关系矛盾）

## 修复原则
- 已成功的节点**不要动**——它们的结果会被保留复用
- 优先用最小的修改解决问题，避免不必要的全量重建
- 如果失败原因是工具选错，换工具；如果是参数问题，改 description 让 Executor 理解更精确
- 如果原因是"工具正常返回但业务目标未达成"（goal_failed），考虑拆分为多个更简单步骤
- 如果原因是"工具本身报错/超时/找不到"，尝试替代方案
- 新节点数不要超过 {max_nodes}（含已成功的节点）
- 敏感操作不要超过 {max_mutations}
- **汇总/编译/整理/报告节点必须标注 execution_mode: "llm_direct"**——见下方 execution_mode 规则

## execution_mode 字段
新增或替换节点时必须标注 execution_mode：
- **tool_call**: 节点需要调用外部工具（搜索、读文件、发邮件等）
- **llm_direct**: 节点不需要外部工具，Executor 直接调 LLM 处理上游数据。
  **仅在以下情况使用 llm_direct**：汇总/编译/总结上游结果、改写/润色/翻译文本、
  综合多个上游产物生成报告、最终输出节点（compile_report、compose_answer、summarize 等）
- 搜索/查询/调用工具 → tool_call；汇总/编写/整理报告 → llm_direct

## 可用工具
{tools}

## 输出格式

**CRITICAL**: `full_replan` 是互斥的格式选择器，两种格式绝不能混用字段。

- `full_replan: false` → 只输出 DAGPatch 字段（patch_type, trigger_nodes, replace_nodes, add_nodes, remove_nodes, add_edges, remove_edges, reason）。**禁止**输出 nodes/edges。
- `full_replan: true` → 只输出 nodes + edges（全量重建 DAG）。**禁止**输出 patch_type/replace_nodes/add_nodes/remove_nodes/add_edges/remove_edges。

### 局部修复:
{{
  "full_replan": false,
  "patch_type": "replace_nodes | add_nodes | remove_nodes",
  "reason": "修复理由",
  "trigger_nodes": ["failed_node_id"],
  "replace_nodes": [["old_node_id", {{"node_id": "new_id", "description": "...", "output_contract": {{"description": "..."}}, "required": true, "execution_mode": "tool_call 或 llm_direct（按上述规则）", "input_bindings": ["output:upstream"], "output_as": "output:new_id"}}]],
  "add_nodes": [{{"node_id": "...", "description": "...", "execution_mode": "tool_call 或 llm_direct（按上述规则）", ...}}],
  "remove_nodes": ["node_id_to_remove"],
  "remove_edges": [["from_id", "to_id"]],
  "add_edges": [["from_id", "to_id"]]
}}

### 全量重建:
{{
  "full_replan": true,
  "nodes": [
    {{
      "node_id": "string",
      "description": "string",
      "output_contract": {{"description": "string"}},
      "required": true,
      "execution_mode": "tool_call 或 llm_direct（按上述规则）",
      "input_bindings": ["output:upstream"],
      "output_as": "output:this_node"
    }}
  ],
  "edges": [["from_id", "to_id"]]
}}

严格返回以上两种 JSON 之一（只返回 JSON，不要 markdown 代码块）。"""

_REPLAN_USER_PROMPT = """## 任务意图
{intent}

## 约束
- DAG 节点数上限（含已完成节点）: {max_nodes}
- 敏感操作上限: {max_mutations}

## 已成功的节点
{successful_nodes}

## 失败的节点
{failed_nodes}

## 尚未执行的节点
{pending_nodes}

## 原 DAG 拓扑 (edges)
{edges}"""


class Planner:
    """一次 LLM 调用，生成 DAG 骨架（首次）或增量修复/全量重建（Replan）。

    plan() 流程:
        1. 构建 prompt（含可用工具、约束、输出 schema）
        2. LLM 调用（json_object 模式）
        3. 解析 JSON → Plan
        4. 返回 Plan（校验由 PlanValidator 在 SubAgent 层完成）

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
        self._infra = infra
        self._token_ledger = token_ledger

    # ── 首次规划 ──────────────────────────────────────────────────────────────

    async def plan(
        self,
        context: DelegationContext,
        global_constraints: ConstraintConfig,
        experience_records: list[ExperienceRecord] | None = None,
    ) -> Plan:
        """首次规划——DelegationContext → DAG 骨架。

        Args:
            context:            委派上下文（intent/hints/tools/task_constraints/…）
            global_constraints: 全局约束天花板，TaskConstraints.resolve() 的填充来源
            experience_records: v1 忽略（经验记忆未实现）

        Returns:
            Plan（DAG 节点列表 + 依赖关系）

        Raises:
            ValueError: LLM 返回的 JSON 无法解析为合法 Plan
        """
        # ── 计算生效约束 ──
        # resolve: None → 全局值；> 全局 → clamp + warn
        tc = context.task_constraints.resolve(global_constraints)
        max_nodes = tc.max_dag_nodes
        max_mutations = tc.max_mutations

        # ── 构建 prompt ──
        tools_desc = format_tools(context.tools, include_capabilities=False)
        hints = context.hints or "（无）"
        ctx_text = context.summarized_context or "（无）"

        system = _SYSTEM_PROMPT.format(
            tools=tools_desc,
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            output_schema=_OUTPUT_SCHEMA_DESCRIPTION,
        )
        user = _USER_PROMPT.format(
            intent=context.intent,
            hints=hints,
            context=ctx_text,
        )

        # ── LLM 调用 ──
        logger.debug("Planner system prompt:\n%s", system)
        logger.debug("Planner user prompt:\n%s", user)
        raw = await self._call_llm(system, user)
        logger.debug("Planner LLM 响应:\n%s", raw)
        plan = self._parse_response(raw)
        return plan

    # ── Replan ─────────────────────────────────────────────────────────────────

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

        LLM 根据失败诊断和已执行节点的产物，决定局部修复还是全量重建。
        返回 DAGPatch 时走增量路径（保留已成功节点状态），
        返回 Plan 时走全量重建路径（不复用旧状态）。

        Args:
            original_plan:     当前 DAG（含 nodes + edges）
            failed_node_ids:   失败节点列表（并行执行时可能多个）
            failure_diagnosis: Orchestrator/Evaluator 产出的失败原因
            node_results:      所有节点的 NodeState（含 status/summary/error/evaluation）
            intent:            原始任务意图（用于理解上下文）
            tools:             可用工具列表
            max_nodes:         生效的 DAG 节点数上限（来自 TaskConstraints.resolve()）
            max_mutations:     生效的敏感操作上限

        Returns:
            DAGPatch — 局部修复
            Plan     — 全量重建

        Raises:
            _PlanParseError: LLM 返回的 JSON 无法解析
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

        # ── 约束（由调用方从 TaskConstraints.resolve(global_constraints) 计算后传入）
        # 与 plan() 保持一致——使用相同的解析后约束值

        # ── 构建 prompt ──
        tools_desc = format_tools(tools, include_capabilities=False)
        system = _REPLAN_SYSTEM_PROMPT.format(
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            tools=tools_desc,
        )
        user = _REPLAN_USER_PROMPT.format(
            intent=intent or "（未知）",
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            successful_nodes=successful or "（无）",
            failed_nodes=failed or "（无）",
            pending_nodes=pending or "（无）",
            edges=edges_text,
        )
        # 失败诊断追加到 user prompt 末尾——LLM 需要知道具体出了什么问题
        user += f"\n\n## 失败诊断\n{failure_diagnosis}"

        # ── LLM 调用 ──
        logger.debug("Planner replan system prompt:\n%s", system)
        logger.debug("Planner replan user prompt:\n%s", user)
        raw = await self._call_llm(system, user)
        logger.debug("Planner replan LLM 响应:\n%s", raw)
        return self._parse_replan_response(raw)

    # ── 内部：节点状态格式化（replan 用）───────────────────────────────────

    @staticmethod
    def _format_nodes_by_status(
        node_results: dict[str, NodeState],
        plan: Plan,
        statuses: set[NodeStatus],
    ) -> str:
        """将指定状态的节点格式化为 prompt 可读文本。

        从 plan.dag.nodes 获取 description，从 node_results 获取运行时状态。
        """
        lines: list[str] = []
        for node in plan.dag.nodes:
            nid_str = str(node.node_id)
            ns = node_results.get(nid_str)
            if ns is None or ns.status not in statuses:
                continue

            parts = [f"- {nid_str}: {node.description}"]
            if ns.summary:
                # 截断过长摘要，避免 prompt 膨胀
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

        先判断 full_replan 标志：
            true  → 解析为 Plan（复用 _parse_response 的 nodes/edges 逻辑）
            false → 解析为 DAGPatch

        Raises:
            _PlanParseError: JSON 不合法或缺少必要字段
        """
        text = strip_markdown_fence(raw)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise _PlanParseError(f"Replan JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise _PlanParseError(f"期望 JSON object，收到 {type(data).__name__}")

        full_replan = data.get("full_replan", False)

        # 防御性检查：LLM 可能混用两种格式（如 full_replan=false 却带了 nodes）
        if full_replan:
            if "patch_type" in data or "replace_nodes" in data:
                logger.warning(
                    "Planner replan: full_replan=true 但响应包含 DAGPatch 字段 "
                    "(patch_type/replace_nodes)——这些字段将被忽略，仅使用 nodes/edges"
                )
        else:
            if "nodes" in data or "edges" in data:
                logger.warning(
                    "Planner replan: full_replan=false 但响应包含全量重建字段 "
                    "(nodes/edges)——这些字段将被忽略，仅使用 DAGPatch 字段。"
                    "请检查 Planner prompt 是否充分强调格式互斥。"
                )

        if full_replan:
            # 全量重建——复用 _parse_response（内部从 data["nodes"] + data["edges"] 解析）
            return self._parse_plan_from_dict(data)

        # 局部修复——解析 DAGPatch
        return self._parse_patch_from_dict(data)

    def _parse_plan_from_dict(self, data: dict) -> Plan:
        """从 replan 响应 dict 解析 Plan（全量重建路径）。

        与 _parse_response 逻辑相同，但跳过 strip_markdown_fence（已在调用方处理）。
        """
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise _PlanParseError("全量重建: nodes 为空或不是数组")

        nodes: list[DAGNode] = []
        for i, rn in enumerate(raw_nodes):
            if not isinstance(rn, dict):
                raise _PlanParseError(f"全量重建: nodes[{i}] 不是 object")
            nid = rn.get("node_id", "")
            if not nid or not isinstance(nid, str):
                raise _PlanParseError(
                    f"全量重建: nodes[{i}].node_id 为空或不是字符串: {nid!r}"
                )

            oc_data = rn.get("output_contract", {}) or {}
            nodes.append(DAGNode(
                node_id=NodeId(nid),
                description=rn.get("description", ""),
                output_contract=OutputContract(
                    description=oc_data.get("description", ""),
                ),
                required=rn.get("required", True),
                execution_mode=self._parse_execution_mode(rn.get("execution_mode")),
                input_bindings=[ArtifactId(b) for b in rn.get("input_bindings", []) or []],
                output_as=ArtifactId(str(raw_as)) if (raw_as := (rn.get("output_as") or "").strip()) else None,
            ))

        raw_edges = data.get("edges", [])
        if not isinstance(raw_edges, list):
            raise _PlanParseError("全量重建: edges 不是数组")

        edges: list[tuple[NodeId, NodeId]] = []
        for i, e in enumerate(raw_edges):
            if not isinstance(e, list) or len(e) != 2:
                raise _PlanParseError(f"全量重建: edges[{i}] 不是二元组 [from, to]")
            edges.append((NodeId(str(e[0])), NodeId(str(e[1]))))

        now = now_iso()
        return Plan(
            dag=DAG(nodes=nodes, edges=edges),
            plan_version=0,  # sentinel——由 SubAgent 从旧 plan 继承并递增
            created_at=now,
            updated_at=now,
        )

    def _parse_patch_from_dict(self, data: dict) -> DAGPatch:
        """从 replan 响应 dict 解析 DAGPatch（局部修复路径）。"""
        patch_type_str = data.get("patch_type", "replace_nodes")
        try:
            patch_type = DAGPatchType(patch_type_str)
        except ValueError:
            raise _PlanParseError(
                f"未知 patch_type: {patch_type_str!r}，"
                f"有效值: {[p.value for p in DAGPatchType]}"
            )

        # 解析 trigger_nodes
        trigger_nodes_raw = data.get("trigger_nodes", [])
        if not isinstance(trigger_nodes_raw, list):
            raise _PlanParseError("DAGPatch: trigger_nodes 不是数组")
        trigger_nodes = [NodeId(str(n)) for n in trigger_nodes_raw]

        # 解析 replace_nodes: [["old_id", {new_node_dict}], ...]
        replace_nodes_raw = data.get("replace_nodes", [])
        if not isinstance(replace_nodes_raw, list):
            raise _PlanParseError("DAGPatch: replace_nodes 不是数组")
        replace_nodes: list[tuple[NodeId, DAGNode]] = []
        for i, item in enumerate(replace_nodes_raw):
            if not isinstance(item, list) or len(item) != 2:
                raise _PlanParseError(
                    f"DAGPatch: replace_nodes[{i}] 不是二元组 [old_id, new_node]"
                )
            old_id = NodeId(str(item[0]))
            new_node_dict = item[1]
            if not isinstance(new_node_dict, dict):
                raise _PlanParseError(
                    f"DAGPatch: replace_nodes[{i}][1] 不是 node object"
                )
            nid = new_node_dict.get("node_id", "")
            if not nid or not isinstance(nid, str):
                raise _PlanParseError(
                    f"DAGPatch: replace_nodes[{i}][1].node_id 为空或不是字符串"
                )
            oc_data = new_node_dict.get("output_contract", {}) or {}
            new_node = DAGNode(
                node_id=NodeId(nid),
                description=new_node_dict.get("description", ""),
                output_contract=OutputContract(
                    description=oc_data.get("description", "") if isinstance(oc_data, dict) else "",
                ),
                required=new_node_dict.get("required", True),
                input_bindings=[
                    ArtifactId(b) for b in new_node_dict.get("input_bindings", []) or []
                ],
                output_as=ArtifactId(str(raw_as))
                if (raw_as := (new_node_dict.get("output_as") or "").strip())
                else None,
            )
            replace_nodes.append((old_id, new_node))

        # 解析 add_nodes
        add_nodes_raw = data.get("add_nodes", [])
        if not isinstance(add_nodes_raw, list):
            raise _PlanParseError("DAGPatch: add_nodes 不是数组")
        add_nodes: list[DAGNode] = []
        for i, rn in enumerate(add_nodes_raw):
            if not isinstance(rn, dict):
                raise _PlanParseError(f"DAGPatch: add_nodes[{i}] 不是 object")
            nid = rn.get("node_id", "")
            if not nid or not isinstance(nid, str):
                raise _PlanParseError(
                    f"DAGPatch: add_nodes[{i}].node_id 为空或不是字符串"
                )
            oc_data = rn.get("output_contract", {}) or {}
            add_nodes.append(DAGNode(
                node_id=NodeId(nid),
                description=rn.get("description", ""),
                output_contract=OutputContract(
                    description=oc_data.get("description", "") if isinstance(oc_data, dict) else "",
                ),
                required=rn.get("required", True),
                input_bindings=[ArtifactId(b) for b in rn.get("input_bindings", []) or []],
                output_as=ArtifactId(str(raw_as))
                if (raw_as := (rn.get("output_as") or "").strip())
                else None,
            ))

        # 解析 remove_nodes
        remove_nodes_raw = data.get("remove_nodes", [])
        if not isinstance(remove_nodes_raw, list):
            raise _PlanParseError("DAGPatch: remove_nodes 不是数组")
        remove_nodes = [NodeId(str(n)) for n in remove_nodes_raw]

        # 解析 remove_edges
        remove_edges_raw = data.get("remove_edges", [])
        if not isinstance(remove_edges_raw, list):
            raise _PlanParseError("DAGPatch: remove_edges 不是数组")
        remove_edges: list[tuple[NodeId, NodeId]] = []
        for i, e in enumerate(remove_edges_raw):
            if not isinstance(e, list) or len(e) != 2:
                raise _PlanParseError(f"DAGPatch: remove_edges[{i}] 不是二元组 [from, to]")
            remove_edges.append((NodeId(str(e[0])), NodeId(str(e[1]))))

        # 解析 add_edges
        add_edges_raw = data.get("add_edges", [])
        if not isinstance(add_edges_raw, list):
            raise _PlanParseError("DAGPatch: add_edges 不是数组")
        add_edges: list[tuple[NodeId, NodeId]] = []
        for i, e in enumerate(add_edges_raw):
            if not isinstance(e, list) or len(e) != 2:
                raise _PlanParseError(f"DAGPatch: add_edges[{i}] 不是二元组 [from, to]")
            add_edges.append((NodeId(str(e[0])), NodeId(str(e[1]))))

        return DAGPatch(
            patch_type=patch_type,
            reason=data.get("reason", ""),
            trigger_nodes=trigger_nodes,
            created_at=now_iso(),
            replace_nodes=replace_nodes,
            remove_nodes=remove_nodes,
            add_nodes=add_nodes,
            remove_edges=remove_edges,
            add_edges=add_edges,
        )

    # ── 内部：LLM 调用 ────────────────────────────────────────────────────────

    async def _call_llm(self, system: str, user: str) -> str:
        """调用 LLM，推理模式 + JSON Output。"""
        model = self._infra.llm_model
        response = await self._infra.llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            # reasoning_effort="max",
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
            max_tokens=8192,
        )
        # ── token 追踪 ──
        if self._token_ledger and (usage := response.usage):
            try:
                self._token_ledger.record(
                    component=TokenComponentType.PLANNER,
                    prompt_tokens=usage.prompt_tokens or 0,
                    completion_tokens=usage.completion_tokens or 0,
                    model=model,
                )
            except Exception:
                logger.warning("TokenLedger.record 失败，跳过", exc_info=True)
        content = response.choices[0].message.content
        if not content:
            raise _PlanParseError("LLM 返回空响应")
        return content

    # ── 内部：解析 ────────────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> Plan:
        """解析 LLM 返回的 JSON → Plan。

        Raises:
            _PlanParseError: JSON 不合法或缺少必要字段
        """
        text = strip_markdown_fence(raw)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise _PlanParseError(f"JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise _PlanParseError(f"期望 JSON object，收到 {type(data).__name__}")

        # 解析 nodes
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise _PlanParseError("nodes 为空或不是数组")

        nodes: list[DAGNode] = []
        for i, rn in enumerate(raw_nodes):
            if not isinstance(rn, dict):
                raise _PlanParseError(f"nodes[{i}] 不是 object")
            nid = rn.get("node_id", "")
            if not nid or not isinstance(nid, str):
                raise _PlanParseError(
                    f"nodes[{i}].node_id 为空或不是字符串: {nid!r}"
                )

            oc_data = rn.get("output_contract", {}) or {}
            nodes.append(DAGNode(
                node_id=NodeId(nid),
                description=rn.get("description", ""),
                output_contract=OutputContract(
                    description=oc_data.get("description", ""),
                ),
                required=rn.get("required", True),
                execution_mode=self._parse_execution_mode(rn.get("execution_mode")),
                input_bindings=[ArtifactId(b) for b in rn.get("input_bindings", []) or []],
                output_as=ArtifactId(str(raw_as)) if (raw_as := (rn.get("output_as") or "").strip()) else None,
            ))

        # 解析 edges
        raw_edges = data.get("edges", [])
        if not isinstance(raw_edges, list):
            raise _PlanParseError("edges 不是数组")

        edges: list[tuple[NodeId, NodeId]] = []
        for i, e in enumerate(raw_edges):
            if not isinstance(e, list) or len(e) != 2:
                raise _PlanParseError(f"edges[{i}] 不是二元组 [from, to]")
            edges.append((NodeId(str(e[0])), NodeId(str(e[1]))))

        now = now_iso()
        return Plan(
            dag=DAG(nodes=nodes, edges=edges),
            plan_version=1,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _parse_execution_mode(raw) -> ExecutionMode:
        """解析 execution_mode 字段，容错 LLM 输出。"""
        if raw is None:
            return ExecutionMode.TOOL_CALL
        if isinstance(raw, ExecutionMode):
            return raw
        try:
            return ExecutionMode(str(raw))
        except ValueError:
            logger.warning("Planner: 未知 execution_mode=%r，fallback tool_call", raw)
            return ExecutionMode.TOOL_CALL



# ── 内部异常 ────────────────────────────────────────────────────────────────────

class _PlanParseError(Exception):
    """LLM 输出的 JSON 无法解析为 Plan。"""
