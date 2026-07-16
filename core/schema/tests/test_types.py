"""core/schema/types.py 测试。"""
import pytest
from datetime import datetime, timezone
from pydantic import BaseModel, ValidationError
from core.schema import IsoTimestamp, now_iso, SessionId, MessageId, ToolCallId


@pytest.mark.unit
class TestIsoTimestamp:
    """IsoTimestamp 校验和 now_iso 测试。"""

    def test_valid_iso_passes(self):
        """合法 ISO 8601 通过校验。"""

        class M(BaseModel):
            ts: IsoTimestamp

        m = M(ts="2026-07-15T12:00:00+00:00")
        assert "2026-07-15T12:00:00+00:00" in m.ts

    def test_missing_tz_gets_utc(self):
        """缺时区自动补 UTC。"""

        class M(BaseModel):
            ts: IsoTimestamp

        m = M(ts="2026-07-15T12:00:00")
        assert m.ts.endswith("+00:00")

    def test_empty_string_rejected(self):
        """空字符串被拒绝。"""

        class M(BaseModel):
            ts: IsoTimestamp

        with pytest.raises(ValidationError):
            M(ts="")

    def test_now_iso_returns_valid(self):
        """now_iso() 返回合法 ISO 8601。"""
        ts = now_iso()
        assert isinstance(ts, str)
        assert "T" in ts
        # 应该能 round-trip
        datetime.fromisoformat(ts)


@pytest.mark.unit
class TestNewTypes:
    """NewType 别名行为测试。"""

    def test_session_id_is_str(self):
        """SessionId 是 str 的子类型。"""
        sid = SessionId("test-session")
        assert isinstance(sid, str)
        assert sid == "test-session"

    def test_message_id_is_str(self):
        mid = MessageId("msg-001")
        assert isinstance(mid, str)

    def test_tool_call_id_is_str(self):
        tcid = ToolCallId("call-001")
        assert isinstance(tcid, str)
