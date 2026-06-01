"""L2 长期记忆工具 — 搜索、保存。"""

from Module.tool import ToolContext, tool


@tool(description="在长期记忆中搜索相关信息，比如计划、偏好、历史对话等", requires=["database:read"])
async def search_memory(query: str, limit: int = 5, ctx: ToolContext = None) -> str:
    """在长期记忆中搜索。

    :param query: 搜索关键词
    :param limit: 返回的最大结果数
    """
    memory = ctx.get("memory") if ctx else None
    if memory is None:
        return "（记忆系统不可用）"
    results = await memory.search(query_text=query, top_k=limit)
    if not results:
        return "（记忆库中没有找到相关内容）"
    lines = []
    for r in results:
        meta_str = f" (id={r.id}, type={r.memory_type}, poignancy={r.poignancy})"
        lines.append(f"- {r.text}{meta_str}")
    return "\n".join(lines)


@tool(description="将重要信息保存到长期记忆", side_effects=True, requires=["database:write"])
async def save_memory(content: str, type: str = "fact", ctx: ToolContext = None) -> str:
    """保存新条目到长期记忆。

    :param content: 要保存的内容
    :param type: 记忆类型（fact/plan/preference/note）
    """
    memory = ctx.get("memory") if ctx else None
    if memory is None:
        return "（记忆系统不可用，保存失败）"
    await memory.insert(content, type=type)
    return f"已保存: {content}"
