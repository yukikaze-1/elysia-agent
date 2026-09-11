"""CognitiveToolPhase mixin — 工具轮执行阶段。

CORE-MOD-P1-04：自 core/cognitive/loop.py 抽出。继承 CognitiveTools 以获得
工具 gate / submit_task / 直接执行能力，CognitiveLoop 不再直接继承 CognitiveTools。
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Callable

from Module.tool.protocol import ToolResult
from Module.tool.safety import summarize_tool_arguments
from Module.workflow_contract import WorkflowId
from core.adapter.tool_call_parser import normalize_tool_calls
from core.cognitive.tools import CognitiveTools
from core.schema.message import AssistantMessage, ToolResultMessage
from core.schema.part import ImageMime, ImagePart, Part, TextPart
from core.schema.tool import ToolCall
from core.schema.types import SessionId
from core.utils import new_message_id

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from Module.output import OutputManager
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.adapter.tool_adapter import ToolAdapter
    from core.cognitive.delegation import DelegationPreparer
    from core.cognitive.generation import GenerationContext
    from core.cognitive.timing import TimingClock
    from core.memory.session import SessionMemory
    from core.schema.message import CharacterMessageSnapshot
    from core.status.fsm import FSMBackedPhaseController


def _build_tool_result_parts(tool_result: ToolResult) -> list[Part]:
    """组装 ToolResultMessage.content：文本摘要 + 图片 ImagePart（mime 白名单过滤）。

    ImagePart.mime 兜底：mime_type 命中 ImageMime 枚举 → 原样；空/非图片 → 过滤
    不落 ImagePart——否则 ImagePart 构造抛 ValidationError 拖垮整轮工具结果。
    """
    parts: list[Part] = [
        TextPart(content=tool_result.llm_summary or tool_result.content or ""),
    ]
    for item in tool_result.images:
        if not item.url:
            continue
        try:
            mime = ImageMime(item.mime_type)
        except ValueError:
            continue
        parts.append(ImagePart(src=item.url, mime=mime))
    return parts


def _truncate_output_summary(content: str | None, max_len: int = 2000) -> tuple[str | None, bool]:
    """outputSummary 截断（§7-1）：≤ max_len 原样；超长截断 + truncated=True。

    Args:
        content: 工具结果原始内容（可 None）。
        max_len: 截断上限（字符，默认 2000）。

    Returns:
        (output_summary, truncated)——output_summary 为 None 时 truncated 恒 False。
        截断后总长 ≤ max_len（`content[:max_len - 1] + "…"`，含省略号）。
    """
    if content is None:
        return None, False
    if len(content) <= max_len:
        return content, False
    return content[: max_len - 1] + "…", True


class CognitiveToolPhase(CognitiveTools):
    """工具执行阶段 mixin：submit_task 拦截 → Gate → dispatch → 结果注入。"""

    # 类型占位：运行时由 CognitiveLoop.__init__ / configure() 初始化
    _delegation_preparer: "DelegationPreparer | None"
    _tool_adapter: "ToolAdapter | None"
    _session: "SessionMemory"
    _output: "OutputManager"
    _on_background_task: Callable | None
    _task_system: "WorkflowEngine | None"
    _executing_tools: list[str]
    _clock: "TimingClock | None"
    _generation_ctx: "GenerationContext | None"
    phases: "FSMBackedPhaseController"
    # 跨 mixin 方法（定义于 CognitiveFinalize，运行时由 CognitiveLoop 组合提供）：
    # 工具轮归档 assistant 消息需要与本 mixin 无关的角色身份快照。
    _turn_character_snapshot: Callable[[], "CharacterMessageSnapshot | None"]

    def _prepare_tool_round(
        self,
        raw_tool_calls: list[dict],
        acc_text: str,
        messages: list[dict],
    ) -> tuple[list[ToolCall], list[ToolCall], list[ToolCall], bool]:
        """归一化/名称反查/gate；返回 (normalized, submit_tcs, other_tcs, gate_ok)。"""
        normalized = normalize_tool_calls(raw_tool_calls)

        if self._delegation_preparer is not None:
            submit_tcs = [tc for tc in normalized if tc.function.name == "submit_task"]
            other_tcs = [tc for tc in normalized if tc.function.name != "submit_task"]
        else:
            submit_tcs = []
            other_tcs = normalized

        assistant_entry: dict = {"role": "assistant", "content": acc_text or None}
        if normalized:
            assistant_entry["tool_calls"] = [tc.model_dump(mode="json") for tc in normalized]
        messages.append(assistant_entry)

        if self._tool_adapter is not None:
            resolver = getattr(self._tool_adapter, "resolve_llm_name", None)
            if resolver is not None:
                for tc in normalized:
                    runtime_name = resolver(tc.function.name)
                    if isinstance(runtime_name, str) and runtime_name:
                        tc.function.name = runtime_name

        return normalized, submit_tcs, other_tcs, self._gate_tool_calls(normalized, messages)

    async def _run_submit_tasks(
        self,
        submit_tcs: list[ToolCall],
        messages: list[dict],
        conversation_id: SessionId | None = None,
    ) -> None:
        """submit_task 专用路径：执行、结果注入、后台任务 mailbox 注册。"""
        for tc in submit_tcs:
            parsed = tc.function.parsed if isinstance(tc.function.parsed, dict) else {}
            await self._output.tool_started(
                call_id=tc.id, tool_name="submit_task", runtime="backend",
                v1_args={}, input_summary=summarize_tool_arguments(parsed),
                v1_suppress=True,   # submit_task v1 从未发 tool_executing，帧集不变
                generation_id=(self._generation_ctx.generation_id
                               if self._generation_ctx is not None else None),
                conversation_id=conversation_id,
            )
            result = await self._handle_submit_task(tc)
            tc_id = tc.id
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result.llm_content,
            })
            error_type_str = result.error_type.value if result.error_type else None
            output_summary, output_truncated = _truncate_output_summary(result.content)
            await self._output.tool_completed(
                call_id=tc_id, tool_name="submit_task", runtime="backend",
                success=result.success,
                v1_content=result.content or "",
                output_summary=output_summary,
                v1_truncated=result.truncated,
                output_truncated=output_truncated,
                error_v1=result.error,
                error_v2=({"code": error_type_str or "TOOL_ERROR",
                           "message": result.error or ""}
                          if result.error else None),
                error_type=error_type_str,
                duration_ms=result.duration_ms,
                generation_id=(self._generation_ctx.generation_id
                               if self._generation_ctx is not None else None),
                conversation_id=conversation_id,
            )
            self._session.add_message(ToolResultMessage(
                message_id=new_message_id(),
                content=[TextPart(content=result.content or "")],
                tool_call_id=tc.id,
                tool_name="submit_task",
                success=result.success,
                error=result.error,
                error_type=None,
                duration_ms=result.duration_ms,
                truncated=result.truncated,
            ))
            if result.success and result.async_task_id and self._on_background_task and self._task_system:
                mailbox = self._task_system.get_task_mailbox(WorkflowId(result.async_task_id))
                if mailbox:
                    dispatched_now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    self._on_background_task(
                        result.async_task_id, mailbox, intent=(
                            tc.function.parsed.get("intent", "") if isinstance(
                                tc.function.parsed, dict) else ""
                        ), dispatched_at=dispatched_now,
                    )

    async def _dispatch_tool_calls(
        self,
        dispatch_tcs: list[ToolCall],
        _on_tool_calls: Callable | None,
        messages: list[dict],
        conversation_id: SessionId | None = None,
    ) -> None:
        """普通工具 dispatch、执行、结果注入与 executing 追踪清理。"""
        for tc in dispatch_tcs:
            func_name = tc.function.name
            call_id = tc.id
            args = tc.function.parsed
            if not isinstance(args, dict):
                args = {}
            self._executing_tools.append(func_name)
            await self._output.tool_started(
                call_id=call_id, tool_name=func_name,
                runtime=(self._tool_adapter.runtime_for(func_name)
                         if self._tool_adapter is not None else "backend"),
                v1_args={k: str(v) for k, v in args.items()},
                input_summary=summarize_tool_arguments(args),
                generation_id=(self._generation_ctx.generation_id
                               if self._generation_ctx is not None else None),
                conversation_id=conversation_id,
            )

        if self._clock is None:
            raise RuntimeError("TimingClock 未初始化")
        with self._clock.span("tools"):
            if _on_tool_calls is not None:
                results = await _on_tool_calls(dispatch_tcs)
            else:
                results = await self._execute_tools_directly(dispatch_tcs)

        for item in results:
            if isinstance(item, BaseException):
                logger.warning("工具执行返回异常，跳过该工具结果: %r", item)
                continue
            tc, tool_result = item
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result.llm_content,
            })
            error_type_str = tool_result.error_type.value if tool_result.error_type else None
            output_summary, output_truncated = _truncate_output_summary(tool_result.content)
            await self._output.tool_completed(
                call_id=tc.id, tool_name=tc.function.name,
                runtime=(self._tool_adapter.runtime_for(tc.function.name)
                         if self._tool_adapter is not None else "backend"),
                success=tool_result.success,
                v1_content=tool_result.content or "",
                output_summary=output_summary,
                v1_truncated=tool_result.truncated,
                output_truncated=output_truncated,
                error_v1=tool_result.error,
                error_v2=({"code": error_type_str or "TOOL_ERROR",
                           "message": tool_result.error or ""}
                          if tool_result.error else None),
                error_type=error_type_str,
                duration_ms=tool_result.duration_ms,
                generation_id=(self._generation_ctx.generation_id
                               if self._generation_ctx is not None else None),
                conversation_id=conversation_id,
            )
            self._session.add_message(ToolResultMessage(
                message_id=new_message_id(),
                content=_build_tool_result_parts(tool_result),
                tool_call_id=tc.id,
                tool_name=tc.function.name,
                success=tool_result.success,
                error=tool_result.error,
                error_type=tool_result.error_type,
                duration_ms=tool_result.duration_ms,
                truncated=tool_result.truncated,
            ))

        for tc in dispatch_tcs:
            func_name = tc.function.name
            try:
                self._executing_tools.remove(func_name)
            except ValueError:
                pass

    async def _execute_tool_round(
        self, messages: list[dict], acc_text: str,
        raw_tool_calls: list[dict], _on_tool_calls: Callable | None,
        conversation_id: SessionId | None = None,
    ) -> None:
        """执行一轮工具调用：submit_task 拦截 → Gate 校验 → dispatch → 结果注入。"""
        await self.phases.enter_executing()

        normalized, submit_tcs, other_tcs, gate_ok = self._prepare_tool_round(
            raw_tool_calls, acc_text, messages)
        if not gate_ok:
            await self.phases.return_to_thinking()
            return

        if submit_tcs:
            # B-6-04：submit_task 专用路径透传会话 ID（两帧 conversationId 数据源）
            await self._run_submit_tasks(submit_tcs, messages, conversation_id=conversation_id)
            dispatch_tcs = other_tcs
        else:
            dispatch_tcs = normalized

        self._session.add_message(AssistantMessage(
            content=[TextPart(content=acc_text)] if acc_text else [],
            message_id=new_message_id(),
            tool_calls=normalized if normalized else None,
            # P3-1：工具轮消息同样必须带角色身份快照——此前漏传，导致每条工具轮
            # 都以 character=null 落 message_archive；character_profiles=true 时前端
            # 对消息历史整批 fail-closed（问题单「14 条 assistant 缺角色戳」的源头）。
            # 快照不可得时该方法返回 None（legacy/无 runtime），不阻断工具轮。
            character=self._turn_character_snapshot(),
        ))

        await self._dispatch_tool_calls(
            dispatch_tcs, _on_tool_calls, messages, conversation_id=conversation_id)
        await self.phases.return_to_thinking()
