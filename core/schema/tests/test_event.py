"""core/schema/event.py 的 EventData union 模型测试。"""
import pytest
from pydantic import TypeAdapter, ValidationError

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
        d = UserInputData(text="hi", user_name="妖梦", request_id="r1",
                          emotion="happy", events=["laugh"])
        assert d.content is None

    def test_text_path_no_emotion_valid(self):
        d = UserInputData(text="hi", user_name="妖梦", request_id="r1",
                          content=[{"type": "text", "text": "hi"}])
        assert d.emotion == ""
        assert d.events == []

    def test_default_conversation_id_none(self):
        """批次 3：conversation_id 默认 None。"""
        d = UserInputData(text="hi", user_name="妖梦", request_id="r1")
        assert d.conversation_id is None

    def test_conversation_id_round_trip(self):
        """批次 3：conversation_id 可填值 round-trip。"""
        d = UserInputData(text="hi", user_name="妖梦", request_id="r1",
                          conversation_id="sess-9")
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
