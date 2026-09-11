"""Section 边界条件和错误路径单元测试。"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from Module.system_prompt.sections.emotion import EmotionSection
from Module.system_prompt.sections.memory import MemorySection
from Module.system_prompt.sections.sensory import SensorySection
from Module.system_prompt.sections.own_life import OwnLifeSection
from Module.system_prompt.sections.self_perception import SelfPerceptionSection
from Module.system_prompt.sections.personality import PersonalitySection
from Module.system_prompt.sections.relationship import RelationshipSection
from Module.system_prompt.sections.proactive import ProactiveSection
from Module.system_prompt.sections.conflict_rules import ConflictRulesSection
from Module.system_prompt.sections.speaking_style import SpeakingStyleSection, C6_L1_DEFAULT
from Module.system_prompt.sections.output_format import OutputFormatSection
from Module.system_prompt.sections.tts_markup import TTSMarkupSection
from core.memory.session import SessionMemory

pytestmark = pytest.mark.unit

_TEMPLATES = "Module/system_prompt/templates"


def _make_session(**params):
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


# ── EmotionSection ──

class TestEmotionSectionBoundary:
    def test_no_session_defaults_neutral(self):
        """session=None → mood 默认为 neutral。"""
        sec = EmotionSection(f"{_TEMPLATES}/c3_emotion.j2")
        sec.set_context(user_name="test", user_pronoun="她")
        result = asyncio.run(sec.render())
        assert "neutral" in result

    def test_session_mood_passed(self):
        """session 有 mood 值 → 渲染到模板。"""
        s = _make_session(mood="happy")
        sec = EmotionSection(f"{_TEMPLATES}/c3_emotion.j2", session=s)
        sec.set_context(user_name="test", user_pronoun="她")
        result = asyncio.run(sec.render())
        assert "happy" in result


# ── MemorySection ──

class TestMemorySectionBoundary:
    def test_no_long_term_returns_empty(self):
        """long_term=None → 返回 ""。"""
        sec = MemorySection(f"{_TEMPLATES}/c4_memory.j2")
        sec.set_context(user_message="hello")
        result = asyncio.run(sec.render())
        assert result == ""

    def test_empty_user_message_returns_empty(self):
        """user_message="" → 返回 ""。"""
        lt = MagicMock()
        sec = MemorySection(f"{_TEMPLATES}/c4_memory.j2", long_term=lt)
        result = asyncio.run(sec.render())
        assert result == ""

    def test_search_exception_returns_empty(self):
        """long_term.search() 抛异常 → 返回 ""。"""
        lt = MagicMock()
        lt.search = AsyncMock(side_effect=RuntimeError("db down"))
        sec = MemorySection(f"{_TEMPLATES}/c4_memory.j2", long_term=lt)
        sec.set_context(user_message="hello")
        result = asyncio.run(sec.render())
        assert result == ""

    def test_search_returns_empty_list(self):
        """search() 返回 [] → 返回 ""。"""
        lt = MagicMock()
        lt.search = AsyncMock(return_value=[])
        sec = MemorySection(f"{_TEMPLATES}/c4_memory.j2", long_term=lt)
        sec.set_context(user_message="hello")
        result = asyncio.run(sec.render())
        assert result == ""


def test_c4_receives_scoped_view():
    """C4 MemorySection 接收 per-turn scoped view（Task 8，阶段 F §2.4 C4）。

    stub 用 MagicMock——MemorySection 保持 duck-typed Any，不 import core.memory。
    search 签名兼容 ScopedLongTermMemory.search(query_text, top_k)；断言 _long_term
    即注入的 scoped view（PromptAssembler.set_memory_view 重绑的引用）。
    """
    view = MagicMock()
    view.search = AsyncMock(return_value=[])
    sec = MemorySection(f"{_TEMPLATES}/c4_memory.j2", long_term=view, session=None)
    assert sec._long_term is view  # noqa: SLF001 — 注入的 scoped view


# ── SensorySection ──

class TestSensorySectionBoundary:
    def test_time_boundaries(self):
        """_time_of_day 边界条件正确。"""
        assert SensorySection._time_of_day(5) == "早晨"
        assert SensorySection._time_of_day(11) == "早晨"
        assert SensorySection._time_of_day(12) == "中午"
        assert SensorySection._time_of_day(13) == "中午"
        assert SensorySection._time_of_day(14) == "下午"
        assert SensorySection._time_of_day(17) == "下午"
        assert SensorySection._time_of_day(18) == "傍晚"
        assert SensorySection._time_of_day(21) == "傍晚"
        assert SensorySection._time_of_day(22) == "夜晚"
        assert SensorySection._time_of_day(4) == "夜晚"

    def test_season_boundaries(self):
        """_season 边界条件正确。"""
        assert SensorySection._season(3) == "春"
        assert SensorySection._season(5) == "春"
        assert SensorySection._season(6) == "夏"
        assert SensorySection._season(8) == "夏"
        assert SensorySection._season(9) == "秋"
        assert SensorySection._season(11) == "秋"
        assert SensorySection._season(12) == "冬"
        assert SensorySection._season(2) == "冬"



# ── OwnLifeSection ──

class TestOwnLifeSectionBoundary:
    def test_no_session_returns_empty(self):
        """session=None → 返回 ""。"""
        sec = OwnLifeSection(f"{_TEMPLATES}/c8_own_life.j2")
        result = asyncio.run(sec.render())
        assert result == ""

    def test_empty_param_returns_empty(self):
        """own_life="" → 返回 ""。"""
        s = _make_session(own_life="")
        sec = OwnLifeSection(f"{_TEMPLATES}/c8_own_life.j2", session=s)
        result = asyncio.run(sec.render())
        assert result == ""


# ── SelfPerceptionSection ──

class TestSelfPerceptionSectionBoundary:
    def test_no_awareness_returns_empty(self):
        """awareness_text 未设置 → 返回 ""。"""
        sec = SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2")
        result = asyncio.run(sec.render())
        assert result == ""

    def test_with_awareness_renders(self):
        """awareness_text 有值 → 渲染内容。"""
        sec = SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2")
        sec.update_awareness("感觉自己变了")
        result = asyncio.run(sec.render())
        assert "感觉自己变了" in result
        assert "self_perception" in result


# ── Personality / Relationship / Proactive ──

class TestSectionSetContextDefaults:
    def test_personality_no_user_name(self):
        """set_context 不传 user_name → 默认 ""，core 文本渲染。"""
        sec = PersonalitySection(
            f"{_TEMPLATES}/c1_personality.j2",
            core_personality="你是 TestBot。测试人格。",
        )
        sec.set_context(user_pronoun="她")
        result = asyncio.run(sec.render())
        assert "TestBot" in result
        assert "测试人格" in result

    def test_relationship_no_user_name(self):
        """set_context 不传 user_name → 默认 ""，模板用空串渲染。"""
        sec = RelationshipSection(f"{_TEMPLATES}/c2_relationship.j2", "Elysia")
        sec.set_context(user_pronoun="她")
        result = asyncio.run(sec.render())
        assert "关于 " in result  # user_name 为空但结构完整

    def test_proactive_renders_content(self):
        """ProactiveSection 渲染主动性规则。"""
        sec = ProactiveSection(f"{_TEMPLATES}/c7_proactive.j2")
        sec.set_context(user_name="test")
        result = asyncio.run(sec.render())
        assert "proactive" in result
        assert "主动" in result


# ── ConflictRules ──

class TestConflictRules:
    def test_content(self):
        """F1 渲染冲突规则——框架硬约束优先于角色软约束。"""
        sec = ConflictRulesSection(f"{_TEMPLATES}/f1_conflict_rules.j2")
        result = asyncio.run(sec.render())
        assert "硬约束" in result
        assert "软约束" in result
        assert "<conflict_rules>" in result
        assert "框架规则" in result
        assert "角色定义" in result


class TestSpeakingStyleSection:
    def test_no_user_name_default(self):
        """set_context 不传 user_name → 默认 ""，模板可渲染。"""
        sec = SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2")
        result = asyncio.run(sec.render())
        assert "心里想的" in result
        assert "speaking_style" in result

    def test_with_user_name(self):
        """set_context 传入 user_name，模板正常渲染（v2 不引用 user_name）。"""
        sec = SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2")
        sec.set_context(user_name="测试用户")
        result = asyncio.run(sec.render())
        assert "speaking_style" in result
        assert "♪" in result


@pytest.mark.service
class TestSpeakingStyleSectionDynamic:
    def test_fallback_to_default_template(self):
        """speaking_style_c6 为空时渲染 L1 默认模板。"""
        sec = SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2")
        sec.set_context(user_name="妖梦", speaking_style_c6="")
        result = asyncio.run(sec.render())
        assert "五模式切换" in result          # L1 默认内容
        assert "轻松模式" in result

    def test_render_dynamic_text(self):
        """speaking_style_c6 非空时渲染动态文本。"""
        sec = SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2")
        sec.set_context(user_name="妖梦", speaking_style_c6="自定义说话风格文本")
        result = asyncio.run(sec.render())
        assert "自定义说话风格文本" in result
        assert "五模式切换" not in result     # 不使用 L1 默认


def test_speaking_style_accepts_param():
    """构造器接受 speaking_style 参数（签名验证，不测私有属性）。"""
    sec = SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2", speaking_style="x")
    result = asyncio.run(sec.render())   # 构造 + render 不抛错即可（旧模板忽略该变量）
    assert isinstance(result, str)


def test_speaking_style_bare_construct_renders():
    """裸构造（不传 speaking_style）仍可渲染（旧模板忽略变量，走 else）。"""
    sec = SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2")
    result = asyncio.run(sec.render())
    assert "五模式切换" in result   # 旧模板 else 分支内容（Task 1 未改模板）


def test_c6_way_b_renders_l1_default():
    """方式 B：speaking_style=C6_L1_DEFAULT 渲染 L1 默认（裸构造）。"""
    sec = SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2")   # 裸构造 → 回退常量
    result = asyncio.run(sec.render())
    assert "五模式切换" in result
    assert "明快" in result


def test_c6_way_b_speaking_style_empty_disappears():
    """方式 B：speaking_style="" 且 speaking_style_c6="" → C6 段真消失。"""
    sec = SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2", speaking_style="")
    sec.set_context(user_name="", speaking_style_c6="")
    result = asyncio.run(sec.render())
    assert result == ""


class TestOutputFormatSection:
    def test_renders_protocol(self):
        """F2 inner_voice 协议正常渲染。"""
        sec = OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2")
        result = asyncio.run(sec.render())
        assert "<inner_voice>" in result
        assert "必须闭合" in result

    def test_validate_passes(self):
        """validate 对正常模板返回 True。"""
        sec = OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2")
        assert sec.validate() is True


class TestTTSMarkupSection:
    def test_renders_minimax(self):
        """tts_provider=minimax → 渲染 MiniMax TTS 标记。"""
        sec = TTSMarkupSection(f"{_TEMPLATES}/f3_tts_markup.j2")
        sec.set_context(tts_provider="minimax")
        result = asyncio.run(sec.render())
        assert "<tts_markup>" in result
        assert "语音合成标记" in result
        assert "(laughs)" in result

    def test_renders_gpt_sovits(self):
        """tts_provider=gpt-sovits → 渲染纯文本指令。"""
        sec = TTSMarkupSection(f"{_TEMPLATES}/f3_tts_markup.j2")
        sec.set_context(tts_provider="gpt-sovits")
        result = asyncio.run(sec.render())
        assert "<tts_markup>" in result
        assert "纯文本" in result

    def test_empty_provider_renders_empty(self):
        """tts_provider 为空或不匹配 → 只输出包裹标签。"""
        sec = TTSMarkupSection(f"{_TEMPLATES}/f3_tts_markup.j2")
        result = asyncio.run(sec.render())
        assert "<tts_markup>" in result
