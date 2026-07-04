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
        assistant_has_iv = any("<inner_voice>" in m["content"] for m in messages if m["role"] == "assistant")
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
