"""stable_tool_message_id 派生测试（D1，marker=unit）。"""
import pytest

from core.schema.types import MessageId
from core.utils.message_id import new_message_id, stable_tool_message_id


pytestmark = pytest.mark.unit


def test_deterministic_same_input_same_id():
    assert stable_tool_message_id("call_x", "2026-08-13T00:00:00Z") \
        == stable_tool_message_id("call_x", "2026-08-13T00:00:00Z")


def test_different_input_different_id():
    a = stable_tool_message_id("call_x", "2026-08-13T00:00:00Z")
    b = stable_tool_message_id("call_x", "2026-08-13T00:00:01Z")
    c = stable_tool_message_id("call_y", "2026-08-13T00:00:00Z")
    assert len({a, b, c}) == 3


def test_format_matches_msg_namespace():
    mid = stable_tool_message_id("call_x", "2026-08-13T00:00:00Z")
    assert str(mid).startswith("msg_")
    # msg_ + 26 位小写 hex（与 new_message_id 值空间一致）
    assert len(str(mid)) == 30
    assert str(mid)[4:].isalnum() and str(mid)[4:].islower()


def test_new_message_id_never_collides_with_derived():
    # 随机 26hex 与 sha256 摘要撞车概率可忽略；此测试为格式回归护栏
    mid = stable_tool_message_id("call_x", "2026-08-13T00:00:00Z")
    assert mid != new_message_id()
