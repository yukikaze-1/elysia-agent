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
from core.personality import CharacterEngine
from core.schema.character import CharacterState
from core.schema.types import SessionId
from paths import ProjectPath

# integration 档标错：本文件全是 mock 渲染，不连任何 DB/网络，应为 service。
pytestmark = pytest.mark.service

# 模板目录绝对路径（基于 ProjectPath 项目根，不依赖 cwd）
_TEMPLATES = str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve())


def _mock_session(**params):
    """创建带有指定 params 的 SessionMemory。"""
    s = SessionMemory()
    # 映射旧 params 到新属性
    for k, v in params.items():
        if k == "mood":
            s.character_state.mood = v
        elif k == "own_life":
            s.own_life = v
        elif k == "mood_intensity":
            s.character_state.mood_intensity = float(v)
    return s


def _mock_long_term():
    """创建 mock LongTermMemory，search() 返回一条示例记忆。"""
    from core.memory.long_term import MemoryEntry
    lt = MagicMock()
    lt.search = AsyncMock(return_value=[
        MemoryEntry(text="妖梦喜欢吃草莓蛋糕", metadata={"type": "fact"})
    ])
    return lt


def _mock_character_engine():
    """返回 mock CharacterEngine 用于测试。"""
    from core.config import CharacterConfig
    state = CharacterState(session_id=SessionId("test-sid"))
    output = MagicMock()
    output.expression = MagicMock(return_value=MagicMock())
    engine = CharacterEngine(
        llm=MagicMock(),
        state=state,
        character_name="Elysia",
        user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。",
        character_config=CharacterConfig(name="Elysia"),
        output=output,
    )
    return engine


def test_full_render_all_sections():
    """12 个 section 全部注入数据，确认渲染正常。"""
    session = _mock_session(
        mood="warm",
        own_life="最近在追一部新番",
    )
    pe = _mock_character_engine()
    c1_data = pe.render_c1()

    personality_section = PersonalitySection(
        f"{_TEMPLATES}/c1_personality.j2",
        core_personality=c1_data.core_personality,
    )
    self_perception_section = SelfPerceptionSection(
        f"{_TEMPLATES}/c9_self_perception.j2",
    )

    sections = [
        ConflictRulesSection(f"{_TEMPLATES}/f1_conflict_rules.j2"),
        OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2"),
        TTSMarkupSection(f"{_TEMPLATES}/f3_tts_markup.j2"),
        SensorySection(f"{_TEMPLATES}/c5_sensory.j2"),
        EmotionSection(f"{_TEMPLATES}/c3_emotion.j2", session=session),
        MemorySection(f"{_TEMPLATES}/c4_memory.j2",
                      long_term=_mock_long_term(), session=session),
        personality_section,
        RelationshipSection(f"{_TEMPLATES}/c2_relationship.j2", "Elysia"),
        SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2"),
        ProactiveSection(f"{_TEMPLATES}/c7_proactive.j2"),
        OwnLifeSection(f"{_TEMPLATES}/c8_own_life.j2", session=session),
        self_perception_section,
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
    # 注入 personality data
    personality_section.update_personality(
        core=pe.render_c1().core_personality,
        self_p=pe.render_c1().self_personality,
    )
    self_perception_section.update_awareness("感觉自己变了")
    result = asyncio.run(mgr.render())
    assert "硬约束" in result          # F1
    assert "<inner_voice>" in result  # F2
    assert "<tts_markup>" in result   # F3
    assert "Elysia" in result         # C1
    assert "warm" in result           # C3 emotion
    assert "草莓蛋糕" in result        # C4 memory
    assert "新番" in result           # C8 own_life
    assert "self_perception" in result  # C9 self_perception
    assert "proactive" in result       # C7 proactive
    # 验证完整 12-section 排序（静态前缀 → 动态后缀）
    names_in_order = [
        # 静态前缀
        "conflict_rules", "inner_voice", "output_format", "tts_markup",
        "relationship", "proactive", "speaking_style", "personality",
        "self_perception", "own_life",
        # 动态后缀
        "emotion", "memory_bank", "sensory_input",
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
