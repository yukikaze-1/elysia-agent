"""ID 生成器 + GenerationContext 测试（批次 2 Task 2，marker=unit）。"""

from typing import get_type_hints

import pytest

from frontend_contract.ws_message import MessageChannel, SegmentKind
from core.cognitive.generation import GenerationContext
from core.schema import GenerationId, MessageId, RequestId, SegmentId, SessionId
from core.utils.message_id import new_generation_id, new_segment_id


@pytest.mark.unit
def test_new_generation_id_format():
    """generation ID 独立前缀 gen_（与 messageId 的 msg_ 区分）。"""
    gid = new_generation_id()
    assert get_type_hints(new_generation_id)["return"] is GenerationId
    assert gid.startswith("gen_")
    assert len(gid) == len("gen_") + 26


@pytest.mark.unit
def test_new_segment_id_format():
    """segment ID 独立前缀 seg_。"""
    sid = new_segment_id()
    assert get_type_hints(new_segment_id)["return"] is SegmentId
    assert sid.startswith("seg_")
    assert len(sid) == len("seg_") + 26


@pytest.mark.unit
def test_generation_context_open_segment_increments():
    """open_segment 分配 seg_ ID、递增全局 segmentIndex、重置段内 delta。"""
    ctx = GenerationContext(
        generation_id=GenerationId("gen_1"), user_message_id=MessageId("msg_u1"),
        assistant_message_id=MessageId("msg_a1"),
    )
    sid0 = ctx.open_segment(MessageChannel.INNER_VOICE)
    assert sid0.startswith("seg_")
    assert ctx.current_segment_index == 0
    assert ctx.channel == MessageChannel.INNER_VOICE
    # 段内 delta 递增
    assert ctx.next_delta() == 0
    assert ctx.next_delta() == 1

    sid1 = ctx.open_segment(MessageChannel.PUBLIC_REPLY)
    assert ctx.current_segment_index == 1  # 全局递增
    assert ctx.next_delta() == 0  # 新段重置
    assert sid1 != sid0


@pytest.mark.unit
def test_generation_context_tool_round_does_not_reset_index():
    """工具轮（新一次 open_segment）不重置 segmentIndex——跨轮单调递增。"""
    ctx = GenerationContext(
        generation_id=GenerationId("gen_1"), user_message_id=MessageId("msg_u1"),
        assistant_message_id=MessageId("msg_a1"),
    )
    ctx.open_segment(MessageChannel.PUBLIC_REPLY)
    ctx.open_segment(MessageChannel.PUBLIC_REPLY)
    ctx.open_segment(MessageChannel.PUBLIC_REPLY)  # 工具轮后的分段
    assert ctx.current_segment_index == 2


@pytest.mark.unit
def test_generation_context_id_field_types():
    """GenerationContext 的 ID 字段全部对齐 NewType（get_type_hints 查注解）。"""
    hints = get_type_hints(GenerationContext)
    assert hints["generation_id"] is GenerationId
    assert hints["user_message_id"] is MessageId
    assert hints["assistant_message_id"] is MessageId
    assert hints["conversation_id"] == SessionId | None
    assert hints["request_id"] == RequestId | None
    assert hints["current_segment_id"] == SegmentId | None
    assert hints["model_id"] is str  # 模型名非 ID 语义，保持 str


@pytest.mark.unit
def test_open_segment_channel_kind_enum_types():
    """open_segment 的 channel/kind 对齐契约层 StrEnum（get_type_hints 查注解）。"""
    hints = get_type_hints(GenerationContext.open_segment)
    assert hints["channel"] is MessageChannel
    assert hints["kind"] is SegmentKind
