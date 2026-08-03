"""ID 生成器 + GenerationContext 测试（批次 2 Task 2，marker=unit）。"""

import pytest

from core.cognitive.generation import GenerationContext
from core.utils.message_id import new_generation_id, new_segment_id


@pytest.mark.unit
def test_new_generation_id_format():
    """generation ID 独立前缀 gen_（与 messageId 的 msg_ 区分）。"""
    gid = new_generation_id()
    assert gid.startswith("gen_")
    assert len(gid) == len("gen_") + 26


@pytest.mark.unit
def test_new_segment_id_format():
    """segment ID 独立前缀 seg_。"""
    sid = new_segment_id()
    assert sid.startswith("seg_")
    assert len(sid) == len("seg_") + 26


@pytest.mark.unit
def test_generation_context_open_segment_increments():
    """open_segment 分配 seg_ ID、递增全局 segmentIndex、重置段内 delta。"""
    ctx = GenerationContext(
        generation_id="gen_1", user_message_id="msg_u1", assistant_message_id="msg_a1",
    )
    sid0 = ctx.open_segment("inner_voice")
    assert sid0.startswith("seg_")
    assert ctx.current_segment_index == 0
    assert ctx.channel == "inner_voice"
    # 段内 delta 递增
    assert ctx.next_delta() == 0
    assert ctx.next_delta() == 1

    sid1 = ctx.open_segment("public_reply")
    assert ctx.current_segment_index == 1  # 全局递增
    assert ctx.next_delta() == 0  # 新段重置
    assert sid1 != sid0


@pytest.mark.unit
def test_generation_context_tool_round_does_not_reset_index():
    """工具轮（新一次 open_segment）不重置 segmentIndex——跨轮单调递增。"""
    ctx = GenerationContext(
        generation_id="gen_1", user_message_id="msg_u1", assistant_message_id="msg_a1",
    )
    ctx.open_segment("public_reply")
    ctx.open_segment("public_reply")
    ctx.open_segment("public_reply")  # 工具轮后的分段
    assert ctx.current_segment_index == 2
