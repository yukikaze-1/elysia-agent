"""记忆工具插件 — 搜索和保存长期记忆。"""

import logging
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
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
    safety = SafetyProfile(database_allowed=True, database_readonly=True)

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: SearchMemoryParams,
    ) -> str:
        memory = ctx.get("memory")
        if memory is None:
            return "记忆系统当前不可用。"

        try:
            results = await memory.search(
                query_text=params.query, top_k=min(params.limit, 20),
            )
        except Exception as e:
            logger.warning("search_memory 失败: %s", e)
            return f"搜索记忆时出错: {e}"

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
    safety = SafetyProfile(
        database_allowed=True,
        database_readonly=False,
        requires_human_approval=True,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: SaveMemoryParams,
    ) -> str:
        try:
            mem_type = MemoryType(params.type.strip().title())
        except ValueError:
            allowed = ", ".join(t.value for t in MemoryType)
            return f"无效的记忆类型 '{params.type}'，允许: {allowed}"

        memory = ctx.get("memory")
        if memory is None:
            return "记忆系统当前不可用。"

        content = params.content
        try:
            await memory.insert(content, type=mem_type.value)
        except Exception as e:
            logger.warning("save_memory 失败: %s", e)
            return f"保存记忆时出错: {e}"

        return f"已保存 {mem_type.value} 类型记忆: {content[:200]}"
