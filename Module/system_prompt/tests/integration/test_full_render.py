"""System prompt 完整渲染集成测试。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from Module.system_prompt.sections.conflict_rules import ConflictRulesSection
from Module.system_prompt.sections.output_format import OutputFormatSection
from Module.system_prompt.sections.tts_markup import TTSMarkupSection
from Module.system_prompt.sections.sensory import SensorySection
from Module.system_prompt.sections.emotion import EmotionSection
from Module.system_prompt.sections.memory import MemorySection
from Module.system_prompt.sections.personality import PersonalitySection
from Module.system_prompt.sections.relationship import RelationshipSection
from Module.system_prompt.sections.speaking_style import SpeakingStyleSection
from Module.system_prompt.sections.proactive import ProactiveSection
from Module.system_prompt.sections.own_life import OwnLifeSection
from Module.system_prompt.sections.self_perception import SelfPerceptionSection
from Module.system_prompt.system_prompt_manager import SystemPromptManager
from core.memory.session import SessionMemory

_TEMPLATES = "Module/system_prompt/templates"


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
    """12 个 section 全部注入数据，确认渲染正常。"""
    session = _mock_session(
        mood="warm",
        own_life="最近在追一部新番",
        self_perception_history=[
            {"content": "感觉自己变得更爱吐槽了"}
        ],
    )

    sections = [
        ConflictRulesSection(f"{_TEMPLATES}/f1_conflict_rules.j2"),
        OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2"),
        TTSMarkupSection(f"{_TEMPLATES}/f3_tts_markup.j2"),
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
        tts_provider="minimax",
    )
    result = asyncio.run(mgr.render())
    assert "硬约束" in result          # F1
    assert "<inner_voice>" in result  # F2
    assert "<tts_markup>" in result   # F3
    assert "Elysia" in result         # C1
    assert "warm" in result           # C3 emotion
    assert "草莓蛋糕" in result        # C4 memory
    assert "新番" in result           # C8 own_life
    assert "吐槽" in result           # C9 self_perception
    assert "# 主动性" in result        # C7 proactive
    # 验证完整 12-section 排序
    names_in_order = [
        "conflict_rules", "inner_voice", "output_format", "tts_markup",
        "sensory_input", "emotion", "memory_bank", "personality",
        "relationship", "speaking_style", "主动性", "own_life",
        "self_perception",
    ]
    last_pos = -1
    for tag in names_in_order:
        pos = result.find(tag)
        assert pos > last_pos, f"section order broken: {tag} not after previous (pos={pos}, last={last_pos})"
        last_pos = pos


def test_versions_all_sections():
    """全部 12 个 section 都有 version。"""
    sections = [
        ConflictRulesSection(f"{_TEMPLATES}/f1_conflict_rules.j2"),
        OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2"),
        TTSMarkupSection(f"{_TEMPLATES}/f3_tts_markup.j2"),
        SensorySection(f"{_TEMPLATES}/c5_sensory.j2"),
        EmotionSection(f"{_TEMPLATES}/c3_emotion.j2"),
        MemorySection(f"{_TEMPLATES}/c4_memory.j2"),
        PersonalitySection(f"{_TEMPLATES}/c1_personality.j2", "Elysia"),
        RelationshipSection(f"{_TEMPLATES}/c2_relationship.j2", "Elysia"),
        SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2"),
        ProactiveSection(f"{_TEMPLATES}/c7_proactive.j2"),
        OwnLifeSection(f"{_TEMPLATES}/c8_own_life.j2"),
        SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2"),
    ]
    mgr = SystemPromptManager(sections)
    versions = mgr.get_versions()
    assert len(versions) == 12
    for name, ver in versions.items():
        assert ver, f"{name} version is empty"
        assert ver != "0.0.0", f"{name} version is default 0.0.0"


def test_validate_all_passes():
    """全部 12 个 section 的 validate 都通过。"""
    sections = [
        ConflictRulesSection(f"{_TEMPLATES}/f1_conflict_rules.j2"),
        OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2"),
        TTSMarkupSection(f"{_TEMPLATES}/f3_tts_markup.j2"),
        SensorySection(f"{_TEMPLATES}/c5_sensory.j2"),
        EmotionSection(f"{_TEMPLATES}/c3_emotion.j2"),
        MemorySection(f"{_TEMPLATES}/c4_memory.j2"),
        PersonalitySection(f"{_TEMPLATES}/c1_personality.j2", "Elysia"),
        RelationshipSection(f"{_TEMPLATES}/c2_relationship.j2", "Elysia"),
        SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2"),
        ProactiveSection(f"{_TEMPLATES}/c7_proactive.j2"),
        OwnLifeSection(f"{_TEMPLATES}/c8_own_life.j2"),
        SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2"),
    ]
    mgr = SystemPromptManager(sections)
    assert mgr.validate_all() == []
