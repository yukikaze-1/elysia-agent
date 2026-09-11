"""Task 6 ACK 判别联合 + 生成器嵌套判别联合（marker=unit）。

覆盖 plan Task 6 Step 1：UserAckMessage → Accepted/Rejected status 判别联合拒绝非法
组合；Accepted/Rejected 不变量；AckError 顶层 retryAfterMs；ErrorMessage/6 命令消息
可选 clientRequestId；gen_schema/__main__ 嵌套联合不崩；产物 ServerMessage 无
"Annotated"；6 负面 golden 帧经判别校验 + IN_PROGRESS retryAfterMs==30000。
"""
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

import frontend_contract.__main__ as main_mod
from frontend_contract import gen_schema, ws_message as w

_TS = "2026-08-04T00:00:00+00:00"

_ROOT = Path(__file__).resolve().parent.parent.parent


def _ack_adapter() -> TypeAdapter:
    """UserAckMessage 判别联合校验器（status 判别）。"""
    return TypeAdapter(w.UserAckMessage)


# ── 判别 schema 拒绝非法组合 ──────────────────────────────────────────────


@pytest.mark.unit
def test_schema_rejects_accepted_empty_message_id():
    with pytest.raises(ValidationError):
        _ack_adapter().validate_python({
            "type": "user_message.ack", "status": "accepted",
            "messageId": "", "clientRequestId": "req_1", "receivedAt": _TS,
        })


@pytest.mark.unit
def test_schema_rejects_accepted_with_error():
    """Accepted 无 error 字段（extra=forbid）——携带 error 键 → ValidationError。"""
    with pytest.raises(ValidationError):
        _ack_adapter().validate_python({
            "type": "user_message.ack", "status": "accepted",
            "messageId": "msg_1", "clientRequestId": "req_1", "receivedAt": _TS,
            "error": {"code": "X", "message": "m", "retryable": False},
        })


@pytest.mark.unit
def test_schema_rejects_rejected_nonempty_message_id():
    """Rejected.messageId 恒 Literal[""]——非空 → ValidationError。"""
    with pytest.raises(ValidationError):
        _ack_adapter().validate_python({
            "type": "user_message.ack", "status": "rejected",
            "messageId": "msg_1", "clientRequestId": "req_1", "receivedAt": _TS,
            "error": {"code": "X", "message": "m", "retryable": False},
        })


@pytest.mark.unit
def test_schema_rejects_rejected_without_error():
    """Rejected.error 必填——缺 error → ValidationError。"""
    with pytest.raises(ValidationError):
        _ack_adapter().validate_python({
            "type": "user_message.ack", "status": "rejected",
            "messageId": "", "clientRequestId": "req_1", "receivedAt": _TS,
        })


# ── Accepted/Rejected 模型不变量 ──────────────────────────────────────────


@pytest.mark.unit
def test_accepted_requires_nonempty_ids():
    """AcceptedUserAckMessage 强制非空 messageId/clientRequestId。"""
    with pytest.raises(ValidationError):
        w.AcceptedUserAckMessage(messageId="", clientRequestId="req_1", receivedAt=_TS)
    with pytest.raises(ValidationError):
        w.AcceptedUserAckMessage(messageId="msg_1", clientRequestId="", receivedAt=_TS)


@pytest.mark.unit
def test_rejected_message_id_is_always_empty():
    """RejectedUserAckMessage.messageId 恒 ""（D2 决策记录）。"""
    m = w.RejectedUserAckMessage(
        clientRequestId="req_1", receivedAt=_TS, status="rejected",
        error=w.AckError(code="X", message="m", retryable=False))
    assert m.messageId == ""
    with pytest.raises(ValidationError):
        w.RejectedUserAckMessage(
            messageId="msg_1", clientRequestId="req_1", receivedAt=_TS, status="rejected",
            error=w.AckError(code="X", message="m", retryable=False))


@pytest.mark.unit
def test_ack_error_top_level_retry_after_ms():
    """AckError 顶层 retryAfterMs（与 ErrorMessage 同位置）→ wire 保留。"""
    err = w.AckError(code="RATE_LIMITED", message="m", retryable=True, retryAfterMs=30000)
    assert err.retryAfterMs == 30000
    assert err.wire_dict()["retryAfterMs"] == 30000


@pytest.mark.unit
def test_ack_discriminated_union_roundtrip():
    """TypeAdapter 按 status 判别到具体成员，wire 与成员一致。"""
    a = _ack_adapter().validate_python({
        "type": "user_message.ack", "status": "accepted",
        "messageId": "msg_1", "clientRequestId": "req_1", "receivedAt": _TS,
    })
    assert isinstance(a, w.AcceptedUserAckMessage)
    assert a.wire_dict()["status"] == "accepted"
    r = _ack_adapter().validate_python({
        "type": "user_message.ack", "status": "rejected",
        "messageId": "", "clientRequestId": "req_1", "receivedAt": _TS,
        "error": {"code": "X", "message": "m", "retryable": False},
    })
    assert isinstance(r, w.RejectedUserAckMessage)
    assert r.wire_dict()["error"]["code"] == "X"


# ── 生成器嵌套判别联合回归 ────────────────────────────────────────────────


@pytest.mark.unit
def test_build_document_nested_union_no_crash():
    """gen_schema.build_models 成员判定递归化——嵌套 Annotated 成员不抛 TypeError。

    旧实现 all(issubclass(c, BaseModel) ...) 遇 UserAckMessage（Annotated 联合成员）
    抛 TypeError: issubclass() arg 1 must be a class；修复后 pydantic 原生产出嵌套
    oneOf，UserAckMessage/Accepted/Rejected 均入档。
    """
    doc = gen_schema.build_document()
    assert "ServerMessage" in doc["models"]
    assert "UserAckMessage" in doc["models"]           # 命名联合（Annotated）入档
    assert "AcceptedUserAckMessage" in doc["models"]
    assert "RejectedUserAckMessage" in doc["models"]


@pytest.mark.unit
def test_collect_flattens_nested_ack_union():
    """__main__._collect 递归展开嵌套 Annotated 联合——成员为具体 BaseModel 非 "Annotated"。"""
    models, discriminated = main_mod._collect()
    sm_names = {c.__name__ for c in discriminated["ServerMessage"]}
    assert "AcceptedUserAckMessage" in sm_names
    assert "RejectedUserAckMessage" in sm_names
    assert "Annotated" not in sm_names
    assert all(getattr(c, "model_fields", None) is not None
               for c in discriminated["ServerMessage"])


@pytest.mark.unit
def test_generated_ts_ack_union_no_annotated():
    """contract.generated.ts ServerMessage 引用 Accepted/Rejected 具体类型，无 Annotated 字面量。"""
    content = (_ROOT / "frontend" / "src" / "types" / "contract.generated.ts").read_text(encoding="utf-8")
    sm_block = content[content.index("export type ServerMessage ="):content.index("export type ClientMessage =")]
    assert "| AcceptedUserAckMessage" in sm_block
    assert "| RejectedUserAckMessage" in sm_block
    assert "Annotated" not in sm_block


# ── ErrorMessage + 6 命令消息 clientRequestId ──────────────────────────────


@pytest.mark.unit
def test_command_messages_have_optional_client_request_id():
    """6 个命令消息加可选 clientRequestId（switch_model/tool_approval/set_service/
    set_tts_voice/activate_session/wake_toggle）。"""
    for cls in (w.SwitchModelMessage, w.ToolApprovalMessage, w.SetServiceMessage,
                w.SetTtsVoiceMessage, w.ActivateSessionMessage, w.WakeToggleMessage):
        assert "clientRequestId" in cls.model_fields, f"{cls.__name__} 缺 clientRequestId"
        assert cls.model_fields["clientRequestId"].is_required() is False


@pytest.mark.unit
def test_error_message_client_request_id_and_retry_after_ms():
    """ErrorMessage 加可选 clientRequestId + 顶层 retryAfterMs（§2.2）。"""
    assert "clientRequestId" in w.ErrorMessage.model_fields
    assert "retryAfterMs" in w.ErrorMessage.model_fields
    assert w.ErrorMessage.model_fields["clientRequestId"].is_required() is False
    assert w.ErrorMessage.model_fields["retryAfterMs"].is_required() is False
    m = w.ErrorMessage(message="限流", code="RATE_LIMITED", retryable=True,
                       clientRequestId="req_1", retryAfterMs=1000)
    d = m.wire_dict()
    assert d["clientRequestId"] == "req_1"
    assert d["retryAfterMs"] == 1000


# ── 7 负面 golden 帧经判别校验 + IN_PROGRESS retryAfterMs ──────────────────


@pytest.mark.unit
def test_rejected_golden_frames_validate_and_in_progress_retry():
    """7 负面 golden 帧均通过 UserAckMessage 判别校验；IN_PROGRESS retryAfterMs==30000。"""
    traces = _ROOT / "docs" / "archive" / "frontend-handoff-v2" / "traces"
    content = (traces / "ws-v2-rejected.jsonl").read_text(encoding="utf-8")
    frames = [json.loads(line) for line in content.splitlines() if line.strip()]
    assert len(frames) == 7
    codes = {f["error"]["code"] for f in frames}
    assert codes == {"CONTRACT_VALIDATION", "SESSION_NOT_FOUND", "ATTACHMENT_NOT_FOUND",
                     "QUEUE_FULL", "STORAGE_UNAVAILABLE", "RATE_LIMITED", "IN_PROGRESS"}
    in_progress = None
    for f in frames:
        m = _ack_adapter().validate_python(f)
        assert isinstance(m, w.RejectedUserAckMessage)
        assert m.messageId == ""
        assert isinstance(m.error, w.AckError)
        if f["error"]["code"] == "IN_PROGRESS":
            in_progress = f
    assert in_progress is not None
    # 与 Task 4 idempotency.IN_PROGRESS_RETRY_AFTER_MS 单源同值（= GRACE_SECONDS * 1000）
    from core.storage.idempotency import IN_PROGRESS_RETRY_AFTER_MS
    assert in_progress["error"]["retryAfterMs"] == 30000
    assert IN_PROGRESS_RETRY_AFTER_MS == 30000
