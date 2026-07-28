"""DelegationPreparer: LLM 准备委派上下文。

CognitiveLoop 调用 submit_task 后、TaskSystem.submit() 之前，
用一次 LLM 调用准备干净的委派上下文。

v1: 使用 DeepSeek v4，内部创建 AsyncOpenAI（DelegationPreparer 在 TaskSystem 创建前运行，
    无法使用 AgentInfra，因此自建客户端。Planner 已改用 infra.llm）。
Prompt 全文见 architecture.md Section 9。
"""

from __future__ import annotations

import json
import logging
import os
import re

from openai import AsyncOpenAI

from core.schema.message import Message
from core.schema.part import TextPart

from Module.task_system.utils import format_tools, strip_markdown_fence

from Module.task_system.schema import (
    ConstraintConfig,
    DelegationContext,
    DelegationResult,
    DelegationStatus,
    ReturnMode,
    SessionId,
    TaskConstraints,
    Tool,
    UserProfile,
)

logger = logging.getLogger(__name__)

# ── System Prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是任务委派准备器。你的职责不是规划执行步骤，而是判断任务可行性并准备干净的上下文。

## 四个职责
1. **意图透传**: intent/hints 原样保留，不做修改
2. **工具筛选**: 从可用工具列表中选出完成此任务需要的工具。只选直接相关的，不确定就不选
3. **上下文摘要**: 从对话历史中提取与任务相关的信息（≤500字）。解析指代、提取偏好
4. **任务约束估算**: 根据意图复杂度估算约束值
   - max_dag_nodes: 简单查询 1-3，中等任务 4-7，复杂任务 8-15
   - max_mutations: 无敏感操作为 0，1-2 次为 1-2，多次为 3-5
   - max_total_tokens: 通常留 null，使用全局默认
   - max_task_duration_s: 通常留 null，使用全局默认
   - max_failure_count: 通常留 null，使用全局默认

## 输出格式
严格返回以下 JSON（只返回 JSON，不要 markdown 代码块）：
{
  "status": "ok | no_matching_tools | too_vague",
  "reason": "判定理由（始终填写，便于调试）",
  "tools": ["tool_name_1", "tool_name_2"],
  "summarized_context": "从对话历史中提取的相关上下文（≤500字）",
  "max_dag_nodes": 3,
  "max_mutations": 1,
  "max_total_tokens": null,
  "max_task_duration_s": null,
  "max_failure_count": null,
  "missing_tools": ["缺少的工具类别名称"],
  "suggestion": "给用户的澄清建议（仅 too_vague 时填写）"
}

## 判定标准
- **ok**: 任务意图清晰 + 有匹配工具 → 输出筛选后的 tools + 上下文摘要 + 约束估算
- **no_matching_tools**: 意图清晰但缺少所需工具 → missing_tools 列出缺什么
- **too_vague**: 意图模糊，无法确定工具 → suggestion 给出澄清建议

## 注意事项
- 不要规划执行步骤！你不是 Planner
- 不要修改 intent
- 工具筛选宁缺毋滥——不确定是否需要就不选
- missing_tools 仅在 no_matching_tools 时填写
- suggestion 仅在 too_vague 时填写

{global_constraints}"""

_USER_PROMPT = """## 任务意图
{intent}

## 提示
{hints}

## 用户画像
{user_profile}

## 可用工具
{tools}

## 对话历史
{session_messages}"""


class DelegationPreparer:
    """一次 LLM 调用，准备委派上下文。

    代码在 TaskSystem 模块内，调用者却是 CognitiveLoop——
    CognitiveLoop 截获 LLM 的 submit_task tool_call 后调 prepare()，
    拿到 DelegationResult 后决定 submit / fail / 澄清。
    将来可独立为单独的 delegation 服务。

    四个职责（仅 status=ok 时产出 DelegationContext）：
    1. 意图透传（intent/hints 原样写入 DelegationContext）
    2. 工具筛选（从全量 Tool 列表中选出相关的，传给 Planner）
    3. 上下文摘要（从对话历史中提取相关信息，≤500 字）
    4. 任务级约束（估算 max_dag_nodes / max_mutations 等 TaskConstraints 字段）

    v1: 内部创建 AsyncOpenAI（读 DEEPSEEK_API_KEY）。Planner 已改用 infra.llm。
        后续独立为 delegation 服务时改为 LLM client 注入。
    """

    def __init__(self) -> None:
        """Initialize the delegation preparer with a DeepSeek LLM client."""
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY 环境变量未设置，DelegationPreparer 无法创建 DeepSeek client"
            )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )

    async def prepare(
        self,
        intent: str,
        hints: str | None,
        session_messages: list[Message],
        user_profile: UserProfile,
        tools: list[Tool],
        global_constraints: ConstraintConfig,
        session_id: SessionId,
    ) -> DelegationResult:
        """一次 LLM 调用，准备委派上下文。

        Args:
            intent:              LLM 输出的完整任务意图（透传，不修改）
            hints:               LLM 观察到的元信息（紧急程度、偏好等）
            session_messages:    全部对话消息（不截断，LLM 自己判断相关性）
                                 TODO: 当前为裸 list[dict]（{"role":...,"content":...} 格式），
                                 后续统一为 ChatMessageV2 后重构此接口。
            user_profile:        用户画像（只读消费）
            tools:               全量工具列表（CognitiveLoop 持有，DelegationPreparer 筛选）
            global_constraints:  TaskSystem 全局约束（天花板）
            session_id:          会话 ID（CognitiveLoop 透传，写入 DelegationContext）

        Returns:
            DelegationResult——ok / no_matching_tools / too_vague
        """
        # ── 构建 prompt ──
        # 用 replace 而非 format()——system prompt 含 JSON 示例的 { }，
        # format() 会把它们也当占位符导致 KeyError
        system = _SYSTEM_PROMPT.replace(
            "{global_constraints}",
            self._format_constraints(global_constraints),
        )
        user = self._build_user_prompt(
            intent, hints, session_messages, user_profile, tools,
        )

        # ── LLM 调用 ──
        logger.debug("DelegationPreparer user prompt:\n%s", user)
        raw, token_usage = await self._call_llm_with_usage(system, user)
        logger.debug("DelegationPreparer LLM 响应:\n%s", raw)

        # ── 解析 + 校验 ──
        parsed = self._parse_response(raw)
        validated = self._validate_output(parsed, tools)

        # ── 构建结果 ──
        result = self._build_result(validated, intent, hints, tools, session_id)
        result.token_usage = token_usage
        return result

    # ── 内部：构建 prompt ────────────────────────────────────────────────────

    def _build_user_prompt(
        self,
        intent: str,
        hints: str | None,
        session_messages: list[Message],
        user_profile: UserProfile,
        tools: list[Tool],
    ) -> str:
        """构建 user prompt——格式化所有输入。

        Args:
            intent: 任务意图文本。
            hints: 可选的提示信息。
            session_messages: 对话历史消息列表。
            user_profile: 用户画像。
            tools: 可用工具列表。

        Returns:
            str: 格式化后的 user prompt 字符串。
        """
        return _USER_PROMPT.format(
            intent=intent,
            hints=hints or "（无）",
            user_profile=self._format_user_profile(user_profile),
            tools=format_tools(tools),
            session_messages=self._format_session_messages(session_messages),
        )

    # ── 内部：LLM 调用 ────────────────────────────────────────────────────────

    async def _call_llm_with_usage(self, system: str, user: str) -> tuple[str, dict]:
        """调用 LLM，返回 (响应文本, token_usage dict)。

        token_usage 格式: {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
        usage 为 None 时返回空 dict。

        Args:
            system: 系统 prompt。
            user: 用户 prompt。

        Returns:
            tuple: (响应文本, token_usage dict)。

        Raises:
            _PrepareParseError: LLM 返回空响应。
        """
        response = await self._client.chat.completions.create(
            model=os.getenv("TASK_SYSTEM_MODEL", "deepseek-v4-pro"),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
            max_tokens=4096,
        )
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
        content = response.choices[0].message.content
        if not content:
            raise _PrepareParseError("LLM 返回空响应")
        return content, usage

    # ── 内部：解析 ────────────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> dict:
        """解析 LLM 返回的 JSON → dict。

        Args:
            raw: LLM 返回的原始文本。

        Returns:
            dict: 解析后的 JSON 对象。

        Raises:
            _PrepareParseError: JSON 不合法或缺少必要字段。
        """
        text = strip_markdown_fence(raw)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise _PrepareParseError(f"JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise _PrepareParseError(f"期望 JSON object，收到 {type(data).__name__}")

        # 校验必要字段
        status = data.get("status", "")
        if status not in ("ok", "no_matching_tools", "too_vague"):
            raise _PrepareParseError(f"status 无效: {status!r}（期望 ok/no_matching_tools/too_vague）")

        return data

    # ── 内部：校验 ────────────────────────────────────────────────────────────

    def _validate_output(self, parsed: dict, tools: list[Tool]) -> dict:
        """后处理校验——剔除 LLM 幻觉出的不存在的工具名。

        规则：
        1. 将 LLM 返回的 tools 与输入 tools 做交集（按 name 匹配）
        2. 剔除幻觉工具名，记录 warning
        3. 如果 status=ok 但剔除后 tools 为空 → 降级为 no_matching_tools
        4. 如果 status=no_matching_tools 但 tools 非空 → 认为 LLM 判断有误，保持原判

        Args:
            parsed: LLM 解析后的 dict。
            tools: 实际可用的工具列表。

        Returns:
            dict: 修正后的 parsed dict（原地修改 + 返回）。
        """
        valid_names = {t.name for t in tools}
        llm_tools: list[str] = parsed.get("tools", [])
        if not isinstance(llm_tools, list):
            llm_tools = []

        # 交集——只保留真实存在的工具
        matched = [t for t in llm_tools if t in valid_names]
        hallucinated = [t for t in llm_tools if t not in valid_names]

        if hallucinated:
            logger.warning(
                "DelegationPreparer._validate_output: LLM 幻觉工具名 %s，已剔除",
                hallucinated,
            )

        parsed["tools"] = matched

        # 降级检查：status=ok 但没有匹配工具 → no_matching_tools
        if parsed.get("status") == "ok" and not matched:
            logger.warning(
                "DelegationPreparer._validate_output: status=ok 但筛选后工具为空，"
                "降级为 no_matching_tools"
            )
            parsed["status"] = "no_matching_tools"
            if hallucinated:
                parsed["missing_tools"] = hallucinated

        return parsed

    # ── 内部：构建结果 ────────────────────────────────────────────────────────

    @staticmethod
    def _build_result(
        parsed: dict,
        intent: str,
        hints: str | None,
        tools: list[Tool],
        session_id: SessionId,
    ) -> DelegationResult:
        """从校验后的 parsed dict 构建 DelegationResult。

        工具匹配：从输入 tools 中查找 LLM 选中的工具名，
        保留完整的 Tool 对象（含 description + capabilities），
        以便 Planner 后续注入 prompt。

        Args:
            parsed: 校验后的 LLM 输出 dict。
            intent: 任务意图文本。
            hints: 可选的提示信息。
            tools: 可用工具列表。
            session_id: 会话 ID。

        Returns:
            DelegationResult: status=ok/no_matching_tools/too_vague 的结果。
        """
        status = DelegationStatus(parsed["status"])
        reason = parsed.get("reason", "")
        tool_map = {t.name: t for t in tools}

        if status == DelegationStatus.OK:
            selected_names: list[str] = parsed.get("tools", [])
            # 从 tool_map 查找完整 Tool 对象（_validate_output 已保证 name 有效）
            selected_tools = [tool_map[n] for n in selected_names if n in tool_map]
            if not selected_tools:
                # 防御：_validate_output 保证此分支不会进入，但保留 fallback
                logger.error(
                    "DelegationPreparer._build_result: status=ok 但无有效工具，"
                    "这不应该发生——_validate_output 未正确降级"
                )
                return DelegationResult(
                    status=DelegationStatus.NO_MATCHING_TOOLS,
                    reason="工具筛选后无有效工具",
                    missing_tools=parsed.get("missing_tools", []),
                )

            context = DelegationContext(
                intent=intent,
                hints=hints,
                tools=selected_tools,
                task_constraints=TaskConstraints(
                    max_dag_nodes=parsed.get("max_dag_nodes"),
                    max_mutations=parsed.get("max_mutations"),
                    max_total_tokens=parsed.get("max_total_tokens"),
                    max_task_duration_s=parsed.get("max_task_duration_s"),
                    max_failure_count=parsed.get("max_failure_count"),
                ),
                summarized_context=parsed.get("summarized_context", ""),
                session_id=session_id,
                return_mode=ReturnMode.NATURAL_LANGUAGE,
            )
            return DelegationResult(status=status, context=context, reason=reason)

        elif status == DelegationStatus.NO_MATCHING_TOOLS:
            return DelegationResult(
                status=status,
                reason=reason,
                missing_tools=parsed.get("missing_tools", []),
            )

        else:  # TOO_VAGUE
            return DelegationResult(
                status=status,
                reason=reason,
                suggestion=parsed.get("suggestion"),
            )

    @staticmethod
    def _format_constraints(constraints: ConstraintConfig) -> str:
        """将全局约束格式化为 LLM prompt，让 LLM 知晓天花板。

        LLM 估算的 task_constraints 不应超过这些值——
        即便超过了，TaskConstraints.resolve() 也会 clamp，
        但提前告知 LLM 可以避免浪费思考。

        Args:
            constraints: 全局约束配置。

        Returns:
            str: 格式化后的约束文本。
        """
        return (
            f"## 全局约束（天花板，你的估算不应超过这些值）\n"
            f"- DAG 节点数上限: {constraints.max_dag_nodes}\n"
            f"- 敏感操作上限: {constraints.max_mutations}\n"
            f"- 总 token 上限: {constraints.max_total_tokens}\n"
            f"- 任务时长上限: {constraints.max_task_duration_s}s\n"
            f"- 连续失败上限: {constraints.max_failure_count}\n"
            f"- 单节点重试上限: {constraints.max_retries_per_node}"
        )

    # ── 内部：格式化 ──────────────────────────────────────────────────────────


    @staticmethod
    def _format_session_messages(messages: list[Message]) -> str:
        """将会话消息列表格式化为 LLM prompt 可读的文本。

        Args:
            messages: 会话消息列表。

        Returns:
            str: 格式化的对话历史文本，空列表返回"(无对话历史)"。
        """
        if not messages:
            return "（无对话历史）"

        lines: list[str] = []
        for i, m in enumerate(messages):
            role = m.role
            content_parts = m.content
            # content 是 list[Part]
            text_parts = [
                p.content
                for p in content_parts
                if isinstance(p, TextPart)
            ]
            content = " ".join(text_parts) if text_parts else "(非文本内容)"
            # 截断过长的单条消息（保留上下文但防止 prompt 爆炸）
            if len(content) > 2000:
                content = content[:2000] + "...[截断]"
            lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    @staticmethod
    def _format_user_profile(profile: UserProfile) -> str:
        """v1: UserProfile 为占位符，直接序列化为 JSON 字符串。

        TODO: 后续 UserProfile 定义正式字段后，改为结构化格式化。

        Args:
            profile: 用户画像对象。

        Returns:
            str: JSON 格式化的用户画像字符串，空返回"(无用户画像)"。
        """
        dumped = profile.model_dump(exclude_none=True)
        if not dumped:
            return "（无用户画像）"
        return json.dumps(dumped, ensure_ascii=False, indent=2)


# ── 内部异常 ────────────────────────────────────────────────────────────────────

class _PrepareParseError(Exception):
    """LLM 输出的 JSON 无法解析为 DelegationResult。"""
