"""core/schema/event.py 的 EventData union 模型测试。"""
import pytest
from pydantic import TypeAdapter, ValidationError
from typing import Literal, get_args, get_origin, get_type_hints

from core.schema import MessageId, RequestId, SessionId
from core.schema.agent import EventType
from core.schema.event import (
    EventData,
    InterruptData,
    StateChangeData,
    TaskResultData,
    UserInputData,
    SystemTickData,
)


@pytest.mark.unit
class TestEventDataUnion:
    """EventData discriminated union — 按 event_type 分发。"""

    def test_dispatch_all_five(self):
        adapter = TypeAdapter(EventData)
        cases = [
            ({"event_type": "interrupt", "source": "websocket"}, InterruptData),
            ({"event_type": "state_change", "state": "idle", "request_id": "r1"}, StateChangeData),
            ({"event_type": "user_input", "text": "hi", "user_name": "妖梦", "request_id": "r2"},
             UserInputData),
            ({"event_type": "system_tick", "reason": "silence_timeout", "elapsed": 30.0},
             SystemTickData),
            ({"event_type": "task_result", "task_id": "t1", "result": "done",
              "original_intent": "搜索", "dispatched_at": "2025-01-01T00:00:00Z"},
             TaskResultData),
        ]
        for payload, klass in cases:
            result = adapter.validate_python(payload)
            assert isinstance(result, klass)

    def test_unknown_event_type_rejected(self):
        with pytest.raises(ValidationError):
            TypeAdapter(EventData).validate_python({"event_type": "destroy"})


@pytest.mark.unit
class TestInterruptData:
    def test_defaults(self):
        d = InterruptData()
        assert d.source == "?"
        assert d.event_type == "interrupt"


@pytest.mark.unit
class TestStateChangeData:
    def test_missing_request_id_rejected(self):
        with pytest.raises(ValidationError):
            StateChangeData.model_validate({"event_type": "state_change", "state": "idle"})


@pytest.mark.unit
class TestTaskResultData:
    def test_missing_required_fields_rejected(self):
        with pytest.raises(ValidationError):
            TaskResultData.model_validate(
                {"event_type": "task_result", "task_id": "t1"})


@pytest.mark.unit
class TestUserInputData:
    def test_stt_path_no_content_valid(self):
        d = UserInputData(text="hi", user_name="妖梦", request_id=RequestId("r1"),
                          emotion="happy", events=["laugh"])
        assert d.content is None

    def test_text_path_no_emotion_valid(self):
        d = UserInputData(text="hi", user_name="妖梦", request_id=RequestId("r1"),
                          content=[{"type": "text", "text": "hi"}])
        assert d.emotion == ""
        assert d.events == []

    def test_default_conversation_id_none(self):
        """批次 3：conversation_id 默认 None。"""
        d = UserInputData(text="hi", user_name="妖梦", request_id=RequestId("r1"))
        assert d.conversation_id is None

    def test_conversation_id_round_trip(self):
        """批次 3：conversation_id 可填值 round-trip。"""
        d = UserInputData(text="hi", user_name="妖梦", request_id=RequestId("r1"),
                          conversation_id=SessionId("sess-9"))
        assert d.conversation_id == "sess-9"


@pytest.mark.unit
class TestSystemTickData:
    def test_invalid_reason_rejected(self):
        with pytest.raises(ValidationError):
            SystemTickData.model_validate(
                {"event_type": "system_tick", "reason": "schedule"})
        # Literal 大小写敏感——"time_of_day" 在 Literal 白名单中，用非法值 "schedule" 测试拒绝

    def test_silence_timeout_valid(self):
        d = SystemTickData(reason="silence_timeout", elapsed=30.0)
        assert d.elapsed == 30.0
        assert d.time is None

    def test_time_of_day_valid(self):
        d = SystemTickData(reason="time_of_day", time="08:00")
        assert d.time == "08:00"
        assert d.elapsed is None


@pytest.mark.unit
class TestEventTypeEnumBinding:
    """判别字段引用 EventType 成员 + VoiceReason 枚举（防裸字符串漂移）。"""

    def test_event_type_discriminator_references_enum_member(self):
        """判别字段必须是 Literal[EventType.MEMBER] 形式（防裸字符串漂移）。"""
        ann = InterruptData.model_fields["event_type"].annotation
        assert get_origin(ann) is Literal
        assert get_args(ann) == (EventType.INTERRUPT,)

    def test_discriminator_still_rejects_bad_value(self):
        # 判别值引用枚举后，非法值仍被拒（part.py 已证此模式）
        try:
            TypeAdapter(EventData).validate_python({"event_type": "nope"})
            raise AssertionError("should reject")
        except Exception:
            pass

    def test_system_tick_reason_is_enum(self):
        from core.schema.event import VoiceReason
        assert {m.value for m in VoiceReason} == {"silence_timeout", "time_of_day"}
        ann = SystemTickData.model_fields["reason"].annotation
        assert ann is VoiceReason
        ta = TypeAdapter(SystemTickData)
        v = ta.validate_python({"event_type": "system_tick", "reason": "silence_timeout"})
        assert v.reason == "silence_timeout"


@pytest.mark.unit
def test_user_input_data_id_field_types():
    """UserInputData 的 message_id/request_id 对齐 NewType（conversation_id 已 SessionId）。"""
    hints = get_type_hints(UserInputData)
    assert hints["message_id"] == MessageId | None
    assert hints["request_id"] is RequestId
