"""SystemPromptManager 单元测试。"""
import asyncio
import pytest
from Module.prompt.protocol import PromptSection, StaticSection
from Module.prompt.system_prompt_manager import SystemPromptManager


def _make_section(name, text):
    return StaticSection(name, "1.0", f"desc {name}", text)


class TestSystemPromptManager:
    def test_renders_in_order(self):
        sections = [
            _make_section("Memory", "MEMORY"),
            _make_section("ConflictRules", "CONFLICT"),
            _make_section("Personality", "PERSONALITY"),
        ]
        mgr = SystemPromptManager(sections)
        result = asyncio.run(mgr.render())
        assert result == "CONFLICT\n\nMEMORY\n\nPERSONALITY"

    def test_empty_section_skipped(self):
        class EmptySection(PromptSection):
            name = "Empty"
            version = "1.0"
            description = "empty"
            async def render(self) -> str:
                return ""

        sections = [
            _make_section("ConflictRules", "F1"),
            EmptySection(),
            _make_section("Personality", "C1"),
        ]
        mgr = SystemPromptManager(sections)
        result = asyncio.run(mgr.render())
        assert result == "F1\n\nC1"

    def test_set_context_propagates(self):
        received = {}

        class ContextSection(PromptSection):
            name = "ctx"
            version = "1.0"
            description = "ctx"
            async def render(self) -> str:
                return str(received.get("key", "none"))
            def set_context(self, **kwargs) -> None:
                received.update(kwargs)

        mgr = SystemPromptManager([ContextSection()])
        mgr.set_context(key="value")
        result = asyncio.run(mgr.render())
        assert result == "value"

    def test_get_versions(self):
        sections = [_make_section("ConflictRules", "text"), _make_section("Memory", "text")]
        mgr = SystemPromptManager(sections)
        versions = mgr.get_versions()
        assert versions["ConflictRules"] == "1.0"
        assert versions["Memory"] == "1.0"

    def test_validate_all_passes(self):
        sections = [_make_section("a", "text"), _make_section("b", "text")]
        mgr = SystemPromptManager(sections)
        assert mgr.validate_all() == []
