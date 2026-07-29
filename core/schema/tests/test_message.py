# core/schema/tests/test_message.py
"""core/schema/message.py 测试。"""
import pytest
from pydantic import ValidationError
from core.schema.message import MessageRole, UserMessage, AssistantMessage, ToolResultMessage
from core.schema.part import TextPart
from core.schema.types import MessageId, ToolCallId
from Module.tool.protocol import ErrorType


@pytest.mark.unit
class TestMessageRole:
    """MessageRole 枚举。"""

    def test_roles(self):
        assert MessageRole.USER == "user"
        assert MessageRole.ASSISTANT == "assistant"
        assert MessageRole.TOOL == "tool"
        assert MessageRole.SYSTEM == "system"


@pytest.mark.unit
class TestUserMessage:
    """UserMessage 构造和校验。"""

    def test_minimal_construction(self):
        msg = UserMessage(
            content=[TextPart(content="hello")],
            message_id=MessageId("msg-001"),
        )
        assert msg.role == MessageRole.USER
        assert len(msg.content) == 1
        assert msg.content[0].content == "hello"  # type: ignore[reportAttributeAccessIssue]
        assert msg.timestamp  # now_iso 自动生成

    def test_role_is_frozen(self):
        msg = UserMessage(
            content=[TextPart(content="test")],
            message_id=MessageId("msg-002"),
        )
        with pytest.raises(ValidationError):
            msg.role = MessageRole.ASSISTANT  # frozen

    def test_model_validate_from_dict(self):
        d = {
            "content": [{"type": "text", "content": "hello world"}],
            "message_id": "msg-003",
            "timestamp": "2026-07-15T12:00:00+00:00",
        }
        msg = UserMessage.model_validate(d)
        assert msg.role == MessageRole.USER  # auto-filled
        assert msg.content[0].content == "hello world"  # type: ignore[reportAttributeAccessIssue]
        assert isinstance(msg.message_id, str)
        assert msg.message_id == "msg-003"

    def test_content_part_union_dispatch(self):
        """content 中混合 Part dict 按 type 正确分发 + 未知 type 拒绝。"""
        from core.schema.part import ImagePart
        from pydantic import ValidationError

        msg = UserMessage.model_validate({
            "content": [
                {"type": "text", "content": "看这张图"},
                {"type": "image", "src": "https://example.com/i.png"},
            ],
            "message_id": "msg-007",
        })
        assert isinstance(msg.content[0], TextPart)
        assert isinstance(msg.content[1], ImagePart)

        with pytest.raises(ValidationError):
            UserMessage.model_validate({
                "content": [{"type": "hologram"}],
                "message_id": "msg-008",
            })


@pytest.mark.unit
class TestAssistantMessage:
    """AssistantMessage 构造和校验。"""

    def test_minimal_construction(self):
        msg = AssistantMessage(
            content=[TextPart(content="你好！")],
            message_id=MessageId("msg-004"),
        )
        assert msg.role == MessageRole.ASSISTANT
        assert msg.tool_calls is None
        assert msg.inner_voice is None

    def test_with_tool_calls(self):
        """带 tool_calls 的 assistant 消息。"""
        from core.schema.tool import ToolCall, ToolCallFunction
        from core.schema.types import ToolCallId

        msg = AssistantMessage(
            content=[],
            message_id=MessageId("msg-005"),
            tool_calls=[
                ToolCall(
                    id=ToolCallId("call_001"),
                    function=ToolCallFunction(name="web_search", arguments='{"q":"test"}'),
                )
            ],
        )
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].function.name == "web_search"

    def test_block_index(self):
        msg = AssistantMessage(
            content=[TextPart(content="x")],
            message_id=MessageId("msg-006"),
            block_index={"markdown": [0], "code": []},
        )
        assert msg.block_index == {"markdown": [0], "code": []}


@pytest.mark.unit
class TestToolResultMessage:
    """ToolResultMessage 构造和校验。"""

    def test_success_result(self):
        msg = ToolResultMessage(
            content=[TextPart(content="done")],
            tool_call_id=ToolCallId("call_001"),
            tool_name="web_search",
            success=True,
            duration_ms=1234.5,
        )
        assert msg.role == MessageRole.TOOL
        assert msg.success is True
        assert msg.error is None
        assert msg.error_type is None
        assert msg.duration_ms == 1234.5

    def test_error_result(self):
        msg = ToolResultMessage(
            content=[TextPart(content="timeout")],
            tool_call_id=ToolCallId("call_002"),
            tool_name="web_search",
            success=False,
            error="request timed out",
            error_type=ErrorType.TIMEOUT,
        )
        assert msg.success is False
        assert msg.error == "request timed out"
        assert msg.error_type == ErrorType.TIMEOUT
