"""Cognitive 工具执行 — _execute_tools_directly + _handle_submit_task。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from core.adapter import ToolResult, ErrorType
from ..schema.tool import ToolCall
from ..utils import new_message_id

if TYPE_CHECKING:
    from Module.workflow_contract import SubmitError
    from Module.workflow_engine.delegation_preparer import DelegationPreparer
    from core.cognitive.user_profile import UserProfile
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from ..adapter.tool_adapter import ToolAdapter
    from ..memory.session import SessionMemory

logger = logging.getLogger(__name__)


def _default_user_profile() -> "UserProfile":
    """返回默认 UserProfile（v1: 空实例，后续由 CognitiveLoop 填充偏好/语言等）。

    Returns:
        空的 UserProfile 实例。
    """
    from core.cognitive.user_profile import UserProfile
    return UserProfile()


class CognitiveTools:
    """工具执行 mixin — 工具直接执行 + submit_task 后台任务。

    所有属性由 CognitiveLoop.__init__ 初始化，本 mixin 不含 __init__。
    """
    # 类型声明 — 属性由 CognitiveLoop 初始化（mixin 无 __init__）
    _tool_adapter: ToolAdapter | None = None  # CognitiveLoop.__init__ 注入 + configure() 补充
    _session: SessionMemory  # CognitiveLoop.__init__ 注入（必需参数）
    _delegation_preparer: DelegationPreparer | None = None
    _user_profile: UserProfile | None = None
    _task_system: WorkflowEngine | None = None

    # ---- Tool execution ----

    async def _execute_tools_directly(
        self, normalized: list[ToolCall],
    ) -> list:
        """直接执行工具调用 — 向后兼容，不经过 Scheduler Worker Pool。

        每个工具在独立的 asyncio.Task 中并行执行，
        CancelledError 由 asyncio.gather 的 return_exceptions 捕获。

        Args:
            normalized: 标准化后的 ToolCall 列表。

        Returns:
            list[tuple[ToolCall, ToolResult]] — 每个工具调用与其结果的配对列表。
        """
        async def _execute_one(tc: ToolCall):
            func_name = tc.function.name
            t_tool = time.perf_counter()
            args = tc.function.parsed
            logger.info("工具开始: %s(%s)", func_name, {k: str(v)[:60] for k, v in args.items()})
            try:
                if self._tool_adapter is None:
                    raise RuntimeError("ToolAdapter 未初始化")
                tr = await self._tool_adapter.execute(func_name, ctx=self._tool_adapter.tool_context, **args)
            except asyncio.CancelledError:
                logger.info("工具取消: %s", func_name)
                tr = ToolResult.fail(name=func_name, error="用户取消")
            except Exception as e:
                logger.warning("工具失败: %s — %s", func_name, e)
                tr = ToolResult.fail(name=func_name, error=str(e))
            t_tool_elapsed = (time.perf_counter() - t_tool) * 1000
            logger.info("工具完成: %s (%s) — %d ms",
                        func_name, "OK" if tr.success else "FAIL", int(t_tool_elapsed))
            return tc, tr

        return await asyncio.gather(
            *[_execute_one(tc) for tc in normalized],
            return_exceptions=True,
        )

    def _gate_tool_calls(
        self,
        tool_calls: list[ToolCall],
        messages: list[dict],
        blocked_tools: set[str] | None = None,
    ) -> bool:
        """校验 LLM 产出的 tool_calls。全部通过返回 True。
        任一被拒 → 注入 GATE_REJECTED tool messages 到 messages，返回 False。

        submit_task 被跳过——它是合成工具，有自己的 _handle_submit_task 校验。

        Args:
            tool_calls: LLM 产出的 ToolCall 列表。
            messages: 当前消息列表（校验失败时会被原地追加 tool messages）。
            blocked_tools: 已禁用的工具名集合，其中的工具调用会被拒绝。

        Returns:
            True 表示全部通过，False 表示有工具被拒绝。
        """
        # 1. 拆分：submit_task 单独放行
        gate_tcs = [tc for tc in tool_calls if tc.function.name != "submit_task"]

        # 2. 构建已知工具名集合
        known_names: set[str] = set()
        if self._tool_adapter is not None:
            for d in self._tool_adapter.definitions():
                name = d.get("function", {}).get("name", "")
                if name:
                    known_names.add(name)

        # 3. 逐项校验
        rejected = False
        rejected_ids: set[str] = set()
        for tc in gate_tcs:
            name = tc.function.name
            reason: str | None = None
            if name not in known_names:
                reason = f"未知工具: {name}"
            elif blocked_tools and name in blocked_tools:
                reason = f"工具已禁用: {name}"

            if reason:
                rejected = True
                rejected_ids.add(tc.id)
                tr = ToolResult.fail(
                    name=name, error=reason,
                    error_type=ErrorType.GATE_REJECTED,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tr.llm_content,
                })

        # 4. 整轮连坐：合法工具也注入失败消息
        if rejected:
            for tc in gate_tcs:
                if tc.id in rejected_ids:
                    continue
                tr = ToolResult.fail(
                    name=tc.function.name,
                    error="本轮其他工具校验失败，整轮取消",
                    error_type=ErrorType.GATE_REJECTED,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tr.llm_content,
                })
            return False

        return True

    # ---- submit_task 拦截框架 ----

    @staticmethod
    def _submit_task_definition() -> dict:
        """submit_task 工具的 function definition。

        不注册为 ToolPlugin——只在 tool_defs 中合并，LLM 可调用，
        但 ToolAdapter 永远不执行它（CognitiveLoop 拦截处理）。

        Returns:
            OpenAI 格式的 function definition dict。
        """
        return {
            "type": "function",
            "function": {
                "name": "submit_task",
                "description": (
                    "将复杂多步骤任务提交到后台异步执行。"
                    "后台 WorkflowEngine 有自己的 LLM，会自动规划、搜索、执行所有步骤。"
                    "你不需要在调用前做任何准备工作（如搜索、分析）——直接提交即可。"
                    "适用：涉及多步操作、状态变更、或长时间运行的任务。"
                    "简单单步操作（如单次搜索、单次文件读取）请直接用对应工具。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "完整任务意图描述，包含目标和约束",
                        },
                        "hints": {
                            "type": "string",
                            "description": (
                                "对任务执行有帮助的额外信息，"
                                "如用户情绪、紧急程度、偏好等"
                            ),
                        },
                    },
                    "required": ["intent"],
                },
            },
        }

    async def _handle_submit_task(self, tc: ToolCall) -> ToolResult:
        """处理 submit_task function call。

        校验 intent → DelegationPreparer.prepare() → WorkflowEngine.submit()。
        返回 ToolResult——LLM 在下一轮看到成功/失败。

        Args:
            tc: submit_task 的 ToolCall，其 function.parsed 包含 intent 和 hints。

        Returns:
            操作结果的 ToolResult，成功时携带 async_task_id。
        """
        from Module.workflow_contract import SubmitError

        args = tc.function.parsed
        intent = (args.get("intent") or "").strip()

        # 1. 校验
        if not intent:
            return ToolResult.fail(name="submit_task",
                error="intent 不能为空，请修正参数后重试。")
        if len(intent) > 10000:
            return ToolResult.fail(name="submit_task",
                error="intent 过长（超过 10000 字符），请缩短描述后重试。")

        hints = (args.get("hints") or "").strip() or None

        # 2. DelegationPreparer + WorkflowEngine guard
        if self._delegation_preparer is None:
            return ToolResult.fail(name="submit_task",
                error="DelegationPreparer 未初始化，请告知用户后台任务系统暂不可用。")
        task_system = self._task_system
        if task_system is None:
            return ToolResult.fail(name="submit_task",
                error="WorkflowEngine 未初始化，请告知用户后台任务系统暂不可用。")

        # 将 ToolExecutor.definitions() 的 dict 格式转换为 DelegationPreparer 期望的 Tool 列表
        from Module.tool.tool_descriptor import Tool
        tool_defs = self._tool_adapter.definitions() if self._tool_adapter else []
        tool_list = [
            Tool(
                name=d["function"]["name"],
                description=d["function"].get("description", ""),
                capabilities=[],
            )
            for d in tool_defs
        ]

        from Module.workflow_contract import SessionId as _SessionId

        prep_result = await self._delegation_preparer.prepare(
            intent=intent,
            hints=hints,
            session_messages=self._session.recent_messages(),
            user_profile=self._user_profile or _default_user_profile(),
            tools=tool_list,
            global_constraints=task_system.constraints,
            session_id=_SessionId(getattr(self._session, "_session_id", "default")),
        )

        if prep_result.status.value == "no_matching_tools":
            missing = getattr(prep_result, 'missing_tools', [])
            missing_str = ", ".join(missing) if missing else "未知"
            return ToolResult.fail(name="submit_task",
                error=f"缺少能力: {missing_str}。请直接告知用户当前不支持此功能，不要重复尝试。")
        if prep_result.status.value == "too_vague":
            suggestion = getattr(prep_result, 'suggestion', '') or '能再具体一点吗？'
            return ToolResult.fail(name="submit_task",
                error=f"意图不够明确: {suggestion}。请向用户澄清，不要重复尝试。")

        # 3. WorkflowEngine.submit
        if prep_result.context is None:
            raise RuntimeError("submit_task: DelegationPreparer 返回 ok 但 context 为空")
        mailbox_or_error = await task_system.submit(prep_result.context)
        if isinstance(mailbox_or_error, SubmitError):
            return ToolResult.fail(name="submit_task",
                error=f"任务提交失败: {mailbox_or_error.message}。请告知用户具体原因。")

        # 4. 成功
        return ToolResult.ok(
            name="submit_task",
            content="好的，已经在后台执行了。",
            duration_ms=0.0,
            async_task_id=str(mailbox_or_error.task_id),
        )
