"""Executor — v1 单工具直接调用（LLM 匹配 + ToolRegistry 执行）。

接收单个 DAGNode → LLM 工具匹配（function-calling） → ToolRegistry 调用 → 存储结果。

v1 不做 DBT 生成、不做 Worker Agent 分配——每个 DAGNode 直接匹配工具并调用。
v2 考虑 DBT：Sequence/Selector/Parallel/Action/Condition 行为树 + Worker Agent 池。
v2 考虑审批风暴检测：执行前扫描所有节点 → 汇总 MUTATION 步骤 → 批量审批预览。
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from Module.task_system.schema import (
    Artifact,
    ArtifactId,
    DAGNode,
    NodeErrorCode,
    NodeResult,
    TaskId,
    Tool,
)
from Module.tool.protocol import ToolContext, ToolResult

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.infra import AgentInfra
    from Module.task_system.protocol import HITLNotifier

logger = logging.getLogger(__name__)

# LLM 直出文本的哨兵值——当 _match_tool 返回此 tool_name 时，
# 表示 LLM 判断不需要外部工具，直接用自身能力完成了节点（如汇总/分析/改写）。
_DIRECT_TEXT_SENTINEL = "__llm_direct__"

# LLM 工具匹配的系统提示词
_TOOL_MATCH_SYSTEM = """你是一个任务执行专家。根据节点的描述和可用的工具列表，选择最合适的工具并填写参数。

规则：
1. 仔细阅读节点的 description，理解需要做什么
2. 从可用工具中选择最匹配的工具
3. 根据 description 和上游上下文数据填写工具参数
4. 如果没有任何工具能匹配，不要强行选择
"""


class Executor:
    """DAG 节点的执行引擎。

    v1（当前）：单个 DAGNode → LLM 工具匹配（function-calling） → ToolRegistry 直接调用 → 存储结果。
    v2（规划中）：DBT 生成 + Worker Agent 池分配 + 审批风暴检测。

    v1 审批：完全依赖 ToolManager 内置 HITL（SafetyPipeline._run_hitl），
    Executor 不主动推送审批。v2 由 Executor 通过 HITLNotifier 推送审批。

    职责：
        1. LLM 工具匹配：根据 node.description 选择工具并填写参数（一次 function-calling）
        2. 工具调用：透传参数到 ToolRegistry，由其内部处理验证/安全/审批/超时
        3. 结果存储：ToolResult → Artifact（artifact_id = node.output_as） → ArtifactStore

    不负责：
        - 找 ready nodes（Orchestrator）
        - 评估结果（Evaluator —— 产出 NodeResultEvaluation）
        - 生成 DAG / Replan（Planner）
        - v1: DBT 生成、Worker Agent 分配、审批风暴检测
    """

    def __init__(
        self,
        infra: AgentInfra,
        artifact_store: ArtifactStore,
        hitl_notifier: HITLNotifier,
    ) -> None:
        """注入 Executor 依赖。

        Args:
            infra:          共享基础设施（llm 用于工具匹配, tool_registry 用于工具执行）
            artifact_store: 产物存储（解析 input_bindings + 存储执行结果）
            hitl_notifier:  v2 审批风暴检测用，v1 不调用，仅持有引用
        """
        self._infra = infra
        self._artifact_store = artifact_store
        self._hitl = hitl_notifier  # v2 审批风暴检测用，v1 不调用

    async def execute(
        self,
        node: DAGNode,
        task_id: TaskId,
        tools: list[Tool],
        session_id: str = "",
    ) -> NodeResult:
        """执行单个 DAGNode——LLM 匹配工具（或直出文本）→ 调用/存储结果。

        流程：
            1. 解析上游输入：input_bindings → ArtifactStore.get()
            2. LLM 工具匹配：node.description + tools schema → function-calling →
               - 匹配到工具: (tool_name, params)
               - LLM 直出文本: ("__llm_direct__", {"content": ...})  ← 汇总/分析类节点
            3. 调用工具（仅匹配到工具时）/ 直接存储 LLM 文本
            4. 存储结果：→ Artifact → artifact_store.put()

        Args:
            node:   待执行的 DAG 节点（含 description、input_bindings、output_as）
            task_id:所属任务 ID
            tools:  本次任务可用的工具列表（从 DelegationContext 透传）

        Returns:
            NodeResult（artifact_refs + error + attempt + duration_ms）
            attempt=0 由 Orchestrator 在调用前设置（Executor 不追踪重试次数）
        """
        start = time.monotonic()

        try:
            # 1. 解析上游输入（input_bindings → ArtifactStore）
            upstream_data = await self._resolve_inputs(node, task_id)

            # 2. LLM 工具匹配（function-calling：选工具 + 填参数）
            #    或 LLM 直出文本（汇总/分析类节点不需要外部工具）
            tool_name, params = await self._match_tool(node, tools, upstream_data)

            # 3. 调用工具（或直接使用 LLM 直出的文本）
            if tool_name == _DIRECT_TEXT_SENTINEL:
                # LLM 直出文本——不需要外部工具
                content = params["content"]
                tool_result = ToolResult.ok(
                    name="llm_direct",
                    content=content,
                    duration_ms=0.0,
                )
                logger.info(
                    "Executor: node=%s LLM 直出文本作为产物 (%d chars)",
                    node.node_id, len(content),
                )
            else:
                tool_result = await self._call_tool(tool_name, params, task_id, node.node_id, session_id)

            # 4. 存储结果（ToolResult → Artifact → ArtifactStore）
            artifact = await self._store_result(node, task_id, tool_result)

            duration_ms = int((time.monotonic() - start) * 1000)

            if tool_result.success:
                return NodeResult(
                    node_id=node.node_id,
                    artifact_refs=[artifact.artifact_id] if artifact else [],
                    attempt=0,
                    duration_ms=duration_ms,
                )
            else:
                return NodeResult(
                    node_id=node.node_id,
                    artifact_refs=[artifact.artifact_id] if artifact else [],
                    error=self._map_error(tool_result),
                    attempt=0,
                    duration_ms=duration_ms,
                )

        except _ToolMatchError as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning("Executor: node=%s 工具匹配失败: %s", node.node_id, e)
            return NodeResult(
                node_id=node.node_id,
                error=NodeErrorCode.TOOL_NOT_FOUND,
                attempt=0,
                duration_ms=duration_ms,
            )

        except Exception:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.exception("Executor: node=%s 执行异常", node.node_id)
            return NodeResult(
                node_id=node.node_id,
                error=NodeErrorCode.TOOL_ERROR,
                attempt=0,
                duration_ms=duration_ms,
            )

    # ── 输入解析 ────────────────────────────────────────────────────────────

    async def _resolve_inputs(
        self, node: DAGNode, task_id: TaskId,
    ) -> dict[ArtifactId, Any]:
        """解析 input_bindings → 从 ArtifactStore 获取上游产物数据。

        上游节点执行完成后将产物以 output_as 为 key 存入 ArtifactStore，
        下游节点通过 input_bindings 引用这些 key 来获取上下文数据。
        """
        inputs: dict[ArtifactId, Any] = {}
        for aid in node.input_bindings:
            artifact = await self._artifact_store.get(aid)
            if artifact is not None:
                inputs[aid] = artifact.content
            else:
                logger.warning(
                    "Executor: node=%s task=%s 上游产物 %s 不存在",
                    node.node_id, task_id, aid,
                )
        return inputs

    # ── LLM 工具匹配 ────────────────────────────────────────────────────────

    async def _match_tool(
        self,
        node: DAGNode,
        tools: list[Tool],
        upstream_data: dict[ArtifactId, Any],
    ) -> tuple[str, dict[str, Any]]:
        """LLM function-calling：根据 node.description 匹配工具并填写参数。

        流程：
            1. 从 ToolRegistry 获取完整的 OpenAI function-calling 格式 schema
            2. 按 tools 列表过滤（只保留本次任务允许的工具）
            3. 调用 LLM（DeepSeek v4），传入 tools + tool_choice="auto"
               - 匹配到工具 → 返回 (tool_name, params)
               - LLM 直出文本 → 返回 ("__llm_direct__", {"content": ...})
            4. 解析返回的 tool_calls → (tool_name, params)

        Returns:
            (tool_name, params_dict)
            tool_name 为 "__llm_direct__" 时表示 LLM 直出文本（汇总/分析类节点）

        Raises:
            _ToolMatchError: LLM 未返回 tool_call 且无文本内容，或参数解析失败
        """
        # 从 ToolRegistry 获取完整 schema，按 tools 列表过滤
        allowed_names = {t.name for t in tools}
        # infra.tool_registry 类型为 object（避免循环导入），运行时是 ToolManager
        all_defs = self._infra.tool_registry.definitions()  # type: ignore[attr-defined]
        tool_defs = [d for d in all_defs if d["function"]["name"] in allowed_names]

        if not tool_defs:
            # 无可用工具——LLM 可能仍能通过直出文本完成（如 compose_answer 节点）
            logger.info(
                "Executor: node=%s 无可用的工具（allowed=%s），尝试 LLM 直出文本",
                node.node_id, allowed_names,
            )
            # 继续走 LLM 调用——tool_choice="auto" 且 tool_defs=[] 时，
            # LLM 将返回纯文本 content → 走 _DIRECT_TEXT_SENTINEL 路径
            # 如果 LLM 仍返回 tool_call（幻觉），由下方正常流程处理

        # 构建上下文消息：node 描述 + 期望输出 + 上游数据
        context_parts = [f"节点目标: {node.description}"]
        if node.output_contract and node.output_contract.description:
            context_parts.append(f"期望输出: {node.output_contract.description}")
        if upstream_data:
            context_parts.append(f"上游数据: {upstream_data}")

        user_message = "\n".join(context_parts)

        logger.debug("Executor: node=%s 开始工具匹配, allowed_tools=%s",
                     node.node_id, allowed_names)
        logger.debug("Executor: node=%s LLM 上下文:\n%s", node.node_id, user_message)

        # 调用 LLM（function-calling）
        # 注意：DeepSeek v4 thinking 模式不支持 tool_choice="required"，
        # 使用 "auto" 让模型自行判断是否需要调用工具。
        #
        # TODO: tool_choice="auto" + LLM 直出文本的设计有职责越界的问题。
        #   Planner 在生成 DAG 时就知道哪些节点是"纯 LLM 任务"（汇总/分析/改写），
        #   哪些需要外部工具。当前 Executor 替 Planner 做了这个决策——
        #   LLM 判断不需要工具 → 返回文本 → Executor 当作有效产物。
        #
        #   更好的方案：DAGNode 增加 execution_mode 字段（tool_call / llm_direct），
        #   Planner 在生成 DAG 时显式指定。Executor 按标注执行，不需要猜测。
        #   llm_direct 模式跳过 tool matching，直接调 LLM 生成内容，省一次调用。
        #   当前方案作为 MVP 够用，后续 Planner 成熟后迁移到方案 C。
        response = await self._infra.llm.chat.completions.create(
            model=self._infra.llm_model,
            messages=[
                {"role": "system", "content": _TOOL_MATCH_SYSTEM},
                {"role": "user", "content": user_message},
            ],
            tools=tool_defs,
            tool_choice="auto",
            max_tokens=2048,
        )

        msg = response.choices[0].message
        if not msg.tool_calls:
            # tool_choice="auto" 时 LLM 可能判断不需要外部工具，
            # 直接返回文本完成节点（如汇总、分析、改写等纯 LLM 任务）
            if msg.content:
                logger.info(
                    "Executor: node=%s LLM 直出文本（无 tool_call），作为有效产物",
                    node.node_id,
                )
                return _DIRECT_TEXT_SENTINEL, {"content": msg.content}
            raise _ToolMatchError("LLM 未返回工具调用且无文本内容")

        tc = msg.tool_calls[0]
        tool_name = tc.function.name
        try:
            params = json.loads(tc.function.arguments)
        except json.JSONDecodeError as e:
            raise _ToolMatchError(f"LLM 返回的参数 JSON 解析失败: {e}") from e

        logger.info(
            "Executor: node=%s 匹配到工具 %s, params_keys=%s",
            node.node_id, tool_name, list(params.keys()),
        )
        return tool_name, params

    # ── 工具调用 ────────────────────────────────────────────────────────────

    async def _call_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        task_id: TaskId,
        node_id: Any,
        session_id: str = "",
    ) -> ToolResult:
        """通过 ToolRegistry 执行工具——v1 直接透传。

        ToolManager 内部已处理：参数验证、安全检查、HITL 审批、并发控制、超时、token 截断。
        Executor 只负责透传参数和收集结果。
        """
        ctx = ToolContext(
            session_id=session_id,
            trace_id=f"{task_id}/{node_id}",
        )
        # infra.tool_registry 类型为 object（避免循环导入），运行时是 ToolManager
        result = await self._infra.tool_registry.execute(  # type: ignore[attr-defined]
            tool_name,
            ctx=ctx,
            **params,
        )
        logger.info(
            "Executor: tool=%s success=%s duration=%.0fms",
            tool_name, result.success, result.duration_ms,
        )
        return result

    # ── 结果存储 ────────────────────────────────────────────────────────────

    async def _store_result(
        self,
        node: DAGNode,
        task_id: TaskId,
        tool_result: ToolResult,
    ) -> Artifact | None:
        """将 ToolResult 包装为 Artifact 并存入 ArtifactStore。

        artifact_id 使用 node.output_as，下游节点通过 input_bindings 引用，
        形成 DAG 的数据流（push 模式：上游产出 → 下游引用）。
        """
        if not node.output_as:  # None 或空串均视为无产物
            logger.warning("Executor: node=%s 没有 output_as，跳过存储", node.node_id)
            return None

        artifact = Artifact(
            artifact_id=node.output_as,
            artifact_type=(
                "text" if tool_result.result_type == "text"
                else tool_result.result_type
            ),
            node_id=node.node_id,
            task_id=task_id,
            content=tool_result.content if tool_result.success else None,
            metadata={
                "tool_name": tool_result.tool_name,
                "success": tool_result.success,
                "duration_ms": tool_result.duration_ms,
                "error": tool_result.error,
                "error_type": (
                    tool_result.error_type.value
                    if tool_result.error_type else None
                ),
                "llm_summary": tool_result.llm_summary,
            },
        )
        await self._artifact_store.put(artifact)
        logger.debug(
            "Executor: node=%s 产物已存储 artifact_id=%s",
            node.node_id, node.output_as,
        )
        return artifact

    # ── 错误映射 ────────────────────────────────────────────────────────────

    @staticmethod
    def _map_error(tool_result: ToolResult) -> NodeErrorCode:
        """将 ToolResult.error_type 映射为 NodeErrorCode。

        ToolManager 的 ErrorType 和 task_system 的 NodeErrorCode 是两套独立的
        错误码体系——Executor 负责桥接。
        """
        from Module.tool.protocol import ErrorType as ToolErrorType

        mapping: dict[Any, NodeErrorCode] = {
            ToolErrorType.INVALID_ARGUMENT: NodeErrorCode.TOOL_ERROR,
            ToolErrorType.TIMEOUT: NodeErrorCode.TOOL_TIMEOUT,
            ToolErrorType.PERMISSION_DENIED: NodeErrorCode.PERMISSION_DENIED,
            ToolErrorType.RATE_LIMITED: NodeErrorCode.TOOL_ERROR,
            ToolErrorType.NETWORK_ERROR: NodeErrorCode.NETWORK_ERROR,
            ToolErrorType.UNAVAILABLE: NodeErrorCode.TOOL_ERROR,
            ToolErrorType.CANCELLED: NodeErrorCode.TOOL_ERROR,
            ToolErrorType.TOOL_ERROR: NodeErrorCode.TOOL_ERROR,
            ToolErrorType.UNKNOWN_TOOL: NodeErrorCode.TOOL_NOT_FOUND,
        }
        return mapping.get(tool_result.error_type, NodeErrorCode.TOOL_ERROR)


class _ToolMatchError(Exception):
    """LLM 工具匹配失败——无法为节点选择合适的工具。"""
    pass
