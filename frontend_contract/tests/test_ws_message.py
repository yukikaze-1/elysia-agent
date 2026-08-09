"""ws_message 契约建模测试：判别联合成员 + ts_null/X? 定性 + wire_dict 递归（marker=unit）。

pydantic 2.11 剥 Annotated（field.annotation 只剩内层类型、marker 移入 field.metadata），
故 ts_null 定性一律走 field_is_ts_null(field) 而非 is_ts_null(field.annotation)。
"""
import pytest

from frontend_contract import ws_message as w
from frontend_contract.base import field_is_ts_null
from frontend_contract.gen_ts import generate_ts
from frontend_contract.rest import ConversationUsage
from frontend_contract.ws_message import MessageChannel, SegmentKind


@pytest.mark.unit
def test_server_message_members():
    """ServerMessage 判别联合：38 具名成员（27 既有 + 11 批次 6 语音/播放；UnknownMessage 由发射器追加）。"""
    from typing import get_args
    # ServerMessage = Annotated[Union[成员], Field(discriminator="type")]
    # get_args(Annotated) 返回 (Union[...], FieldInfo) —— 必须先剥 Annotated 再剥 Union
    members = get_args(get_args(w.ServerMessage)[0])
    assert len(members) == 38  # 39 = 38 具名 + UnknownMessage（发射器追加）
    assert all(m.__name__ != "UnknownMessage" for m in members)
    assert all(getattr(m, "model_fields", None) is not None for m in members)  # 均为 BaseModel


@pytest.mark.unit
def test_switch_vs_service_error_qualification():
    """switch_model_result.error 缺省省略（X?）；service_mode_result.error 可为 null（ts_null）。"""
    assert field_is_ts_null(w.SwitchModelResultMessage.model_fields["error"]) is False
    assert field_is_ts_null(w.ServiceModeResultMessage.model_fields["error"]) is True


@pytest.mark.unit
def test_capabilities_model_is_ts_null():
    """CapabilitiesMessage.services.{stt,tts,llm}.model 为 null（available=false 时显式 null）。"""
    assert field_is_ts_null(w.ServiceCapability.model_fields["model"]) is True


@pytest.mark.unit
def test_wire_dict_list_recursive():
    """list[ContractModel] 元素递归 wire_dict（SessionMessage.messages 场景，base.py list 分支守护）。"""
    item = w.SessionMessageItem(role="assistant", content="hi")
    msg = w.SessionMessage(messages=[item], token_stats=w.TokenStats(total=1, byModel={}),
                           conversationId="conv_x")
    d = msg.wire_dict()
    assert d["messages"][0]["role"] == "assistant"
    assert d["token_stats"]["total"] == 1


@pytest.mark.unit
def test_session_message_has_conversation_id():
    """SessionMessage 必发 conversationId（批次 9a1）：wire 携带 + type=session。"""
    msg = w.SessionMessage(messages=[], token_stats=None, conversationId="conv_abc123")
    wire = msg.wire_dict()
    assert wire["conversationId"] == "conv_abc123"
    assert wire["type"] == "session"


@pytest.mark.unit
def test_server_info_message_wire():
    """server_info：protocol_version/server_version/features/services；features 全 false 默认。"""
    msg = w.ServerInfoMessage(
        server_version="0.1.0",
        features=w.ServerInfoFeatures(),
        services={
            "llm": w.ServiceCapability(
                available=True, enabled=True, model="fake/test", default="fake/test"),
        },
    )
    d = msg.wire_dict()
    assert d["type"] == "server_info"
    assert d["protocol_version"] == "2.0"
    assert d["server_version"] == "0.1.0"
    assert d["features"] == {
        "structured_generation": False, "multi_conversation": False,
        "desktop_runtime": False, "dictation": False,
        "voice_recording": False, "voice_realtime": False,
        "media_upload": False,
    }
    assert d["services"]["llm"]["model"] == "fake/test"


# ── 批次 2 v2 generation 事件契约 ────────────────────────────────────────


@pytest.mark.unit
def test_user_ack_message_wire():
    """user_message.ack：messageId/clientRequestId/status；None 缺省省略。"""
    msg = w.UserAckMessage(
        messageId="msg_u1", clientRequestId="req_1", clientMessageId="cm_1",
        conversationId=None, receivedAt="2026-08-04T00:00:00+00:00", status="accepted",
    )
    d = msg.wire_dict()
    assert d["type"] == "user_message.ack"
    assert d["messageId"] == "msg_u1"
    assert d["clientRequestId"] == "req_1"
    assert d["clientMessageId"] == "cm_1"
    assert "conversationId" not in d  # None 缺省省略
    assert d["status"] == "accepted"
    assert d["receivedAt"]


@pytest.mark.unit
def test_generation_segment_started_wire():
    """generation.segment.started：channel/kind/segmentIndex。"""
    msg = w.GenerationSegmentStartedMessage(
        generationId="gen_1", segmentId="seg_1", segmentIndex=0,
        channel=MessageChannel.PUBLIC_REPLY, kind=SegmentKind.TEXT,
    )
    d = msg.wire_dict()
    assert d["type"] == "generation.segment.started"
    assert d["channel"] == "public_reply"
    assert d["kind"] == "text"
    assert d["segmentIndex"] == 0


@pytest.mark.unit
def test_generation_delta_wire():
    """generation.delta 消息：deltaSequence + delta。"""
    msg = w.GenerationDeltaMessage(
        generationId="gen_1", segmentId="seg_1", segmentIndex=0,
        deltaSequence=2, delta="你",
    )
    d = msg.wire_dict()
    assert d["type"] == "generation.delta"
    assert d["deltaSequence"] == 2
    assert d["delta"] == "你"


@pytest.mark.unit
def test_generation_completed_wire():
    """generation.completed：finishReason 限 stop|tool_calls + usage/model/mood。"""
    usage = w.TokenUsage(rounds=[], turn_total=0)
    msg = w.GenerationCompletedMessage(
        generationId="gen_1", messageId="msg_1", channel=MessageChannel.PUBLIC_REPLY,
        finishReason="stop", usage=usage, model="fake/test", mood="开心",
        completeness="complete",
    )
    d = msg.wire_dict()
    assert d["type"] == "generation.completed"
    assert d["finishReason"] == "stop"
    assert d["usage"]["turn_total"] == 0
    assert d["model"] == "fake/test"
    assert d["mood"] == "开心"
    assert d["completeness"] == "complete"


@pytest.mark.unit
def test_generation_failed_wire():
    """generation.failed：code/message/retryable。"""
    msg = w.GenerationFailedMessage(
        generationId="gen_1", messageId="msg_1", code="GENERATION_CANCELLED",
        message="已取消", retryable=False,
    )
    d = msg.wire_dict()
    assert d["type"] == "generation.failed"
    assert d["code"] == "GENERATION_CANCELLED"
    assert d["retryable"] is False


@pytest.mark.unit
def test_v2_text_message_model():
    """V2TextMessage：type=text + clientRequestId/clientMessageId/conversationId（不进 ServerMessage 联合）。"""
    msg = w.V2TextMessage(
        content="你好", clientRequestId="req_1", clientMessageId="cm_1",
        conversationId=None,
    )
    d = msg.wire_dict()
    assert d["type"] == "text"
    assert d["content"] == "你好"
    assert d["clientRequestId"] == "req_1"
    assert "conversationId" not in d


# ── 批次 3 v2 增量（generation conversationId / conversationUsage / activate_session） ──


@pytest.mark.unit
def test_generation_events_have_conversation_id_field():
    """6 个 generation.* 均带 conversationId（Started 已有，其余 5 个批次 3 新增可缺省字段）。"""
    for cls in (w.GenerationStartedMessage, w.GenerationSegmentStartedMessage,
                w.GenerationDeltaMessage, w.GenerationSegmentCompletedMessage,
                w.GenerationCompletedMessage, w.GenerationFailedMessage):
        assert "conversationId" in cls.model_fields
        assert cls.model_fields["conversationId"].is_required() is False


@pytest.mark.unit
def test_generation_segment_started_conversation_id():
    """generation.segment.started 带 conversationId（None 缺省省略）。"""
    msg = w.GenerationSegmentStartedMessage(
        generationId="gen_1", segmentId="seg_1", segmentIndex=0,
        channel=MessageChannel.PUBLIC_REPLY, kind=SegmentKind.TEXT, conversationId="conv_abc",
    )
    d = msg.wire_dict()
    assert d["type"] == "generation.segment.started"
    assert d["conversationId"] == "conv_abc"


@pytest.mark.unit
def test_generation_delta_conversation_id():
    """generation.delta 带 conversationId。"""
    msg = w.GenerationDeltaMessage(
        generationId="gen_1", segmentId="seg_1", segmentIndex=0,
        deltaSequence=2, delta="你", conversationId="conv_abc",
    )
    assert msg.wire_dict()["conversationId"] == "conv_abc"


@pytest.mark.unit
def test_generation_segment_completed_conversation_id():
    """generation.segment.completed 带 conversationId。"""
    msg = w.GenerationSegmentCompletedMessage(
        generationId="gen_1", segmentId="seg_1", segmentIndex=0,
        channel=MessageChannel.PUBLIC_REPLY, kind=SegmentKind.TEXT, content="你好", conversationId="conv_abc",
    )
    assert msg.wire_dict()["conversationId"] == "conv_abc"


@pytest.mark.unit
def test_generation_completed_conversation_id_and_usage():
    """generation.completed 带 conversationId + conversationUsage（会话累计，可缺省省略）。"""
    usage = w.TokenUsage(rounds=[], turn_total=0)
    conv_usage = ConversationUsage(total=42, byModel={"fake/test": 42})
    msg = w.GenerationCompletedMessage(
        generationId="gen_1", messageId="msg_1", channel=MessageChannel.PUBLIC_REPLY,
        finishReason="stop", usage=usage, model="fake/test",
        conversationId="conv_abc", conversationUsage=conv_usage,
    )
    d = msg.wire_dict()
    assert d["conversationId"] == "conv_abc"
    assert d["conversationUsage"] == {"total": 42, "byModel": {"fake/test": 42}}
    # conversationUsage 可缺省省略
    msg2 = w.GenerationCompletedMessage(
        generationId="gen_2", messageId="msg_2", channel=MessageChannel.PUBLIC_REPLY,
        finishReason="stop", usage=usage, model="fake/test",
    )
    assert "conversationUsage" not in msg2.wire_dict()


@pytest.mark.unit
def test_generation_failed_conversation_id():
    """generation.failed 带 conversationId。"""
    msg = w.GenerationFailedMessage(
        generationId="gen_1", messageId="msg_1", code="GENERATION_CANCELLED",
        message="已取消", retryable=False, conversationId="conv_abc",
    )
    assert msg.wire_dict()["conversationId"] == "conv_abc"


@pytest.mark.unit
def test_activate_session_message():
    """ActivateSessionMessage：Client→Server，type=activate_session，conversationId 必填。"""
    msg = w.ActivateSessionMessage(conversationId="conv_abc")
    d = msg.wire_dict()
    assert d == {"type": "activate_session", "conversationId": "conv_abc"}
    # conversationId 必填：缺失抛 ValidationError
    with pytest.raises(Exception):
        w.ActivateSessionMessage()


# ── 批次 8c error 信封完整化（F1/F18） ────────────────────────────────────────


@pytest.mark.unit
def test_error_message_retryable_details_wire():
    """ErrorMessage：retryable/details 显式赋值 → wire_dict 保留。"""
    m = w.ErrorMessage(message="x", code="STT_UNAVAILABLE",
                       retryable=True, details={"n": 1})
    d = m.wire_dict()
    assert d["retryable"] is True
    assert d["details"] == {"n": 1}


@pytest.mark.unit
def test_error_message_retryable_omitted_when_none():
    """ErrorMessage：retryable/details 缺省 None → wire_dict 省略（exclude_none）。"""
    d = w.ErrorMessage(message="x").wire_dict()
    assert "retryable" not in d
    assert "details" not in d


# ── 批次 8c F3 死契约面移除 + ActionMessage.params 可选（Task 3） ──────────────────


@pytest.mark.unit
def test_heartbeat_ack_removed_from_union():
    """HeartbeatAckMessage 死契约面：已从 ServerMessage 联合移除（零发射点/零前端消费）。"""
    from frontend_contract.tests.test_batch6_contract import _union_members
    members = _union_members()
    assert "HeartbeatAckMessage" not in members
    assert len(members) == 38


@pytest.mark.unit
def test_action_message_params_optional_omitted_when_none():
    """ActionMessage.params 可选：None 缺省省略，显式传值保留。"""
    d = w.ActionMessage(action="tool_executing").wire_dict()
    assert "params" not in d
    d2 = w.ActionMessage(action="tool_executing", params={"tool": "t"}).wire_dict()
    assert d2["params"] == {"tool": "t"}


# ── 批次 8c F19 TokenStats.byModel 对齐（Task 15） ─────────────────────────


@pytest.mark.unit
def test_token_stats_wire_camel_by_model():
    """TokenStats wire 用 camel byModel（对齐 ConversationUsage）：byModel 识别 + by_model 键省略。"""
    d = w.TokenStats(total=1, byModel={"m": 1}).wire_dict()
    assert d["byModel"] == {"m": 1}
    assert "by_model" not in d


# ── Literal → StrEnum 统一（plan Task 6） ─────────────────────────────────


@pytest.mark.unit
class TestWsEnums:
    """ws_message 非判别字段 Literal → StrEnum：枚举成员值 + 字段引用 + gen_ts 发射命名别名。"""

    def test_new_enums_defined(self):
        assert w.MessageChannel.INNER_VOICE == "inner_voice"
        assert w.SegmentKind.MARKDOWN == "markdown"
        assert w.AudioFormat.WAV == "wav"
        assert w.ServiceName.STT == "stt"
        assert w.VoiceEndReason.TIMEOUT == "timeout"

    def test_user_ack_status_annotation_is_enum(self):
        """UserAckMessage.status 必须是 AckStatus 类型（防回落 Literal）。"""
        assert w.UserAckMessage.model_fields["status"].annotation is w.AckStatus

    def test_gen_ts_emits_named_aliases(self):
        out = generate_ts([w.GenerationSegmentStartedMessage, w.GenerationCompletedMessage])
        assert "export type MessageChannel =" in out
        assert "export type SegmentKind =" in out
        assert "'inner_voice'" in out


# ── 9B-2 ClientMessage 判别联合（Task 5） ─────────────────────────────────


@pytest.mark.unit
def test_client_message_discriminated_union():
    """ClientMessage 判别联合：type=text → V2TextMessage 实例（TextMessage 不入联合）。"""
    from pydantic import TypeAdapter

    m = TypeAdapter(w.ClientMessage).validate_python(
        {"type": "text", "content": "hi", "clientRequestId": "r1"})
    assert m.type == "text"
    assert isinstance(m, w.V2TextMessage)


@pytest.mark.unit
def test_client_message_excludes_text_message():
    """ClientMessage 不含 TextMessage——type=text 由 V2TextMessage 独占（discriminator 撞名）。"""
    from typing import get_args

    members = {c.__name__ for c in get_args(get_args(w.ClientMessage)[0])}
    assert "TextMessage" not in members
    assert "V2TextMessage" in members
