"""角色/身份 REST 路由（Stage H）。

路由层只负责 HTTP 解析与错误映射；领域校验/CAS 在 CharacterService 中完成。
所有请求体先经过 frontend_contract 的严格模型，避免用 dict/Any 掩盖 wire 漂移。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, TypedDict, cast

from aiohttp import web
from pydantic import ValidationError

from core.character.draft import CharacterDraft
from core.character.service import (
    CharacterArchivedError,
    CharacterBusyError,
    CharacterImmutableError,
    CharacterInstanceNotFoundError,
    CharacterNotFoundError,
    CharacterPreviewUnavailableError,
    CharacterQueryError,
    RESET_FIELDS,
    CharacterService,
    CharacterStateConflictError,
    CharacterStorageUnavailableError,
    CharacterTooLargeError,
    CharacterValidationError,
    RevisionConflictError,
)
from core.character.runtime_loader import CharacterRuntimeSourceError
from core.input.errors import _protocol_error_response
from core.storage.base import StorageUnavailableError
from frontend_contract.rest import (
    CharacterDraft as WireCharacterDraft,
    CharacterPreviewRequest,
    CharacterPreviewResult,
    CharacterPublicState,
    CharacterResetRequest,
    CharacterResetResult,
    CharacterStatePolicy,
    UserIdentityProfile,
    UserIdentityUpdate,
)

if TYPE_CHECKING:
    from core.character.runtime import CharacterRuntime


class _QueryParams(TypedDict):
    source: str | None
    status: str
    search: str | None
    cursor: str | None
    limit: int


def _character_error_response(exc: Exception) -> web.Response | None:
    """把领域异常映射为冻结错误码；未知异常交由调用方映射 500。"""
    if isinstance(exc, CharacterInstanceNotFoundError):
        return _protocol_error_response("CHARACTER_INSTANCE_NOT_FOUND", "角色实例不存在", 404)
    if isinstance(exc, CharacterNotFoundError):
        return _protocol_error_response("CHARACTER_NOT_FOUND", "角色不存在", 404)
    if isinstance(exc, CharacterImmutableError):
        return _protocol_error_response("CHARACTER_IMMUTABLE", "内置角色不可编辑", 409)
    if isinstance(exc, CharacterArchivedError):
        return _protocol_error_response("CHARACTER_ARCHIVED", "archived 角色不可操作", 409)
    if isinstance(exc, CharacterTooLargeError):
        return _protocol_error_response("CHARACTER_TOO_LARGE", "角色内容过大", 413)
    if isinstance(exc, CharacterValidationError):
        # BE-STAGEH-04：顶层 code 恒为 CHARACTER_VALIDATION，具体原因只走
        # details.issues[]（issue.code 可为 CHARACTER_RESERVED_CONTENT 等）。
        # 不再按 issue code 改写顶层——保存信封与 /validate 的 issue shape 一致。
        details: dict[str, object] = {}
        if exc.issues:
            details["issues"] = [issue.wire_dict() for issue in exc.issues]
        return _protocol_error_response(
            "CHARACTER_VALIDATION", "角色内容校验失败", 400, details=details)
    if isinstance(exc, CharacterQueryError):
        return _protocol_error_response("CHARACTER_VALIDATION", "请求参数无效", 400)
    if isinstance(exc, RevisionConflictError):
        details = (
            {}
            if exc.current_revision is None
            else {"currentRevision": exc.current_revision}
        )
        return _protocol_error_response(
            "CHARACTER_REVISION_CONFLICT",
            "expectedRevision 已过期",
            409,
            retryable=True,
            details=details,
            retry_after_ms=100,
        )
    if isinstance(exc, CharacterStateConflictError):
        details = (
            {}
            if exc.current_state_version is None
            else {"currentStateVersion": exc.current_state_version}
        )
        return _protocol_error_response(
            "CHARACTER_STATE_CONFLICT",
            "expectedStateVersion 已过期",
            409,
            retryable=True,
            details=details,
            retry_after_ms=100,
        )
    if isinstance(exc, CharacterBusyError):
        return _protocol_error_response(
            "CHARACTER_BUSY", "角色实例正忙，请稍后重试", 409,
            retryable=True, retry_after_ms=500,
        )
    if isinstance(exc, CharacterPreviewUnavailableError):
        return _protocol_error_response(
            "CHARACTER_PREVIEW_FAILED", "角色预览暂不可用", 503,
            retryable=True, retry_after_ms=1000,
        )
    if isinstance(exc, (CharacterStorageUnavailableError, StorageUnavailableError)):
        return _protocol_error_response(
            "CHARACTER_STORAGE_UNAVAILABLE", "角色存储不可用", 503,
            retryable=True, retry_after_ms=1000,
        )
    return None


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="invalid JSON") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be an object")
    return body


def _draft_from_body(body: dict) -> CharacterDraft:
    """Strict wire draft -> domain draft; no arbitrary extra fields survive."""
    wire = WireCharacterDraft.model_validate(body)
    return CharacterDraft(**wire.model_dump(mode="python"))


def _wire_update_body(body: dict) -> tuple[CharacterDraft, int, str]:
    allowed = {
        "displayName", "description", "aliases", "promptProfile",
        "runtimeDefaults", "presentation", "openingMessage",
        "expectedRevision", "statePolicy",
    }
    if set(body) - allowed:
        raise ValueError("unknown update fields")
    if "expectedRevision" not in body or "statePolicy" not in body:
        raise ValueError("expectedRevision/statePolicy required")
    expected = body["expectedRevision"]
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise ValueError("expectedRevision invalid")
    policy = body["statePolicy"]
    if policy not in {item.value for item in CharacterStatePolicy}:
        raise ValueError("statePolicy invalid")
    draft_body = {key: value for key, value in body.items() if key not in {"expectedRevision", "statePolicy"}}
    return _draft_from_body(draft_body), expected, policy


def _service(app: web.Application) -> CharacterService:
    return app["character_service"]


def _require_character_mutation_idle(app: web.Application) -> None:
    """Do not replace a runtime profile while a turn/TTS is in flight."""
    agent = app["agent"]
    status = getattr(agent, "status", None)
    if status is not None and not status.is_accepting_input:
        raise CharacterBusyError("agent")


@asynccontextmanager
async def _character_mutation_guard(
    app: web.Application,
) -> AsyncIterator[None]:
    """Serialize role replacement against CognitiveLoop.process()."""
    agent = app["agent"]
    _require_character_mutation_idle(app)
    cognitive = getattr(agent, "cognitive", None)
    # 公开属性（2026-09-12 前直取 cognitive._process_lock 私有成员）
    process_lock = getattr(cognitive, "process_lock", None)
    if process_lock is None:
        yield
        return
    if process_lock.locked():
        raise CharacterBusyError("agent")
    async with process_lock:
        _require_character_mutation_idle(app)
        yield


async def _load_instance_runtime(agent: object, instance_id: str) -> "CharacterRuntime":
    registry = getattr(agent, "character_runtime_registry", None)
    runtime = registry.get_existing(instance_id) if registry is not None else None
    if runtime is not None:
        return cast("CharacterRuntime", runtime)
    ensure = getattr(agent, "ensure_character_runtime", None)
    if ensure is None:
        raise CharacterInstanceNotFoundError(instance_id)
    try:
        return cast("CharacterRuntime", await ensure(instance_id))
    except CharacterRuntimeSourceError:
        raise CharacterInstanceNotFoundError(instance_id) from None


def _query_params(request: web.Request, *, allowed: set[str]) -> _QueryParams:
    unknown = set(request.query) - allowed
    if unknown:
        raise CharacterQueryError("未知查询参数")
    limit_raw = request.query.get("limit", "50")
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        raise CharacterQueryError("limit 无效") from None
    return {
        "source": request.query.get("source"),
        "status": request.query.get("status", "active"),
        "search": request.query.get("search"),
        "cursor": request.query.get("cursor"),
        "limit": limit,
    }


async def _me_get(request: web.Request) -> web.Response:
    store = request.app["user_identity_store"]
    profile = store.get()
    if profile is None:
        # P0-3：权威存储不可用时不得报「未初始化」（会诱导客户端重新初始化并覆盖
        # 用户认知）——按稳定公开错误 fail-closed。
        if store.is_authoritative and not store.is_available:
            return _protocol_error_response(
                "STORAGE_UNAVAILABLE", "用户身份存储不可用", 503, retryable=True)
        return _protocol_error_response("USER_PROFILE_NOT_FOUND", "用户身份未初始化", 404)
    return web.json_response(UserIdentityProfile(**profile.model_dump()).wire_dict(), status=200)


async def _me_put(request: web.Request) -> web.Response:
    try:
        body = await _json_body(request)
        wire = UserIdentityUpdate.model_validate(body)
    except (web.HTTPBadRequest, ValidationError):
        return _protocol_error_response("CHARACTER_VALIDATION", "用户身份请求无效", 400)
    store = request.app["user_identity_store"]
    raw = set(body)
    updates = {
        # Omitted nullable fields explicitly clear; defaultCharacterId omission
        # means bootstrap default, per §11.2.
        "preferredAddress": body.get("preferredAddress"),
        "pronouns": body.get("pronouns"),
        "displayName": wire.displayName,
        "locale": wire.locale,
        "timezone": wire.timezone,
        "initialized": True,
        "defaultCharacterId": (
            body.get("defaultCharacterId")
            if "defaultCharacterId" in raw
            else "chr_builtin_elysia"
        ),
    }
    try:
        outcome = await store.update_if_revision(wire.expectedRevision, updates)
    except StorageUnavailableError:
        return _protocol_error_response(
            "STORAGE_UNAVAILABLE", "用户身份存储不可用", 503, retryable=True)
    new = outcome.identity
    if new is None:
        # N-3 / Batch 6：`currentRevision` 只来自**权威库**（repository 在冲突点回读，
        # 随 outcome 带上来）。此前这里读 `store.get()`——进程内缓存，多 writer 时陈旧，
        # 于是 409 谎报 revision，客户端按它重试必然再冲突（可死循环）。
        # `None` = 权威现值未知（记录已不存在/回读失败）→ **省略**该字段，不伪造
        # （§31/§33：承认不知道，好过报一个旧值）。
        details = ({} if outcome.current_revision is None
                   else {"currentRevision": outcome.current_revision})
        return _protocol_error_response(
            "USER_PROFILE_REVISION_CONFLICT",
            "expectedRevision 已过期",
            409,
            retryable=True,
            details=details,
            retry_after_ms=100,
        )
    return web.json_response(UserIdentityProfile(**new.model_dump()).wire_dict(), status=200)


async def _characters_list(request: web.Request) -> web.Response:
    try:
        params = _query_params(
            request, allowed={"source", "status", "search", "cursor", "limit"}
        )
        response = await _service(request.app).list(
            source=params["source"], status=params["status"], search=params["search"],
            cursor=params["cursor"], limit=params["limit"],
        )
        return web.json_response(response.wire_dict(), status=200)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _characters_create(request: web.Request) -> web.Response:
    try:
        draft = _draft_from_body(await _json_body(request))
        detail = await _service(request.app).create(
            draft, owner_user_id="usr_local_default"
        )
        return web.json_response(detail.wire_dict(), status=201)
    except (web.HTTPBadRequest, ValidationError):
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _characters_validate(request: web.Request) -> web.Response:
    try:
        draft = _draft_from_body(await _json_body(request))
        result = await _service(request.app).validate(draft)
        return web.json_response(result.wire_dict(), status=200)
    except (web.HTTPBadRequest, ValidationError):
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)


async def _characters_preview(request: web.Request) -> web.Response:
    try:
        body = await _json_body(request)
        request_model = CharacterPreviewRequest.model_validate(body)
        draft = CharacterDraft(
            **request_model.draft.model_dump(mode="python")
        )
        result = await _service(request.app).preview(
            draft, user_message=request_model.userMessage
        )
        return web.json_response(
            CharacterPreviewResult.model_validate(result).wire_dict(), status=200
        )
    except (web.HTTPBadRequest, ValidationError):
        return _protocol_error_response("CHARACTER_VALIDATION", "角色预览请求无效", 400)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _characters_get(request: web.Request) -> web.Response:
    try:
        detail = await _service(request.app).get(request.match_info["id"])
        if detail is None:
            raise CharacterNotFoundError(request.match_info["id"])
        return web.json_response(detail.wire_dict(), status=200)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _characters_update(request: web.Request) -> web.Response:
    try:
        async with _character_mutation_guard(request.app):
            body = await _json_body(request)
            draft, expected, policy = _wire_update_body(body)
            result = await _service(request.app).update(
                request.match_info["id"], draft,
                expected_revision=expected, state_policy=policy,
            )
        return web.json_response(result.wire_dict(), status=200)
    except (web.HTTPBadRequest, ValidationError, ValueError):
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _characters_delete(request: web.Request) -> web.Response:
    try:
        result = await _service(request.app).archive(request.match_info["id"])
        return web.json_response(result.wire_dict(), status=200)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _characters_clone(request: web.Request) -> web.Response:
    try:
        body = await _json_body(request)
        if set(body) - {"displayName"}:
            raise ValueError("unknown clone fields")
        display_name = body.get("displayName")
        if display_name is not None and (
            not isinstance(display_name, str) or len(display_name.strip()) > 64
        ):
            raise ValueError("displayName invalid")
        detail = await _service(request.app).clone(
            request.match_info["id"], display_name=display_name
        )
        return web.json_response(detail.wire_dict(), status=201)
    except (web.HTTPBadRequest, ValidationError, ValueError):
        return _protocol_error_response("CHARACTER_VALIDATION", "克隆请求无效", 400)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _characters_revisions(request: web.Request) -> web.Response:
    try:
        params = _query_params(request, allowed={"cursor", "limit"})
        response = await _service(request.app).list_revisions(
            request.match_info["id"],
            cursor=cast(str | None, params["cursor"]),
            limit=cast(int, params["limit"]),
        )
        return web.json_response(response.wire_dict(), status=200)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _characters_rollback(request: web.Request) -> web.Response:
    try:
        async with _character_mutation_guard(request.app):
            body = await _json_body(request)
            allowed = {"targetRevision", "expectedRevision", "statePolicy"}
            if set(body) != allowed:
                raise ValueError("rollback fields mismatch")
            target = body["targetRevision"]
            expected = body["expectedRevision"]
            policy = body["statePolicy"]
            if (
                any(not isinstance(value, int) or isinstance(value, bool) or value < 1
                    for value in (target, expected))
                or policy not in {item.value for item in CharacterStatePolicy}
            ):
                raise ValueError("rollback values invalid")
            result = await _service(request.app).rollback(
                request.match_info["id"],
                target_revision=target,
                expected_revision=expected,
                state_policy=policy,
            )
        return web.json_response(result.wire_dict(), status=200)
    except (web.HTTPBadRequest, ValidationError, ValueError):
        return _protocol_error_response("CHARACTER_VALIDATION", "回滚请求无效", 400)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


def _public_state(runtime: "CharacterRuntime") -> CharacterPublicState:
    from core.character.public_state import make_public_state

    view = make_public_state(runtime)
    return CharacterPublicState(
        characterId=runtime.definition.characterId,
        characterInstanceId=runtime.instance.instanceId,
        characterRevision=runtime.revision.revision,
        stateVersion=runtime.stateVersion,
        state=view,
    )


async def _instance_state_get(request: web.Request) -> web.Response:
    agent = request.app["agent"]
    instance_id = request.match_info["id"]
    try:
        runtime = await _load_instance_runtime(agent, instance_id)
        return web.json_response(
            _public_state(runtime).wire_dict(), status=200
        )
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


async def _instance_reset_evolution(request: web.Request) -> web.Response:
    agent = request.app["agent"]
    instance_id = request.match_info["id"]
    try:
        request_model = CharacterResetRequest.model_validate(await _json_body(request))
        # BE-STAGEH-01 第一道快速拒绝：turn 在途时立即 409（领域 service 的
        # runtime lease 仍独立守住不变量——route 只是快速路径，不是唯一防线）。
        async with _character_mutation_guard(request.app):
            runtime = await _load_instance_runtime(agent, instance_id)
            repo = getattr(agent, "instance_repo", None)
            if repo is None:
                raise CharacterStorageUnavailableError("CharacterInstanceRepository 未装配")

            async def persist(state: object, new_version: int) -> bool:
                return await repo.cas_state_version(
                    instance_id,
                    expected=request_model.expectedStateVersion,
                    new_state=state,
                    new_version=new_version,
                )

            reset_state = await _service(request.app).reset_evolution(
                instance_id,
                expected_state_version=request_model.expectedStateVersion,
                runtime=runtime,
                persist=persist,
            )
            public = _public_state(runtime)
            result = CharacterResetResult(
                state=public,
                resetFields=list(RESET_FIELDS),
            )
            # Touch result construction with the state returned by the domain method so
            # tests cannot accidentally pass a stale runtime snapshot.
            _ = reset_state
            return web.json_response(result.wire_dict(), status=200)
    except (web.HTTPBadRequest, ValidationError):
        return _protocol_error_response("CHARACTER_VALIDATION", "reset 请求无效", 400)
    except Exception as exc:
        return _character_error_response(exc) or _protocol_error_response(
            "INTERNAL", "内部错误", 500
        )


def register_character_routes(app: web.Application) -> None:
    """注册角色/身份 REST 路由。

    鉴权归属：本组 14 条路由全部位于 `/api/` 前缀下，由 `_make_access_auth_middleware`
    （产品 access_token，`AGENT_ACCESS_TOKEN`）统一保护——与 session/provider 等其余业务
    REST 域一致，故此处不再接收 check_auth 回调。管理面 `_check_auth`
    （`SERVER_AUTH_TOKEN`）只护 `/shutdown`、`/restart`、`/tools`、`/debug/api/*`。
    """
    app.router.add_get("/api/me", _me_get)
    app.router.add_put("/api/me", _me_put)
    app.router.add_get("/api/characters", _characters_list)
    app.router.add_post("/api/characters", _characters_create)
    app.router.add_post("/api/characters/validate", _characters_validate)
    # 固定路径必须先于 /{id}，否则 preview 会被动态 id 路由吞掉。
    app.router.add_post("/api/characters/preview", _characters_preview)
    app.router.add_get("/api/characters/{id}", _characters_get)
    app.router.add_put("/api/characters/{id}", _characters_update)
    app.router.add_delete("/api/characters/{id}", _characters_delete)
    app.router.add_post("/api/characters/{id}/clone", _characters_clone)
    app.router.add_get("/api/characters/{id}/revisions", _characters_revisions)
    app.router.add_post("/api/characters/{id}/rollback", _characters_rollback)
    app.router.add_get("/api/character-instances/{id}/state", _instance_state_get)
    app.router.add_post(
        "/api/character-instances/{id}/reset-evolution", _instance_reset_evolution
    )
