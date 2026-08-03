"""Debug overview routes: aggregated info + database status。

响应构造 `frontend_contract.debug` 契约模型 + `wire_dict()`；内部 DebugInfo dataclass
经 `asdict` → 契约视图（session_recent 的 tool 消息 content 为 str → 归一为 DebugContentPart 列表）。
"""

import logging

from aiohttp import web

from core.debug.info import collect, asdict
from frontend_contract import debug as contract

logger = logging.getLogger(__name__)


def register_overview_routes(app: web.Application) -> None:
    """注册 overview 路由。"""
    app.router.add_get("/debug/api", debug_api_handler)
    app.router.add_get("/debug/api/db-status", db_status_handler)


def _normalize_session_recent(items: list[dict]) -> list[dict]:
    """session_recent 内部 dict → 契约 DebugContentPart 形状。

    tool 消息 content 为 str（sessions_to_client 平铺），契约 DebugContentPart 建模为列表——
    发送点归一为单元素 text 块，保证 model_validate 通过且前端 v-for 不遍历字符串字符。

    Args:
        items: collect() 产出的 session_recent 原始 dict 列表。

    Returns:
        list[dict]: content 恒为 DebugContentPart dict 列表的条目列表。
    """
    for item in items:
        c = item.get("content")
        if isinstance(c, str):
            item["content"] = [{"type": "text", "content": c}] if c else []
        elif c is None:
            item["content"] = []
    return items


async def debug_api_handler(request: web.Request) -> web.Response:
    """Return aggregated debug info for the overview dashboard.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: DebugData 契约 wire（16 面板，system/memory/scheduler 等）。
    """
    info = await collect(request.app["agent"])
    wire = asdict(info)
    _normalize_session_recent(wire.get("session_recent", []))
    data = contract.DebugData.model_validate(wire)
    return web.json_response(data.wire_dict())


async def db_status_handler(request: web.Request) -> web.Response:
    """Return database status (LTM count, session count, archive count, health checks).

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: DbStatus 契约 wire（ltm/session/archive 三库状态）。
    """
    agent = request.app["agent"]
    ltm = agent.long_term
    data = contract.DbStatus(
        ltm=contract.DbStatusLtm(
            count=ltm.count,
            milvus_ok=await agent.memory_repo.health_check(),
            dirty_count=len(ltm._dirty_ids),  # noqa: SLF001
        ),
        session=contract.DbStatusSession(
            count=1,  # 当前只有单个 session
            current_id=str(agent.session.session_id),
            mongodb_ok=await agent.session_repo.health_check(),
        ),
        archive=contract.DbStatusArchive(
            count=await agent.message_repo.count(),
            mongodb_ok=await agent.message_repo.health_check(),
        ),
    )
    return web.json_response(data.wire_dict())
