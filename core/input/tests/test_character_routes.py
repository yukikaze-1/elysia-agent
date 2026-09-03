"""character_routes 错误映射单测（marker=service，纯函数，无 aiohttp client/Agent）。

阶段 D 路由的可靠测试点是 `_character_error_response`（Service 异常 → ProtocolError
映射），它是纯函数、确定性、无 I/O。完整 aiohttp 路由冒烟在阶段 G/联调做（需装 Agent）。
"""
from core.input.character_routes import _character_error_response
from core.character.service import (
    CharacterNotFoundError, CharacterImmutableError, CharacterArchivedError,
    CharacterValidationError, RevisionConflictError,
)
from core.storage.base import StorageUnavailableError


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


def test_unknown_exc_maps_to_none():
    assert _character_error_response(ValueError("x")) is None


def test_archived_maps_to_409():
    r = _character_error_response(CharacterArchivedError("x"))
    assert r is not None and r.status == 409
