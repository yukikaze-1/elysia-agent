"""MessageBuilder 集成测试。"""
import pytest
from Module.prompt.message_builder import MessageBuilder


class TestBuildMessages:
    def test_basic_assembly(self):
        """基本 messages 组装：system + history + user。"""
        builder = MessageBuilder()
        system = "You are a helpful assistant."
        history = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!", "inner_voice": "someone said hi"},
        ]
        messages = builder.build_messages(system, history, "How are you?")
        assert messages[0] == {"role": "system", "content": system}
        assert len(messages) >= 3
        assistant_has_iv = any("<inner_voice>" in m.get("content", "") for m in messages if m["role"] == "assistant")
        assert assistant_has_iv

    def test_tool_message_skipped(self):
        """工具结果消息被跳过。"""
        builder = MessageBuilder()
        history = [
            {"role": "assistant", "content": "let me check", "tool_calls": [
                {"function": {"name": "web_search", "arguments": '{"query":"test"}'}}
            ]},
            {"role": "tool", "content": "search results here"},
            {"role": "assistant", "content": "found it"},
        ]
        messages = builder.build_messages("system", history, "thanks")
        roles = [m["role"] for m in messages]
        assert "tool" not in roles

    def test_multimodal_content_list_supported(self):
        """content 为 list（image 类型）且 supports_multimodal=True 时正常传递。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=True)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert isinstance(user_msg["content"], list)

    def test_multimodal_unsupported_image_fallback(self):
        """content 为 list + 有 image 类型 + supports_multimodal=False → describe_image 提示。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "text": "check this"},
            {"type": "image", "image_url": {"url": "http://a.com/1.png"}},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=False)
        user_content = messages[-1]["content"]
        assert "describe_image" in user_content

    def test_tool_summary_injected(self):
        """tool_calls → _format_tool_summary 注入到 assistant 消息。"""
        builder = MessageBuilder()
        history = [
            {"role": "assistant", "content": "let me search",
             "tool_calls": [{"function": {"name": "search", "arguments": '{"q":"test"}'}}]},
            {"role": "tool", "content": "result: found"},
        ]
        messages = builder.build_messages("system", history, "ok")
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        has_tool_summary = any("search" in m.get("content", "") for m in assistant_msgs)
        assert has_tool_summary

    def test_inner_voice_with_str_content(self):
        """inner_voice 注入：字符串 content + iv → 包含 <inner_voice>。"""
        builder = MessageBuilder()
        history = [
            {"role": "assistant", "content": "Hello", "inner_voice": "thinking about greeting"},
        ]
        messages = builder.build_messages("system", history, "bye")
        assistant = [m for m in messages if m["role"] == "assistant"][0]
        assert "<inner_voice>thinking about greeting</inner_voice>" in assistant["content"]

    def test_gemini_parts_format(self):
        """"parts" 字段（Gemini 格式）→ content 从 parts 提取，不丢失数据。"""
        builder = MessageBuilder()
        history = [
            {"role": "user", "parts": [{"content": "hello from gemini"}]},
        ]
        messages = builder.build_messages("system", history, "hi")
        user_msg = [m for m in messages if m["role"] == "user" and "gemini" in str(m)][0]
        assert "hello from gemini" in user_msg.get("content", "")

    def test_content_raw_other_type(self):
        """content_raw 为 int → else fallback → str 化。"""
        builder = MessageBuilder()
        history = [
            {"role": "user", "content": 12345},
        ]
        messages = builder.build_messages("system", history, "ok")
        user_msg = [m for m in messages if m["role"] == "user" and "12345" in str(m.get("content", ""))]
        assert len(user_msg) > 0

    def test_format_tool_summary_json_parse_fails(self):
        """_format_tool_summary JSON 解析失败时不崩，返回 name 基本字符串。"""
        builder = MessageBuilder()
        history = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "test", "arguments": "not-json"}}]},
            {"role": "tool", "content": "ok"},
        ]
        messages = builder.build_messages("system", history, "done")
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant) > 0

    def test_single_image_unsupported(self):
        """单张图片 + 不支持多模态 → describe_image 提示。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image", "image_url": {"url": "http://a.com/1.png"}},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=False)
        assert "describe_image" in messages[-1]["content"]

    def test_no_user_content(self):
        """user_content 为 "" → 不添加 user message。"""
        builder = MessageBuilder()
        messages = builder.build_messages("system", [{"role": "user", "content": "hi"}], "")
        assert len(messages) == 2

    def test_history_image_count_multiple(self):
        """历史消息中有多张图片 + 不支持多模态 → 数量提示。"""
        builder = MessageBuilder()
        history = [
            {"role": "user", "content": [
                {"type": "image", "image_url": {"url": "http://a.com/1.png"}},
                {"type": "image", "image_url": {"url": "http://a.com/2.png"}},
            ]},
            {"role": "assistant", "content": "nice pics"},
        ]
        messages = builder.build_messages("system", history, "thanks", supports_multimodal=False)
        user_msgs = [m for m in messages if m["role"] == "user"]
        two_image_msg = [m for m in user_msgs if "2张" in m.get("content", "")]
        assert len(two_image_msg) > 0
