"""Debug character 路由：L2 / C6 版本历史 + 回滚。

响应构造 `frontend_contract.debug` 契约模型 + `wire_dict()`；内部 VersionEntry
(TypedDict，trigger 已为 CompressionTrigger.value 字符串) → 契约
`CharacterVersionListResponse` 视图；回滚 → `RollbackResult` 契约视图。
"""

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from core.input.errors import _json_object

from core.input.debug_routes._shared import _protocol_error_response
from core.schema.utils import now_iso
from core.schema.compression import CompressionTrigger
from frontend_contract import debug as contract

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


def register_character_routes(app: web.Application) -> None:
    """注册 Character 版本历史路由。"""
    app.router.add_get("/debug/api/l2-versions", l2_versions_handler)
    app.router.add_post("/debug/api/l2-rollback", l2_rollback_handler)
    app.router.add_get("/debug/api/character-versions", character_versions_handler)
    app.router.add_post("/debug/api/character-rollback", character_rollback_handler)


async def _list_character_versions(agent: "Agent", content_type: str,
                                    limit: int, before_version: int | None):
    """列出当前会话的角色版本历史（L2 或 C6）。

    Args:
        agent: 带 character engine 的 Agent 实例。
        content_type: "l2" 或 "c6"。
        limit: 最多返回的版本数量。
        before_version: 可选的上界版本号（不包含）。

    Returns:
        tuple: (CharacterVersionListResponse | None, error_str | None, error_code | None, http_status_code)
    """
    # Task 12（§6.5 读端迁移）：_list_character_versions 经 agent.character_engine
    # 解析当前会话绑定 instance 的 engine（非全局单例）——Task 11 后该 property
    # 经 registry.get(binding.instance_id).ensure_engine() 解析；版本历史键 `sid`
    # 仍为 state.session_id（§5.1 保留作版本键）。
    engine = getattr(agent, "character_engine", None)
    if not engine or not engine.version_repo:
        return None, "版本历史不可用", "STORAGE_UNAVAILABLE", 503
    sid = agent.session.session_id
    versions = await engine.version_repo.list_versions(
        sid, limit=limit, before_version=before_version,
        content_type=content_type,
    )
    total = await engine.version_repo.count_versions(sid, content_type=content_type)
    result = contract.CharacterVersionListResponse(
        versions=versions,
        total=total,
        has_more=len(versions) == limit and versions[-1]["version"] > 1,
    )
    return result, None, "", 200


async def _character_rollback(agent: "Agent", content_type: str, target_version: int):
    """将角色内容（L2 或 C6）回滚到指定版本。

    Args:
        agent: 带 character engine 的 Agent 实例。
        content_type: "l2" 或 "c6"。
        target_version: 要回滚到的版本号。

    Returns:
        tuple: (RollbackResult | None, error_str | None, error_code | None, http_status_code)
    """
    engine = getattr(agent, "character_engine", None)
    if not engine or not engine.version_repo:
        return None, "版本历史不可用", "STORAGE_UNAVAILABLE", 503
    sid = agent.session.session_id
    entry = await engine.version_repo.get_version(sid, target_version,
                                                    content_type=content_type)
    if entry is None:
        return None, f"版本 {target_version} 不存在", "RESOURCE_NOT_FOUND", 404

    # 恢复文本
    if content_type == "l2":
        engine.state.self_l2 = entry["text"]
    elif content_type == "c6":
        engine.state.speaking_style_c6 = entry["text"]

    new_version = await engine.version_repo.append_version(
        session_id=sid, text=entry["text"], timestamp=now_iso(),
        trigger=CompressionTrigger.MANUAL,
        change_summary=[f"回滚到版本 {target_version}"],
        content_type=content_type,
    )

    # L2 rollback 后触发 C6 刷新
    if content_type == "l2":
        try:
            await engine._refresh_c6()  # noqa: SLF001
        except Exception:
            logger.warning("L2 回滚后 C6 刷新失败", exc_info=True)

    result = contract.RollbackResult(
        status="ok", new_version=new_version, text=entry["text"])
    return result, None, "", 200


async def l2_versions_handler(request: web.Request) -> web.Response:
    """列出 L2 自我形象版本历史（legacy 路由）。

    Args:
        request: aiohttp Request, query params: limit, before。

    Returns:
        web.Response: CharacterVersionListResponse 契约 wire（versions/total/has_more）。
    """
    agent = request.app["agent"]
    try:
        limit = int(request.query.get("limit", "20"))
        before = request.query.get("before")
        before_version = int(before) if before else None
    except ValueError:
        return _protocol_error_response("CONTRACT_VALIDATION", "limit/before 必须是整数", 400)
    result, error, code, status = await _list_character_versions(
        agent, "l2", limit, before_version)
    if error:
        return _protocol_error_response(code, error, status,
                            retryable=(code == "STORAGE_UNAVAILABLE"))
    if result is None:
        return _protocol_error_response("INTERNAL", "内部错误", 500)
    return web.json_response(result.wire_dict())


async def l2_rollback_handler(request: web.Request) -> web.Response:
    """将 L2 自我形象回滚到指定版本（legacy 路由）。

    Args:
        request: aiohttp Request, JSON body: version。

    Returns:
        web.Response: RollbackResult 契约 wire（status/new_version/text）或错误。
    """
    agent = request.app["agent"]
    try:
        body = await _json_object(request)
        target_version = int(body["version"])
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except (ValueError, KeyError, TypeError):
        return _protocol_error_response("CONTRACT_VALIDATION", "缺少 version 或格式错误", 400)
    result, error, code, status = await _character_rollback(
        agent, "l2", target_version)
    if error:
        return _protocol_error_response(code, error, status,
                            retryable=(code == "STORAGE_UNAVAILABLE"))
    if result is None:
        return _protocol_error_response("INTERNAL", "内部错误", 500)
    logger.warning("DEBUG API: l2_rollback version=%d, new_version=%s",
                  target_version, result.new_version)
    return web.json_response(result.wire_dict())


async def character_versions_handler(request: web.Request) -> web.Response:
    """列出角色版本历史（L2 或 C6）。

    Args:
        request: aiohttp Request, query params: type (l2/c6), limit, before。

    Returns:
        web.Response: CharacterVersionListResponse 契约 wire（versions/total/has_more）。
    """
    agent = request.app["agent"]
    content_type = request.query.get("type", "l2")
    if content_type not in ("l2", "c6"):
        return _protocol_error_response("CONTRACT_VALIDATION", "type 必须为 l2 或 c6", 400)
    try:
        limit = int(request.query.get("limit", "20"))
        before = request.query.get("before")
        before_version = int(before) if before else None
    except ValueError:
        return _protocol_error_response("CONTRACT_VALIDATION", "limit/before 必须是整数", 400)
    result, error, code, status = await _list_character_versions(
        agent, content_type, limit, before_version)
    if error:
        return _protocol_error_response(code, error, status,
                            retryable=(code == "STORAGE_UNAVAILABLE"))
    if result is None:
        return _protocol_error_response("INTERNAL", "内部错误", 500)
    return web.json_response(result.wire_dict())


async def character_rollback_handler(request: web.Request) -> web.Response:
    """将角色内容（L2 或 C6）回滚到指定版本。

    Args:
        request: aiohttp Request, JSON body: type (l2/c6), version。

    Returns:
        web.Response: RollbackResult 契约 wire（status/new_version/text）或错误。
    """
    agent = request.app["agent"]
    try:
        body = await _json_object(request)
        content_type = body["type"]
        target_version = int(body["version"])
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except (ValueError, KeyError, TypeError):
        return _protocol_error_response("CONTRACT_VALIDATION", "缺少 type/version 或格式错误", 400)
    if content_type not in ("l2", "c6"):
        return _protocol_error_response("CONTRACT_VALIDATION", "type 必须为 l2 或 c6", 400)
    result, error, code, status = await _character_rollback(
        agent, content_type, target_version)
    if error:
        return _protocol_error_response(code, error, status,
                            retryable=(code == "STORAGE_UNAVAILABLE"))
    if result is None:
        return _protocol_error_response("INTERNAL", "内部错误", 500)
    logger.warning("DEBUG API: character_rollback type=%s, version=%d, new_version=%s",
                  content_type, target_version, result.new_version)
    return web.json_response(result.wire_dict())
