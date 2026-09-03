"""character_routes — 角色/身份 REST 端点（阶段 D）。

错误码映射对齐架构稿 §14：CHARACTER_NOT_FOUND(404) / CHARACTER_VALIDATION(400) /
CHARACTER_REVISION_CONFLICT(409) / CHARACTER_IMMUTABLE(409) / CHARACTER_ARCHIVED(409) /
CHARACTER_STORAGE_UNAVAILABLE(503) / CHARACTER_TOO_LARGE(413) / USER_PROFILE_REVISION_CONFLICT(409)。
feature flag 恒 false —— custom 不参与真实聊天。
"""

from typing import Callable

from aiohttp import web
from pydantic import ValidationError

from core.character.service import (
    CharacterNotFoundError, CharacterImmutableError, CharacterArchivedError,
    CharacterValidationError, RevisionConflictError, CharacterService,
)
from core.character.draft import CharacterDraft
from core.storage.base import StorageUnavailableError
from core.input.errors import _protocol_error_response
from frontend_contract.rest import UserIdentityProfile


def _character_error_response(exc: Exception) -> web.Response | None:
    """把 Service 异常映射为 ProtocolError 响应；未知返回 None。"""
    if isinstance(exc, CharacterNotFoundError):
        return _protocol_error_response("CHARACTER_NOT_FOUND", "角色不存在", 404)
    if isinstance(exc, CharacterImmutableError):
        return _protocol_error_response("CHARACTER_IMMUTABLE", "内置角色不可编辑", 409)
    if isinstance(exc, CharacterArchivedError):
        return _protocol_error_response("CHARACTER_ARCHIVED", "archived 角色不可操作", 409)
    if isinstance(exc, CharacterValidationError):
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    if isinstance(exc, RevisionConflictError):
        return _protocol_error_response("CHARACTER_REVISION_CONFLICT", "expectedRevision 已过期", 409, retryable=True)
    if isinstance(exc, StorageUnavailableError):
        return _protocol_error_response("CHARACTER_STORAGE_UNAVAILABLE", "角色存储不可用", 503, retryable=True)
    return None


def _draft_from_body(body: dict) -> CharacterDraft:
    """把 wire 请求体（CharacterDraftWidget 形状）转为领域 CharacterDraft。"""
    return CharacterDraft(**body)


def _service(app: web.Application) -> CharacterService:
    return app["character_service"]


async def _me_get(request: web.Request) -> web.Response:
    # 注意：app["user_identity_store"] 是 UserIdentityStore（有 .get()），
    # 不是 bootstrap() 返回的 UserIdentity 对象——后者无 .get()（B7 修复）。
    profile = request.app["user_identity_store"].get()
    if profile is None:
        return _protocol_error_response("USER_PROFILE_NOT_FOUND", "用户身份未初始化", 404)
    return web.json_response(UserIdentityProfile(**profile.model_dump()).wire_dict(), status=200)


async def _me_put(request: web.Request) -> web.Response:
    body = await request.json()
    expected = body.get("expectedRevision")
    store = request.app["user_identity_store"]
    new = store.update_if_revision(int(expected) if expected is not None else 1,
                                   {k: v for k, v in body.items() if k != "expectedRevision"})
    if new is None:
        return _protocol_error_response("USER_PROFILE_REVISION_CONFLICT", "expectedRevision 已过期", 409, retryable=True)
    return web.json_response(UserIdentityProfile(**new.model_dump()).wire_dict(), status=200)


async def _characters_list(request: web.Request) -> web.Response:
    svc = _service(request.app)
    try:
        resp = await svc.list()
        return web.json_response(resp.wire_dict(), status=200)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)


async def _characters_create(request: web.Request) -> web.Response:
    body = await request.json()
    svc = _service(request.app)
    try:
        draft = _draft_from_body(body)
    except ValidationError as e:
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    try:
        detail = await svc.create(draft, owner_user_id="usr_local_default")
        return web.json_response(detail.wire_dict(), status=201)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)


async def _characters_validate(request: web.Request) -> web.Response:
    body = await request.json()
    svc = _service(request.app)
    try:
        draft = _draft_from_body(body)
    except ValidationError as e:
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    result = await svc.validate(draft)
    return web.json_response(result.wire_dict(), status=200)


def register_character_routes(app: web.Application, check_auth: Callable | None = None) -> None:
    """注册角色/身份 REST 路由（对齐 server.py 既有 register_xxx_routes 模式）。"""
    app.router.add_get("/api/me", _me_get)
    app.router.add_put("/api/me", _me_put)
    app.router.add_get("/api/characters", _characters_list)
    app.router.add_post("/api/characters", _characters_create)
    app.router.add_post("/api/characters/validate", _characters_validate)
    # 其余 {id} 路由在 Task 7 补齐（GET/PUT/DELETE/clone/revisions/rollback）
