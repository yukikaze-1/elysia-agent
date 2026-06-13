"""Planner — LLM 调用：DelegationContext → DAG 骨架。

TaskSystem 编排循环的起点。首次规划产出 Plan，Replan 产出 DAGPatch。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from Module.task_system.schema import (
    ArtifactId,
    ConstraintConfig,
    DAG,
    DAGNode,
    DAGPatch,
    DelegationContext,
    ExperienceRecord,
    NodeId,
    NodeResultEvaluation,
    OutputContract,
    Plan,
)

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


class Planner:
    """一次 LLM 调用，生成 DAG 骨架（首次）或增量 Patch（Replan）。

    plan() 流程:
        1. 构建 prompt（含可用工具、约束、输出 schema）
        2. LLM 调用（json_object 模式）
        3. 解析 JSON → Plan
        4. 返回 Plan（校验由 PlanValidator 在 SubAgent 层完成）

    replan() 流程:
        v1 暂不实现——当前只处理正常完成路径。
    """

    def __init__(self) -> None:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY 环境变量未设置，Planner 无法创建 DeepSeek client"
            )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )

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
            global_constraints: 全局约束天花板，与 task_constraints 取 min
            experience_records: v1 忽略（经验记忆未实现）

        Returns:
            Plan（DAG 节点列表 + 依赖关系）

        Raises:
            ValueError: LLM 返回的 JSON 无法解析为合法 Plan
        """
        # ── 计算生效约束 ──
        tc = context.task_constraints
        max_nodes = min(global_constraints.max_dag_nodes, tc.max_dag_nodes)
        max_mutations = min(global_constraints.max_mutations, tc.max_mutations)

        # ── 构建 prompt ──
        tools_desc = self._format_tools(context.tools)
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
        failed_node_id: NodeId,
        failure_diagnosis: str,
        node_results: dict[str, NodeResultEvaluation],
    ) -> DAGPatch:
        """Replan——失败诊断 → 增量 DAG 修改。

        v1 暂不实现。
        """
        raise NotImplementedError("Planner.replan() 尚未实现")

    # ── 内部：LLM 调用 ────────────────────────────────────────────────────────

    async def _call_llm(self, system: str, user: str) -> str:
        """调用 DeepSeek v4，推理模式 + JSON Output。"""
        response = await self._client.chat.completions.create(
            model="deepseek-v4-pro",
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
        text = self._strip_markdown_fence(raw)

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
                input_bindings=[ArtifactId(b) for b in rn.get("input_bindings", []) or []],
                output_as=ArtifactId(rn.get("output_as", "")),
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

        now = datetime.now(timezone.utc).isoformat()
        return Plan(
            dag=DAG(nodes=nodes, edges=edges),
            plan_version=1,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        """去除 LLM 响应中可能的 markdown 代码块包裹。

        处理 ```json ... ``` 和 ``` ... ``` 两种格式，
        容忍首尾空白、尾随空行等情况。
        """
        import re
        text = text.strip()
        # 去掉开头的 ```json 或 ```（含可能的行尾空白）
        text = re.sub(r'^```[a-z]*\s*\n', '', text)
        # 去掉末尾的 ```（前面可能有任意空白行）
        text = re.sub(r'\n```\s*$', '', text)
        return text

    # ── 内部：工具列表格式化 ──────────────────────────────────────────────────

    @staticmethod
    def _format_tools(tools: list) -> str:
        """将 Tool 列表格式化为 LLM prompt 可读的文本。

        Tool.name 和 Tool.description 是必需字段，直接用属性访问。
        如果对象缺少字段，让 AttributeError 抛出——静默降级会掩盖 LLM prompt 中的垃圾文本。
        """
        if not tools:
            return "（无可用工具）"
        lines: list[str] = []
        for t in tools:
            desc = t.description or ""
            lines.append(f"- {t.name}: {desc}" if desc else f"- {t.name}")
        return "\n".join(lines)


# ── 内部异常 ────────────────────────────────────────────────────────────────────

class _PlanParseError(Exception):
    """LLM 输出的 JSON 无法解析为 Plan。"""
