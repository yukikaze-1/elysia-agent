"""Evaluator — 双重判定：对 NodeResult 产出 NodeResultEvaluation。

区分代码状态（ExecutionStatus：工具是否正常返回？）和
业务状态（BusinessStatus：Plan 中声明的期望结果是否达成？）。

v1: ExecutionStatus 确定性映射（NodeErrorCode → ExecutionStatus），
    BusinessStatus LLM 判定（对比 artifact 内容 vs output_contract），
    suggested_action 规则推导。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from Module.task_system.schema import (
    BusinessStatus,
    DAGNode,
    EvaluatorAction,
    ExecutionStatus,
    NodeErrorCode,
    NodeResult,
    NodeResultEvaluation,
)

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.infra import AgentInfra
    from Module.task_system.schema import Plan

logger = logging.getLogger(__name__)

# LLM 业务判定的系统提示词
_BUSINESS_JUDGE_SYSTEM = """你是一个任务执行结果的评估专家。你的工作是判断工具执行的实际产出是否达到了计划中声明的期望。

判定标准：
- goal_achieved: 实际产出完全满足 output_contract 中描述的所有要求
- goal_partial: 实际产出部分满足了要求，但缺少一些关键信息或质量不够
- goal_failed: 实际产出完全没有满足要求，或者工具返回了无意义的内容

请严格依据 output_contract 的 description 和 json_schema（如有）来判断。

输出格式——严格返回以下 JSON（只返回 JSON，不要 markdown 代码块）：
{
  "business_status": "goal_achieved | goal_partial | goal_failed",
  "reason": "判定依据（说明实际产出与期望的差距，或为何满足要求）"
}"""


class Evaluator:
    """双重判定——ExecutionStatus + BusinessStatus。

    v1 流程：
        1. NodeResult.error → ExecutionStatus（确定性映射）
        2. 执行成功时调 LLM 对比 artifact 内容 vs output_contract → BusinessStatus
        3. 执行失败时 BusinessStatus=GOAL_FAILED（无需 LLM）
        4. 组合判定 → suggested_action（规则推导）

    依赖（2 个）:
        infra（LLM client，用于 BusinessStatus 语义判定）
        artifact_store（读取实际产物内容）
    """

    def __init__(
        self,
        infra: AgentInfra,
        artifact_store: ArtifactStore,
    ) -> None:
        self._infra = infra
        self._artifact_store = artifact_store

    async def evaluate(
        self,
        result: NodeResult,
        node: DAGNode,
        plan: Plan,
    ) -> NodeResultEvaluation:
        """评估节点结果——Executor 产出 → Evaluator 双重判定。

        判定流程：
            1. NodeResult.error → ExecutionStatus（确定性）
            2. 产物内容 vs output_contract → BusinessStatus（LLM 语义判定）
            3. execution + business + node.required → suggested_action（规则推导）

        Args:
            result: Executor 返回的节点执行结果
            node:   原始 DAGNode（含 output_contract，用于 business 判定）
            plan:   当前 Plan（v1 仅用于上下文透传，不影响判定逻辑）

        Returns:
            NodeResultEvaluation（execution_status + business_status + reason + suggested_action + result）
        """
        # 1. 确定执行状态（确定性映射）
        execution_status = self._determine_execution_status(result)

        # 2. 判定业务状态
        if execution_status == ExecutionStatus.OK:
            # 执行成功 → LLM 语义比对产物 vs 契约
            business_status, reason = await self._judge_business(result, node)
        else:
            # 执行失败 → 无需 LLM，直接 goal_failed
            business_status = BusinessStatus.GOAL_FAILED
            reason = f"工具执行失败: {result.error.value if result.error else 'unknown'}"

        # 3. 推导建议动作
        suggested_action = self._derive_action(
            execution_status, business_status, node, result,
        )

        logger.info(
            "Evaluator: node=%s exec=%s biz=%s action=%s",
            node.node_id, execution_status.value, business_status.value, suggested_action.value,
        )

        return NodeResultEvaluation(
            node_id=result.node_id,
            execution_status=execution_status,
            business_status=business_status,
            reason=reason,
            suggested_action=suggested_action,
            result=result,
        )

    # ── 执行状态判定 ────────────────────────────────────────────────────────

    @staticmethod
    def _determine_execution_status(result: NodeResult) -> ExecutionStatus:
        """NodeResult.error → ExecutionStatus 确定性映射。

        无错误 = OK，有错误按类型映射。
        """
        if result.error is None:
            return ExecutionStatus.OK

        mapping: dict[NodeErrorCode, ExecutionStatus] = {
            NodeErrorCode.TOOL_TIMEOUT: ExecutionStatus.TIMEOUT,
            NodeErrorCode.PERMISSION_DENIED: ExecutionStatus.PERMISSION_DENIED,
            # 其余错误统一映射为 ERROR
            NodeErrorCode.TOOL_ERROR: ExecutionStatus.ERROR,
            NodeErrorCode.TOOL_NOT_FOUND: ExecutionStatus.ERROR,
            NodeErrorCode.NETWORK_ERROR: ExecutionStatus.ERROR,
            NodeErrorCode.WORKER_CRASH: ExecutionStatus.ERROR,
            NodeErrorCode.CONTEXT_OVERFLOW: ExecutionStatus.ERROR,
            NodeErrorCode.TOKEN_BUDGET_EXCEEDED: ExecutionStatus.ERROR,
        }
        mapped = mapping.get(result.error)
        if mapped is None:
            logger.warning(
                "Evaluator: 未知 NodeErrorCode=%s，fallback ExecutionStatus.ERROR",
                result.error,
            )
            return ExecutionStatus.ERROR
        return mapped

    # ── 业务状态判定 ────────────────────────────────────────────────────────

    async def _judge_business(
        self,
        result: NodeResult,
        node: DAGNode,
    ) -> tuple[BusinessStatus, str]:
        """LLM 语义判定：对比 artifact 实际内容 vs output_contract 期望。

        从 ArtifactStore 按 artifact_refs 取实际产物，与 output_contract 一起
        发给 LLM 做语义比对。

        Returns:
            (business_status, reason)
        """
        # 读取产物内容
        artifacts_data = await self._fetch_artifacts(result)

        # 构建 output_contract 描述
        contract_desc = node.output_contract.description or node.description
        json_schema = node.output_contract.json_schema

        contract_text = f"期望输出: {contract_desc}"
        if json_schema:
            contract_text += f"\n期望 JSON Schema: {json.dumps(json_schema, ensure_ascii=False)}"

        # 构建产物描述
        if artifacts_data:
            artifact_text = f"实际输出: {json.dumps(artifacts_data, ensure_ascii=False, default=str)}"
        else:
            artifact_text = "实际输出: （无产物数据）"

        user_message = f"{contract_text}\n\n{artifact_text}"

        logger.debug("Evaluator: node=%s LLM 业务判定上下文:\n%s", node.node_id, user_message)

        try:
            # 调用 LLM（空响应重试 1 次——DeepSeek 偶发）
            content = await self._call_llm_for_judge(user_message)
            if not content:
                logger.warning("Evaluator: node=%s LLM 返回空响应(已重试)，fallback goal_failed",
                               node.node_id)
                return BusinessStatus.GOAL_FAILED, "LLM 返回空响应（已重试1次仍为空）"

            data = json.loads(content.strip())
            biz_str = data.get("business_status")
            reason = data.get("reason", "")

            if biz_str is None:
                logger.warning(
                    "Evaluator: LLM 返回 JSON 缺少 business_status 键，"
                    "fallback goal_failed。keys=%s", list(data.keys()),
                )
                return BusinessStatus.GOAL_FAILED, (
                    f"LLM 响应缺少 business_status 字段（keys={list(data.keys())}）"
                )

            try:
                biz = BusinessStatus(biz_str)
            except ValueError:
                logger.warning(
                    "Evaluator: LLM 返回未知 business_status=%s，fallback goal_failed",
                    biz_str,
                )
                biz = BusinessStatus.GOAL_FAILED
                reason = f"（LLM 返回未知状态 {biz_str}，fallback）{reason}"

            return biz, reason

        except Exception as e:
            logger.exception("Evaluator: LLM 业务判定异常，fallback goal_failed")
            return BusinessStatus.GOAL_FAILED, f"LLM 判定异常: {e}"

    async def _call_llm_for_judge(self, user_message: str) -> str | None:
        """调用 LLM 做业务判定。空响应时重试 1 次（DeepSeek 偶发空 content）。

        Returns:
            LLM 响应文本，两次都失败时返回 None。
        """
        # TODO: 大产物 / 复杂 json_schema 可能超出上下文窗口，
        #       后续参考 ConstraintConfig.max_context_tokens 做截断。
        for attempt in range(2):
            try:
                response = await self._infra.llm.chat.completions.create(
                    model=self._infra.llm_model,
                    messages=[
                        {"role": "system", "content": _BUSINESS_JUDGE_SYSTEM},
                        {"role": "user", "content": user_message},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=1024,
                )
            except Exception:
                logger.warning(
                    "Evaluator: LLM 调用异常 (attempt %d/2)，%s",
                    attempt + 1,
                    "重试中..." if attempt == 0 else "放弃",
                    exc_info=True,
                )
                if attempt == 0:
                    continue
                return None
            content = response.choices[0].message.content
            if content:
                return content
            logger.warning(
                "Evaluator: LLM 返回空响应 (attempt %d/2)，%s",
                attempt + 1,
                "重试中..." if attempt == 0 else "放弃",
            )
        return None

    async def _fetch_artifacts(self, result: NodeResult) -> dict[str, Any]:
        """从 ArtifactStore 按 artifact_refs 获取实际产物数据。"""
        data: dict[str, Any] = {}
        for aid in result.artifact_refs:
            artifact = await self._artifact_store.get(aid)
            if artifact is not None:
                data[str(aid)] = artifact.content
        return data

    # ── 动作推导 ────────────────────────────────────────────────────────────

    @staticmethod
    def _derive_action(
        execution_status: ExecutionStatus,
        business_status: BusinessStatus,
        node: DAGNode,
        result: NodeResult,
    ) -> EvaluatorAction:
        """根据执行状态 + 业务状态 + 节点属性推导建议动作。

        规则表：
            OK + goal_achieved        → CONTINUE
            OK + goal_partial         → CONTINUE（部分达成但可接受）
            OK + goal_failed + req    → REPLAN（即使有产物也不静默传播垃圾数据）
            OK + goal_failed + !req   → SKIP
            TIMEOUT + req             → RETRY
            TIMEOUT + !req            → SKIP
            ERROR + req               → RETRY¹
            ERROR + !req              → SKIP
            PERMISSION_DENIED         → ABORT
            CANCELLED                 → ABORT

            ¹ TOOL_NOT_FOUND / CONTEXT_OVERFLOW → REPLAN
        """
        # 权限拒绝 → 直接中止
        # 取消：_determine_execution_status 当前不产出 CANCELLED，
        # 此分支为外部预留——Orchestrator 可能在任务级取消时直接传 CANCELLED
        if execution_status in (ExecutionStatus.PERMISSION_DENIED, ExecutionStatus.CANCELLED):
            return EvaluatorAction.ABORT

        # 执行成功，看业务结果
        if execution_status == ExecutionStatus.OK:
            if business_status == BusinessStatus.GOAL_ACHIEVED:
                return EvaluatorAction.CONTINUE
            elif business_status == BusinessStatus.GOAL_PARTIAL:
                return EvaluatorAction.CONTINUE
            else:  # goal_failed —— 工具声称成功但 Evaluator 判定目标未达成
                # Evaluator 已明确判定产出不满足 output_contract。
                # 即使有 artifact（如搜索结果不相关），也不应静默放行——
                # 垃圾数据沿 DAG 传播会导致下游也失败，最终无声产出无用结果。
                # REPLAN 让 Planner 根据评估反馈换策略重试（熔断上限兜底）。
                logger.warning(
                    "Evaluator: node=%s exec=OK biz=goal_failed — %s",
                    node.node_id,
                    "REPLAN" if node.required else "SKIP",
                )
                return EvaluatorAction.REPLAN if node.required else EvaluatorAction.SKIP

        # 执行失败（TIMEOUT / ERROR）
        if execution_status == ExecutionStatus.TIMEOUT:
            return EvaluatorAction.RETRY if node.required else EvaluatorAction.SKIP

        if execution_status == ExecutionStatus.ERROR:
            # 部分错误不适合重试（如 TOOL_NOT_FOUND）
            if result.error in (NodeErrorCode.TOOL_NOT_FOUND, NodeErrorCode.CONTEXT_OVERFLOW):
                return EvaluatorAction.REPLAN if node.required else EvaluatorAction.SKIP
            return EvaluatorAction.RETRY if node.required else EvaluatorAction.SKIP

        return EvaluatorAction.CONTINUE
