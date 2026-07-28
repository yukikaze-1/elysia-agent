"""Planner — 首次规划：DelegationContext → DAG 骨架。"""

from __future__ import annotations

import json
import logging

from typing import TYPE_CHECKING

from Module.task_system.utils import format_tools, strip_markdown_fence

from Module.task_system.schema import (
    ConstraintConfig,
    DelegationContext,
    ExperienceRecord,
    Plan,
)

from . import prompts
from .parser import call_llm, parse_plan_from_dict, PlanParseError

if TYPE_CHECKING:
    from Module.task_system.infra import AgentInfra
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class Planner:
    """首次规划——一次 LLM 调用，DelegationContext → DAG 骨架。

    plan() 流程:
        1. 构建 prompt（含可用工具、约束、输出 schema）
        2. LLM 调用（json_object 模式）
        3. 解析 JSON → Plan
        4. 返回 Plan（校验由 PlanValidator 在 SubAgent 层完成）
    """

    def __init__(
        self,
        infra: AgentInfra,
        token_ledger: TokenLedger | None = None,
    ) -> None:
        """初始化 Planner。

        Args:
            infra: Agent 基础设施，提供 LLM 模型引用。
            token_ledger: Token 账本，用于记录 token 消耗；可选。
        """
        self._infra = infra
        self._token_ledger = token_ledger

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
            PlanParseError: LLM 返回的 JSON 无法解析为合法 Plan
        """
        # ── 计算生效约束 ──
        tc = context.task_constraints.resolve(global_constraints)
        max_nodes = tc.max_dag_nodes
        max_mutations = tc.max_mutations

        # ── 构建 prompt ──
        tools_desc = format_tools(context.tools, include_capabilities=False)
        hints = context.hints or "（无）"
        ctx_text = context.summarized_context or "（无）"

        system = prompts.SYSTEM_PROMPT.format(
            tools=tools_desc,
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            output_schema=prompts.OUTPUT_SCHEMA_DESCRIPTION,
        )
        user = prompts.USER_PROMPT.format(
            intent=context.intent,
            hints=hints,
            context=ctx_text,
        )

        # ── LLM 调用 ──
        logger.debug("Planner system prompt:\n%s", system)
        logger.debug("Planner user prompt:\n%s", user)
        raw = await call_llm(self._infra, self._token_ledger, system, user)
        logger.debug("Planner LLM 响应:\n%s", raw)
        plan = self._parse_response(raw)
        return plan

    def _parse_response(self, raw: str) -> Plan:
        """解析 LLM 返回的 JSON → Plan。

        Args:
            raw: LLM 原始返回文本。

        Returns:
            解析后的 Plan 对象。

        Raises:
            PlanParseError: JSON 不合法或缺少必要字段。
        """
        text = strip_markdown_fence(raw)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PlanParseError(f"JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise PlanParseError(f"期望 JSON object，收到 {type(data).__name__}")

        return parse_plan_from_dict(data)
