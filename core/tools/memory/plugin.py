"""记忆工具插件 — 搜索和保存长期记忆。"""

import logging
from typing import Any

from pydantic import BaseModel, Field
from Module._common.error_type import ErrorType
from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter, tool_failure, ToolResult
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile
from core.schema.memory import MemoryType

logger = logging.getLogger(__name__)


class SearchMemoryParams(BaseModel):
    query: str = Field(..., description="搜索关键词或语义描述")
    limit: int = Field(default=5, ge=1, le=20, description="返回结果数量，最大20")


class MemorySearchPlugin(ToolPlugin):
    name = "search_memory"
    description = "在长期记忆中搜索相关信息，比如计划、偏好、历史对话等"
    param_model = SearchMemoryParams
    capabilities = {Capability.DATABASE_READ}
    safety = SafetyProfile(
        database_allowed=True,
        database_readonly=True,
        idempotent=True,  # 只读查询：超时重试安全（2026-09-16 幂等声明）
    )

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: SearchMemoryParams,
    ) -> str | ToolResult:
        """在长期记忆中搜索相关信息。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 搜索参数，包含查询文本和结果数量上限。

        Returns:
            搜索结果字符串，或错误信息。
        """
        memory = ctx.get("memory")
        if memory is None:
            return tool_failure("记忆系统当前不可用。", error_type=ErrorType.UNAVAILABLE)

        try:
            results = await memory.search(
                query_text=params.query, top_k=min(params.limit, 20),
            )
        except Exception as e:
            logger.warning("search_memory 失败: %s", e)
            return tool_failure(f"搜索记忆时出错: {e}", error_type=ErrorType.TOOL_ERROR)

        if not results:
            return "未找到相关记忆。"

        lines = []
        for r in results:
            entry_id = getattr(r, "id", "?")
            entry_type = getattr(r, "memory_type", "unknown")
            poignancy = getattr(r, "poignancy", 0)
            text = getattr(r, "text", str(r))
            lines.append(
                f"[ID:{entry_id}] (类型:{entry_type}, 重要性:{poignancy}) {text}"
            )
        return "\n".join(lines)


class SaveMemoryParams(BaseModel):
    content: str = Field(..., description="要保存的记忆内容")
    type: str = Field(
        default="fact",
        description="记忆类型: fact(事实), preference(偏好), event(事件), plan(计划), note(笔记)",
    )


class MemorySavePlugin(ToolPlugin):
    name = "save_memory"
    description = "将重要信息保存到长期记忆库以供未来检索"
    param_model = SaveMemoryParams
    capabilities = {Capability.DATABASE_WRITE}
    risk_level = "medium"  # 写长期记忆（§3.3）
    safety = SafetyProfile(
        database_allowed=True,
        database_readonly=False,
        requires_human_approval=True,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: SaveMemoryParams,
    ) -> str | ToolResult:
        """将重要信息保存到长期记忆库。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 保存参数，包含记忆内容和类型。

        Returns:
            保存结果字符串。
        """
        # 用 MemoryType.try_coerce 统一归一入口（与 schema 一致，替代手写 .title() 校验）
        mem_type = MemoryType.try_coerce(params.type)
        if mem_type is None:
            allowed = ", ".join(t.value for t in MemoryType)
            return tool_failure(
                f"无效的记忆类型 '{params.type}'，允许: {allowed}",
                error_type=ErrorType.INVALID_ARGUMENT)

        memory = ctx.get("memory")
        if memory is None:
            return tool_failure("记忆系统当前不可用。", error_type=ErrorType.UNAVAILABLE)

        content = params.content
        try:
            await memory.insert(content, type=mem_type.value)
        except Exception as e:
            logger.warning("save_memory 失败: %s", e)
            return tool_failure(f"保存记忆时出错: {e}", error_type=ErrorType.TOOL_ERROR)

        return f"已保存 {mem_type.value} 类型记忆: {content[:200]}"
