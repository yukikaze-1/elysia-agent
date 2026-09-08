"""character_routes 错误映射单测（marker=service，纯函数，无 aiohttp client/Agent）。

阶段 D 路由的可靠测试点是 `_character_error_response`（Service 异常 → ProtocolError
映射），它是纯函数、确定性、无 I/O。完整 aiohttp 路由冒烟在阶段 G/联调做（需装 Agent）。
"""
import asyncio
import json
from types import SimpleNamespace

from core.input.character_routes import (
    _character_error_response,
    _character_mutation_guard,
)
from core.character.service import (
    CharacterNotFoundError, CharacterImmutableError, CharacterArchivedError,
    CharacterValidationError, RevisionConflictError, CharacterStorageUnavailableError,
    CharacterPreviewUnavailableError, CharacterStateConflictError, CharacterBusyError,
)
from core.storage.base import StorageUnavailableError

import pytest
from core.input.tests.test_server import _make_agent


def test_not_found_maps_to_404():
    r = _character_error_response(CharacterNotFoundError("x"))
    assert r is not None and r.status == 404


def test_revision_conflict_maps_to_409_retryable():
    r = _character_error_response(RevisionConflictError("x"))
    assert r is not None and r.status == 409


def test_immutable_maps_to_409():
    r = _character_error_response(CharacterImmutableError("x"))
    assert r is not None and r.status == 409


def test_validation_maps_to_400():
    r = _character_error_response(CharacterValidationError("x"))
    assert r is not None and r.status == 400


def test_storage_unavailable_maps_to_503():
    r = _character_error_response(StorageUnavailableError("x"))
    assert r is not None and r.status == 503


def test_character_storage_unavailable_maps_to_503():
    """不变式 #7 回归：connect 失败抛 CharacterStorageUnavailableError（继承
    StorageUnavailableError）→ 503，而非 500。B1 修复曾使其落 500。"""
    assert issubclass(CharacterStorageUnavailableError, StorageUnavailableError)
    r = _character_error_response(CharacterStorageUnavailableError("x"))
    assert r is not None and r.status == 503


def test_unknown_exc_maps_to_none():
    assert _character_error_response(ValueError("x")) is None


def test_archived_maps_to_409():
    r = _character_error_response(CharacterArchivedError("x"))
    assert r is not None and r.status == 409


def test_preview_unavailable_maps_to_stable_retryable_code():
    r = _character_error_response(CharacterPreviewUnavailableError("x"))
    assert r is not None and r.status == 503
    body = json.loads(r.text)
    assert body["code"] == "CHARACTER_PREVIEW_FAILED"
    assert body["retryable"] is True
    assert body["retryAfterMs"] == 1000


def test_state_conflict_exposes_current_version_and_retry_hint():
    r = _character_error_response(CharacterStateConflictError("chi", 9))
    assert r is not None and r.status == 409
    body = json.loads(r.text)
    assert body["code"] == "CHARACTER_STATE_CONFLICT"
    assert body["details"] == {"currentStateVersion": 9}
    assert body["retryable"] is True


def test_busy_exposes_retry_hint():
    r = _character_error_response(CharacterBusyError("chi"))
    assert r is not None and r.status == 409
    body = json.loads(r.text)
    assert body["code"] == "CHARACTER_BUSY"
    assert body["retryAfterMs"] == 500


@pytest.mark.asyncio
async def test_character_mutation_guard_owns_turn_lock_and_rejects_busy() -> None:
    process_lock = asyncio.Lock()
    status = SimpleNamespace(is_accepting_input=True)
    app = {
        "agent": SimpleNamespace(
            status=status,
            cognitive=SimpleNamespace(_process_lock=process_lock),
        )
    }

    async with _character_mutation_guard(app):  # type: ignore[arg-type]
        assert process_lock.locked()

    await process_lock.acquire()
    try:
        with pytest.raises(CharacterBusyError):
            async with _character_mutation_guard(app):  # type: ignore[arg-type]
                pass
    finally:
        process_lock.release()


# ── 阶段 G REST GET /api/character-instances/{id}/state（只读 fail-closed）──


@pytest.mark.asyncio
async def test_instance_state_returns_public_state(aiohttp_client):
    """GET /api/character-instances/{id}/state 返回 Stage G 嵌套产品态。"""
    from core.input.server import create_app

    agent = _make_agent()  # test helper; ensure has character_runtime_registry
    await agent.start()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        # default elysia instance 已注册
        resp = await client.get("/api/character-instances/chi_builtin_elysia/state")
        assert resp.status == 200
        data = await resp.json()
        assert data["characterId"] == "chr_builtin_elysia"
        assert data["characterInstanceId"] == "chi_builtin_elysia"
        assert "state" in data and "mood" in data["state"]
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_instance_state_missing_returns_404(aiohttp_client):
    """未注册 instance → 404（不懒建）。"""
    from core.input.server import create_app

    agent = _make_agent()
    await agent.start()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        resp = await client.get("/api/character-instances/chi_builtin_nonexistent/state")
        assert resp.status == 404
    finally:
        await agent.stop()
