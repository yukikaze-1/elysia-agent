"""SystemPromptManager 单元测试。"""
import asyncio
import pytest
from Module.prompt.protocol import PromptSection, StaticSection
from Module.prompt.system_prompt_manager import SystemPromptManager, _RENDER_ORDER


def _make_section(name, text):
    return StaticSection(name, "1.0", f"desc {name}", text)


class TestSystemPromptManager:
    def test_renders_in_order(self):
        """用全部 11 个 section 名验证排序。"""
        sections = [_make_section(name, name) for name in _RENDER_ORDER]
        # 打乱输入顺序
        shuffled = sections[::-1]
        mgr = SystemPromptManager(shuffled)
        result = asyncio.run(mgr.render())
        expected = "\n\n".join(_RENDER_ORDER)
        assert result == expected

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

    def test_reload_all(self):
        """reload_all() 遍历所有 section 调用 reload。"""
        calls = []

        class ReloadSection(PromptSection):
            name = "Reload"
            version = "1.0"
            description = "reload"
            async def render(self) -> str:
                return "x"
            def reload(self) -> None:
                calls.append(self.name)

        s1 = ReloadSection()
        s2 = ReloadSection()
        mgr = SystemPromptManager([s1, s2])
        asyncio.run(mgr.reload_all())
        assert len(calls) == 2
