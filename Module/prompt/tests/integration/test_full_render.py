"""System prompt 完整渲染集成测试。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

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
from core.memory.session import SessionMemory

_TEMPLATES = "Module/prompt/templates"


def _mock_session(**params):
    """创建带有指定 params 的 SessionMemory。"""
    s = SessionMemory()
    for k, v in params.items():
        s.set_param(k, v)
    return s


def _mock_long_term():
    """创建 mock LongTermMemory，search() 返回一条示例记忆。"""
    from core.memory.long_term import MemoryEntry
    lt = MagicMock()
    lt.search = AsyncMock(return_value=[
        MemoryEntry(text="妖梦喜欢吃草莓蛋糕", metadata={"type": "fact"})
    ])
    return lt


def test_full_render_all_sections():
    """11 个 section 全部注入数据，确认渲染正常。"""
    session = _mock_session(
        mood="warm",
        own_life="最近在追一部新番",
        self_perception_history=[
            {"content": "感觉自己变得更爱吐槽了"}
        ],
    )

    sections = [
        ConflictRulesSection(),
        OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2"),
        SensorySection(f"{_TEMPLATES}/c5_sensory.j2"),
        EmotionSection(f"{_TEMPLATES}/c3_emotion.j2", session=session),
        MemorySection(f"{_TEMPLATES}/c4_memory.j2",
                      long_term=_mock_long_term(), session=session),
        PersonalitySection(f"{_TEMPLATES}/c1_personality.j2", "Elysia"),
        RelationshipSection(f"{_TEMPLATES}/c2_relationship.j2", "Elysia"),
        SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2"),
        ProactiveSection(f"{_TEMPLATES}/c7_proactive.j2"),
        OwnLifeSection(f"{_TEMPLATES}/c8_own_life.j2", session=session),
        SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2", session=session),
    ]
    mgr = SystemPromptManager(sections)
    mgr.set_context(
        user_message="Hello",
        user_name="妖梦",
        user_pronoun="她",
        stt_emotion="",
        stt_events=[],
    )
    result = asyncio.run(mgr.render())
    assert "框架优先" in result       # F1
    assert "<inner_voice>" in result  # F2
    assert "Elysia" in result         # C1
    assert "warm" in result           # C3 emotion
    assert "草莓蛋糕" in result        # C4 memory
    assert "新番" in result           # C8 own_life
    assert "吐槽" in result           # C9 self_perception
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
        OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2"),
        PersonalitySection(f"{_TEMPLATES}/c1_personality.j2", "Elysia"),
    ]
    mgr = SystemPromptManager(sections)
    assert mgr.validate_all() == []
