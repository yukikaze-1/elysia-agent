"""System prompt 完整渲染集成测试。"""
import asyncio
import pytest
from Module.prompt.sections.conflict_rules import ConflictRulesSection
from Module.prompt.sections.output_format import OutputFormatSection
from Module.prompt.sections.sensory import SensorySection
from Module.prompt.sections.emotion import EmotionSection
from Module.prompt.sections.memory import MemorySection
from Module.prompt.sections.personality import PersonalitySection
from Module.prompt.sections.relationship import RelationshipSection
from Module.prompt.sections.speaking_style import SpeakingStyleSection
from Module.prompt.sections.proactive import ProactiveSection
from Module.prompt.sections.own_life import OwnLifeSection
from Module.prompt.sections.self_perception import SelfPerceptionSection
from Module.prompt.system_prompt_manager import SystemPromptManager

_SECTION = "Module/prompt/templates"


def test_full_render_all_sections():
    """11 个 section 全部参与渲染，不报错。"""
    sections = [
        ConflictRulesSection(),
        OutputFormatSection(f"{_SECTION}/f2_output_format.j2"),
        SensorySection(f"{_SECTION}/c5_sensory.j2"),
        EmotionSection(f"{_SECTION}/c3_emotion.j2"),
        MemorySection(f"{_SECTION}/c4_memory.j2"),
        PersonalitySection(f"{_SECTION}/c1_personality.j2", "Elysia"),
        RelationshipSection(f"{_SECTION}/c2_relationship.j2", "Elysia"),
        SpeakingStyleSection(f"{_SECTION}/c6_speaking_style.j2"),
        ProactiveSection(f"{_SECTION}/c7_proactive.j2"),
        OwnLifeSection(f"{_SECTION}/c8_own_life.j2"),
        SelfPerceptionSection(f"{_SECTION}/c9_self_perception.j2"),
    ]
    mgr = SystemPromptManager(sections)
    mgr.set_context(
        user_message="Hello",
        user_name="妖梦",
        stt_emotion="",
        stt_events=[],
    )
    result = asyncio.run(mgr.render())
    assert "框架优先" in result       # F1
    assert "<inner_voice>" in result  # F2
    assert "Elysia" in result         # C1
    pos_f1 = result.find("框架优先")
    pos_f2 = result.find("<inner_voice>")
    assert pos_f1 < pos_f2


def test_versions_all_sections():
    """所有 section 都有 version。"""
    sections = [ConflictRulesSection()]
    mgr = SystemPromptManager(sections)
    versions = mgr.get_versions()
    assert versions["ConflictRules"] == "1.0.0"


def test_validate_all_passes():
    """所有模板 section 的 validate 都通过。"""
    sections = [
        ConflictRulesSection(),
        OutputFormatSection(f"{_SECTION}/f2_output_format.j2"),
        PersonalitySection(f"{_SECTION}/c1_personality.j2", "Elysia"),
    ]
    mgr = SystemPromptManager(sections)
    assert mgr.validate_all() == []
