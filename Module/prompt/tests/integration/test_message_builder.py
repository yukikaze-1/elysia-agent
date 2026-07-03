"""MessageBuilder 集成测试。"""
from pathlib import Path

import pytest
from Module.prompt.message_builder import MessageBuilder
from Module.prompt.registry import BrainPromptRegistry

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent


class TestBuildSystemPrompt:
    def test_registry_path_renders(self):
        """Registry 可用时走 Registry 渲染。"""
        brain_dir = str(_PROJECT_ROOT / "prompt" / "brain")
        registry = BrainPromptRegistry(brain_dir)
        builder = MessageBuilder(registry=registry)
        context = {
            "name": "Elysia", "user_name": "妖梦",
            "user_preferences": "", "self_perception": "",
            "sensory_ctx": {
                "current_time": "2024-01-01 12:00:00",
                "time_of_day": "下午", "day_of_week": "周一", "season": "冬",
                "perception": "Hello", "stt_emotion": "",
                "stt_emotion_emoji": "", "stt_events": "",
            },
            "memory_ctx": {"micro": [], "macro": []},
            "mood": "neutral", "tool_list": "",
        }
        result = builder.build_system_prompt("default", context)
        assert "Elysia" in result
        assert "2024-01-01" in result

    def test_fallback_hardcoded(self):
        """无 Registry 无 fallback → hardcoded prompt。"""
        builder = MessageBuilder()
        result = builder.build_system_prompt("default", {})
        assert "AI伴侣" in result or "桌面" in result

    def test_fallback_brain_j2(self):
        """Registry None 但有 PromptManager → 走 Brain.j2 fallback。"""
        from Module.prompt import PromptManager
        prompt_dir = str(_PROJECT_ROOT / "prompt")
        fallback = PromptManager(prompt_dir)
        builder = MessageBuilder(fallback_prompt=fallback)
        context = {
            "name": "Elysia", "user_name": "妖梦",
            "user_preferences": "", "self_perception": "",
            "sensory_ctx": {
                "current_time": "2024-01-01 12:00:00",
                "time_of_day": "下午", "day_of_week": "周一", "season": "冬",
                "perception": "Hello", "stt_emotion": "",
                "stt_emotion_emoji": "", "stt_events": "",
            },
            "memory_ctx": {"micro": [], "macro": []},
            "mood": "neutral", "tool_list": "",
        }
        result = builder.build_system_prompt("default", context)
        assert "Elysia" in result


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
