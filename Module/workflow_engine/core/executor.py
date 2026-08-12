"""Executor — v1 单工具直接调用（LLM 匹配 + ToolRegistry 执行）。

接收单个 DAGNode → LLM 工具匹配（function-calling） → ToolRegistry 调用 → 存储结果。

v1 不做 DBT 生成、不做 Worker Agent 分配——每个 DAGNode 直接匹配工具并调用。
v2 考虑 DBT：Sequence/Selector/Parallel/Action/Condition 行为树 + Worker Agent 池。
v2 考虑审批风暴检测：执行前扫描所有节点 → 汇总 MUTATION 步骤 → 批量审批预览。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from Module.workflow_contract import NodeId, SessionId, WorkflowId, WorkflowStatus
from Module.tool.tool_descriptor import Tool
from Module.workflow_engine.cancel import check_cancelled
from Module.workflow_engine.schema import (
    Artifact,
    ArtifactId,
    DAGNode,
    ExecutionMode,
    NodeErrorCode,
    NodeResult,
    TokenComponentType,
)
from Module.tool.protocol import ToolContext, ToolResult

if TYPE_CHECKING:
    from Module.workflow_engine.artifact_store import ArtifactStore
    from Module.workflow_contract import AgentInfra
    from Module.workflow_contract import HITLNotifier
    from Module.workflow_engine.schema.internal import WorkflowState
    from Module.workflow_engine.token_ledger import TokenLedger

logger = logging.getLogger(__name__)

# LLM 直出文本的哨兵值——当 _match_tool 返回此 tool_name 时，
# 表示 LLM 判断不需要外部工具，直接用自身能力完成了节点（如汇总/分析/改写）。
_DIRECT_TEXT_SENTINEL = "__llm_direct__"

# LLM 工具匹配的系统提示词
_TOOL_MATCH_SYSTEM = """你是一个任务执行专家。根据节点的描述和可用的工具列表，选择最合适的工具并填写参数。

## 规则
1. 仔细阅读节点的 description，理解需要做什么
2. 从可用工具中选择最匹配的工具
3. 根据 description 和上游上下文数据填写工具参数
4. 如果没有任何工具能匹配，不要强行选择

## 重要：何时不调用工具
- 如果节点的工作是**汇总/编译/整理/改写/总结**上游已有数据，**不要调用任何工具**，
  直接返回处理后的文本即可（即使工具列表中有 get_time、web_search 等）。
- **只有**在需要获取**新的外部数据**时才调用工具。
- 判断标准：上游数据中是否已经包含了完成此节点所需的全部信息？
  如果是 → 不调工具，直接输出文本。
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
        token_ledger: TokenLedger | None = None,
    ) -> None:
        """注入 Executor 依赖。

        Args:
            infra:          共享基础设施（llm 用于工具匹配, tool_registry 用于工具执行）
            artifact_store: 产物存储（解析 input_bindings + 存储执行结果）
            hitl_notifier:  v2 审批风暴检测用，v1 不调用，仅持有引用
            token_ledger:   可选的 token 账本，用于记录 LLM 调用消耗
        """
        self._infra = infra
        self._artifact_store = artifact_store
        self._hitl = hitl_notifier  # v2 审批风暴检测用，v1 不调用
        self._token_ledger = token_ledger

    async def execute(
        self,
        node: DAGNode,
        task_id: WorkflowId,
        tools: list[Tool],
        session_id: SessionId,
        ts: "WorkflowState",
    ) -> NodeResult:
        """执行单个 DAGNode——LLM 匹配工具（或直出文本）→ 调用/存储结果。

        E3 §3.3 取消语义：
            - 四个 `check_cancelled(ts)` 边界检查点（①执行前 / ②LLM 返回后 / ③工具返回后 / ④存储前）
            - LLM 匹配/直出文本**不 shield**——owned.cancel() 直接打断 in-flight await（强取消）
            - 工具调用 **asyncio.shield 保护**——已开始工具完成（副作用不打断）、结果弃用、
              不再开始下一个调用；孤儿工具登记到 `self._infra.orphan_tools`（跨任务共享容器，stop() 采集）

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
            session_id: 会话 ID（工具调用上下文）
            ts:     任务级状态（E3——取消检查边界 token，await 边界一查即中）

        Returns:
            NodeResult（artifact_refs + error + attempt + duration_ms）
            attempt=0 由 Orchestrator 在调用前设置（Executor 不追踪重试次数）
        """
        start = time.monotonic()

        try:
            check_cancelled(ts)                                # ① 执行前（任务已取消 → 不启动）

            # 1. 解析上游输入（input_bindings → ArtifactStore）
            upstream_data = await self._resolve_inputs(node, task_id)

            # 2. 按 execution_mode 分派
            if node.execution_mode == ExecutionMode.LLM_DIRECT:
                # Planner 显式标注的纯 LLM 任务——跳过工具匹配，直接调 LLM
                content = await self._call_llm_direct(node, upstream_data)
                check_cancelled(ts)                            # 直出路径检查点（不 shield，②④ 适用）
                tool_result = ToolResult.ok(
                    name="llm_direct",
                    content=content,
                    duration_ms=0.0,
                )
                logger.info(
                    "Executor: node=%s llm_direct 产出 (%d chars)",
                    node.node_id, len(content),
                )
            else:
                # tool_call 模式：function-calling 匹配工具 → 调用
                # 或 LLM 自行判断不需要工具 → __llm_direct__
                tool_name, params = await self._match_tool(node, tools, upstream_data)

                check_cancelled(ts)                            # ② LLM 返回后、工具调用前

                if tool_name == _DIRECT_TEXT_SENTINEL:
                    content = params["content"]
                    check_cancelled(ts)
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
                    # 工具调用（shield——在途副作用不打断，结果弃用、不再开始下一个调用）
                    tool_task = asyncio.create_task(
                        self._call_tool(tool_name, params, task_id, node.node_id, session_id)
                    )
                    self._infra.orphan_tools.add(tool_task)     # 登记孤儿工具（跨任务共享容器，stop() 采集）
                    tool_task.add_done_callback(self._infra.orphan_tools.discard)  # 收引用 + 防泄漏
                    tool_task.add_done_callback(
                        lambda t: t.exception() if not t.cancelled() else None  # 收「Task exception was never retrieved」
                    )
                    try:
                        tool_result = await asyncio.shield(tool_task)
                    except asyncio.CancelledError:
                        check_cancelled(ts)                    # ③ 工具返回后被取消 → 走 CANCELLED
                        raise                                   # 否则正常抛（非取消语义的 CancelledError）

            check_cancelled(ts)                                # ④ 存储前（取消后结果不落 artifact）

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
        self, node: DAGNode, task_id: WorkflowId,
    ) -> dict[ArtifactId, Any]:
        """解析 input_bindings → 从 ArtifactStore 获取上游产物数据。

        上游节点执行完成后将产物以 output_as 为 key 存入 ArtifactStore，
        下游节点通过 input_bindings 引用这些 key 来获取上下文数据。

        Args:
            node:    待执行的 DAG 节点
            task_id: 所属任务 ID

        Returns:
            以 artifact_id 为 key、产物内容为 value 的字典
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

        Args:
            node:          待执行的 DAG 节点
            tools:         本次任务可用的工具列表
            upstream_data: 上游产物数据（artifact_id → 内容）

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

        # 调用 LLM（function-calling）——仅 execution_mode=tool_call 节点进入此分支。
        # llm_direct 节点已在 execute() 入口分流到 _call_llm_direct()，不经过 tool matching。
        # max_tokens 需足够大以容纳上游数据（如完整报告正文）作为函数参数的节点。
        # 2048 不够（4000+ char 报告会被截断），4096 作为安全下限。
        response = await self._infra.llm.chat.completions.create(
            model=self._infra.llm_model,
            messages=[
                {"role": "system", "content": _TOOL_MATCH_SYSTEM},
                {"role": "user", "content": user_message},
            ],
            tools=tool_defs,
            tool_choice="auto",
            max_tokens=4096,
        )

        # ── token 追踪 ──
        if self._token_ledger and (usage := response.usage):
            try:
                self._token_ledger.record(
                    component=TokenComponentType.EXECUTOR,
                    node_id=node.node_id,
                    prompt_tokens=usage.prompt_tokens or 0,
                    completion_tokens=usage.completion_tokens or 0,
                    model=self._infra.llm_model,
                )
            except Exception:
                logger.warning("TokenLedger.record 失败，跳过", exc_info=True)

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
        tc_any: Any = tc  # type: ignore[assignment]
        tool_name = tc_any.function.name
        # definitions() 暴露脱点名（OpenAI/Anthropic/Gemini 禁点号，desktop.* →
        # desktop_*），workflow 子 LLM 原样回传脱点名；_call_tool 需要运行时名
        # （ToolManager 以 desktop.file.read 为键）。未知/合成名原样透传。
        resolver = getattr(self._infra.tool_registry, "resolve_llm_name", None)
        if resolver is not None:
            resolved = resolver(tool_name)
            if isinstance(resolved, str) and resolved:
                tool_name = resolved
        try:
            params = json.loads(tc_any.function.arguments)
        except json.JSONDecodeError as e:
            raise _ToolMatchError(f"LLM 返回的参数 JSON 解析失败: {e}") from e

        logger.info(
            "Executor: node=%s 匹配到工具 %s, params_keys=%s",
            node.node_id, tool_name, list(params.keys()),
        )
        return tool_name, params

    # ── LLM 直出文本（execution_mode=llm_direct）─────────────────────────

    async def _call_llm_direct(
        self,
        node: DAGNode,
        upstream_data: dict[ArtifactId, Any],
    ) -> str:
        """Planner 标注了 llm_direct——直接调 LLM 生成文本，跳过工具匹配。

        建模为简单的 chat completion（无 tools），让 LLM 基于上游数据
        和节点描述生成内容。token 消耗由 token_ledger 记录。

        Args:
            node:          待执行的 DAG 节点
            upstream_data: 上游产物数据（artifact_id → 内容）

        Returns:
            LLM 生成的文本内容
        """
        system = "你是一个任务执行助手。根据上游数据和节点描述，直接产出结果。"

        parts = [f"任务: {node.description}"]
        if node.output_contract and node.output_contract.description:
            parts.append(f"期望产出: {node.output_contract.description}")
        if upstream_data:
            # 截断过长数据，保留足够上下文
            data_text = json.dumps(upstream_data, ensure_ascii=False, default=str)
            if len(data_text) > 30000:
                data_text = data_text[:30000] + f"\n...[截断 {len(data_text) - 30000} chars]"
            parts.append(f"上游数据:\n{data_text}")

        user = "\n\n".join(parts)
        logger.debug("Executor: node=%s llm_direct prompt (%d chars)", node.node_id, len(user))

        response = await self._infra.llm.chat.completions.create(
            model=self._infra.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=8192,
        )

        # ── token 追踪 ──
        if self._token_ledger and (usage := response.usage):
            try:
                self._token_ledger.record(
                    component=TokenComponentType.EXECUTOR,
                    node_id=node.node_id,
                    prompt_tokens=usage.prompt_tokens or 0,
                    completion_tokens=usage.completion_tokens or 0,
                    model=self._infra.llm_model,
                )
            except Exception:
                logger.warning("TokenLedger.record 失败，跳过", exc_info=True)

        content = response.choices[0].message.content or ""
        return content

    # ── 工具调用 ────────────────────────────────────────────────────────────

    async def _call_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        task_id: WorkflowId,
        node_id: NodeId,
        session_id: SessionId = SessionId(""),
    ) -> ToolResult:
        """通过 ToolRegistry 执行工具——v1 直接透传。

        ToolManager 内部已处理：参数验证、安全检查、HITL 审批、并发控制、超时、token 截断。
        Executor 只负责透传参数和收集结果。

        Args:
            tool_name:  工具名称
            params:     工具参数
            task_id:    所属任务 ID
            node_id:    节点 ID（用于 trace_id）
            session_id: 会话 ID

        Returns:
            工具执行结果
        """
        ctx = ToolContext(
            session_id=session_id,  # type: ignore[call-arg]
            trace_id=f"{task_id}/{node_id}",  # type: ignore[call-arg]
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
        task_id: WorkflowId,
        tool_result: ToolResult,
    ) -> Artifact | None:
        """将 ToolResult 包装为 Artifact 并存入 ArtifactStore。

        artifact_id 使用 node.output_as，下游节点通过 input_bindings 引用，
        形成 DAG 的数据流（push 模式：上游产出 → 下游引用）。

        Args:
            node:        待执行的 DAG 节点
            task_id:     所属任务 ID
            tool_result: 工具执行结果

        Returns:
            存储后的 Artifact 对象，无 output_as 时返回 None
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

        Args:
            tool_result: 工具执行结果

        Returns:
            映射后的 NodeErrorCode
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
