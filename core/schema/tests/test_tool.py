# 测试文件：core/schema/tests/test_tool.py
"""core/schema/tool.py 测试。"""
import pytest
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId


@pytest.mark.unit
class TestToolCall:
    """ToolCall 构造和序列化。"""

    def test_minimal_construction(self):
        tc = ToolCall(id=ToolCallId("call_001"))
        assert tc.id == "call_001"
        assert tc.type == "function"
        assert tc.function.name == ""
        assert tc.function.arguments == ""

    def test_full_construction(self):
        tc = ToolCall(
            id=ToolCallId("call_002"),
            function=ToolCallFunction(name="web_search", arguments='{"query":"test"}'),
        )
        assert tc.function.name == "web_search"
        assert tc.function.arguments == '{"query":"test"}'

    def test_model_dump(self):
        tc = ToolCall(
            id=ToolCallId("call_003"),
            function=ToolCallFunction(name="read_file", arguments='{"path":"/tmp/x"}'),
        )
        d = tc.model_dump()
        assert d["id"] == "call_003"
        assert d["type"] == "function"
        assert d["function"]["name"] == "read_file"
        assert d["function"]["arguments"] == '{"path":"/tmp/x"}'

    def test_model_validate_from_openai_format(self):
        """从 OpenAI API 格式的 dict 反序列化。"""
        d = {
            "id": "call_004",
            "type": "function",
            "function": {"name": "get_time", "arguments": '{"timezone":"UTC"}'},
        }
        tc = ToolCall.model_validate(d)
        assert tc.id == "call_004"
        assert tc.function.name == "get_time"
