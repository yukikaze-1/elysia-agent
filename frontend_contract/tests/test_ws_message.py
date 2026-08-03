"""ws_message 契约建模测试：判别联合成员 + ts_null/X? 定性 + wire_dict 递归（marker=unit）。

pydantic 2.11 剥 Annotated（field.annotation 只剩内层类型、marker 移入 field.metadata），
故 ts_null 定性一律走 field_is_ts_null(field) 而非 is_ts_null(field.annotation)。
"""
import pytest

from frontend_contract import ws_message as w
from frontend_contract.base import field_is_ts_null


@pytest.mark.unit
def test_server_message_members():
    """ServerMessage 判别联合：26 具名成员（19 v1 层 + 7 v2 generation；UnknownMessage 由发射器追加）。"""
    from typing import get_args
    # ServerMessage = Annotated[Union[19 成员], Field(discriminator="type")]
    # get_args(Annotated) 返回 (Union[...], FieldInfo) —— 必须先剥 Annotated 再剥 Union
    members = get_args(get_args(w.ServerMessage)[0])
    assert len(members) == 26  # 27 = 26 具名 + UnknownMessage（发射器追加）
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
    msg = w.SessionMessage(messages=[item], token_stats=w.TokenStats(total=1, by_model={}))
    d = msg.wire_dict()
    assert d["messages"][0]["role"] == "assistant"
    assert d["token_stats"]["total"] == 1


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
        channel="public_reply", kind="text",
    )
    d = msg.wire_dict()
    assert d["type"] == "generation.segment.started"
    assert d["channel"] == "public_reply"
    assert d["kind"] == "text"
    assert d["segmentIndex"] == 0


@pytest.mark.unit
def test_generation_delta_wire():
    """generation.delta：deltaSequence + delta。"""
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
        generationId="gen_1", messageId="msg_1", channel="public_reply",
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
