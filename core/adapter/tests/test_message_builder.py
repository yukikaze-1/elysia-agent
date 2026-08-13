"""MessageBuilder 集成测试。"""
import pytest
from core.adapter.message_builder import MessageBuilder
from core.schema.message import UserMessage, AssistantMessage, ToolResultMessage, MessageId
from core.schema.part import TextPart, ImagePart
from core.schema.tool import ToolCall, ToolCallFunction

pytestmark = pytest.mark.integration


def _make_user_msg(content: str = "Hi", msg_id: str = "u1") -> UserMessage:
    return UserMessage(content=[TextPart(content=content)], message_id=MessageId(msg_id))


def _make_assistant_msg(
    content: str = "Hello",
    msg_id: str = "a1",
    inner_voice: str | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=[TextPart(content=content)],
        message_id=MessageId(msg_id),
        inner_voice=inner_voice,
        tool_calls=tool_calls,
    )


def _make_user_msg_multimodal(parts: list) -> UserMessage:
    return UserMessage(content=parts, message_id=MessageId("um"))


class TestBuildMessages:
    def test_basic_assembly(self):
        """基本 messages 组装：system + history + user。"""
        builder = MessageBuilder()
        system = "You are a helpful assistant."
        history = [
            _make_user_msg("Hi"),
            _make_assistant_msg("Hello!", inner_voice="someone said hi"),
        ]
        messages = builder.build_messages(system, history, "How are you?")
        assert messages[0] == {"role": "system", "content": system}
        assert len(messages) >= 3
        assistant_has_iv = any(
            "<inner_voice>" in m.get("content", "") for m in messages if m["role"] == "assistant"
        )
        assert assistant_has_iv

    def test_tool_message_skipped(self):
        """工具结果消息被跳过。"""
        builder = MessageBuilder()
        history = [
            _make_assistant_msg(
                "let me check",
                tool_calls=[ToolCall(
                    id="c1",
                    function=ToolCallFunction(name="web_search", arguments='{"query":"test"}'),
                )],
            ),
            ToolResultMessage(
                content=[TextPart(content="search results here")],
                tool_call_id="c1", tool_name="web_search",
                message_id=MessageId("msg_test_t1"),
            ),
            _make_assistant_msg("found it"),
        ]
        messages = builder.build_messages("system", history, "thanks")
        roles = [m["role"] for m in messages]
        assert "tool" not in roles

    def test_multimodal_content_list_supported(self):
        """content 为 list（image 类型）且 supports_multimodal=True 时正常传递。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "content": "look at this"},
            {"type": "image", "src": "data:image/png;base64,xxx"},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=True)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert isinstance(user_msg["content"], list)

    def test_multimodal_unsupported_image_fallback(self):
        """content 为 list + 有 image 类型 + supports_multimodal=False → describe_image 提示。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "content": "check this"},
            {"type": "image", "src": "http://a.com/1.png"},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=False)
        user_content = messages[-1]["content"]
        assert "describe_image" in user_content

    def test_tool_summary_injected(self):
        """tool_calls → _format_tool_summary 注入到 assistant 消息。"""
        builder = MessageBuilder()
        history = [
            _make_assistant_msg(
                "let me search",
                tool_calls=[ToolCall(
                    id="c1",
                    function=ToolCallFunction(name="search", arguments='{"q":"test"}'),
                )],
            ),
            ToolResultMessage(
                content=[TextPart(content="result: found")],
                tool_call_id="c1", tool_name="search",
                message_id=MessageId("msg_test_t2"),
            ),
        ]
        messages = builder.build_messages("system", history, "ok")
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        has_tool_summary = any("search" in m.get("content", "") for m in assistant_msgs)
        assert has_tool_summary

    def test_inner_voice_with_str_content(self):
        """inner_voice 注入：字符串 content + iv → 包含 <inner_voice>。"""
        builder = MessageBuilder()
        history = [
            _make_assistant_msg("Hello", inner_voice="thinking about greeting"),
        ]
        messages = builder.build_messages("system", history, "bye")
        assistant = [m for m in messages if m["role"] == "assistant"][0]
        assert "<inner_voice>thinking about greeting</inner_voice>" in assistant["content"]

    def test_format_tool_summary_json_parse_fails(self):
        """_format_tool_summary JSON 解析失败时不崩，返回 name 基本字符串。"""
        builder = MessageBuilder()
        history = [
            _make_assistant_msg(
                "",
                tool_calls=[ToolCall(
                    id="c1",
                    function=ToolCallFunction(name="test", arguments="not-json"),
                )],
            ),
            ToolResultMessage(
                content=[TextPart(content="ok")],
                tool_call_id="c1", tool_name="test",
                message_id=MessageId("msg_test_t3"),
            ),
        ]
        messages = builder.build_messages("system", history, "done")
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant) > 0
        # JSON 解析失败时只输出工具名，不含参数
        assert "[已调用: test]" in assistant[0]["content"]

    def test_single_image_unsupported(self):
        """单张图片 + 不支持多模态 → describe_image 提示。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "content": "hello"},
            {"type": "image", "src": "http://a.com/1.png"},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=False)
        assert "describe_image" in messages[-1]["content"]

    def test_no_user_content(self):
        """user_content 为 "" → 不添加 user message。"""
        builder = MessageBuilder()
        messages = builder.build_messages("system", [_make_user_msg("hi")], "")
        assert len(messages) == 2

    def test_history_image_count_multiple(self):
        """历史消息中有多张图片 + 不支持多模态 → 数量提示。"""
        builder = MessageBuilder()
        history = [
            _make_user_msg_multimodal([
                ImagePart(src="http://a.com/1.png"),
                ImagePart(src="http://a.com/2.png"),
            ]),
            _make_assistant_msg("nice pics"),
        ]
        messages = builder.build_messages("system", history, "thanks", supports_multimodal=False)
        user_msgs = [m for m in messages if m["role"] == "user"]
        two_image_msg = [m for m in user_msgs if "2张" in m.get("content", "")]
        assert len(two_image_msg) > 0
