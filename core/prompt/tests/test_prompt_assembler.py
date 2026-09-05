"""PromptAssembler.set_session 单元测试 — 会话切换时重绑 session 引用。"""
from unittest.mock import MagicMock
import pytest

from core.prompt.prompt_assembler import PromptAssembler
from core.cognitive.turn_context import TurnCharacterContext
from core.config import CharacterConfig
from core.memory.session import SessionMemory
from Module.system_prompt.sections.emotion import EmotionSection
from Module.system_prompt.sections.memory import MemorySection
from Module.system_prompt.sections.own_life import OwnLifeSection
from core.prompt.tests.conftest import cold_config, elysia_config, make_assembler, mock_long_term, mock_session  # noqa


def _make_assembler():
    """构造测试用 PromptAssembler（mock long_term，无需外部 I/O）。"""
    char = CharacterConfig(name="Elysia")
    return PromptAssembler(char, SessionMemory(), MagicMock())


def _session_dependent_sections(assembler):
    """返回依赖 session 的三个 section 实例。"""
    return [
        s for s in assembler._manager._sections
        if isinstance(s, (EmotionSection, OwnLifeSection, MemorySection))
    ]


@pytest.mark.unit
class TestPromptAssemblerSetSession:
    def test_set_session_rebinds_session(self):
        """set_session 后 _session 指向新会话。"""
        assembler = _make_assembler()
        new_session = SessionMemory()
        assembler.set_session(new_session)
        assert assembler._session is new_session

    def test_set_session_rebinds_session_dependent_sections(self):
        """三个 session 依赖 section 的 session 引用全部重绑。"""
        assembler = _make_assembler()
        new_session = SessionMemory()
        assembler.set_session(new_session)
        sections = _session_dependent_sections(assembler)
        assert len(sections) == 3
        for section in sections:
            assert section._session is new_session


def test_resolve_template_uses_role_dir(elysia_config, mock_long_term, mock_session):
    """角色名有角色目录时用 per-role 路径。"""
    from core.prompt.prompt_assembler import PromptAssembler
    a = PromptAssembler(elysia_config, mock_session, mock_long_term)
    path = a._resolve_template("c2_relationship.j2")
    assert "elysia" in path and path.endswith("c2_relationship.j2")


def test_resolve_template_falls_back_to_shared(cold_config, mock_long_term, mock_session):
    """无角色目录时回退共享路径。"""
    from core.prompt.prompt_assembler import PromptAssembler
    a = PromptAssembler(cold_config, mock_session, mock_long_term)
    path = a._resolve_template("c2_relationship.j2")
    assert "/templates/c2_relationship.j2" in path


@pytest.mark.asyncio
async def test_user_pronoun_from_turn_context_overrides(
    cold_config, cold_engine_mock, mock_long_term, mock_session
):
    """_turn_context.userPronoun=她 时，即使 _character.user_pronoun=你 也渲染她。

    CORRECTNESS #10：断言敏感 section（C2 第 18 行句式），非泛泛"她"。
    cold_config.user_pronoun="你"；context.userPronoun="她" → 渲染"此刻她只注视着我"。
    """
    assembler = make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session)
    assembler._turn_context = TurnCharacterContext(
        conversationId="c", characterInstanceId="chi", characterId="chr",
        characterRevision=1, userPronoun="她",
    )
    rendered = await assembler.build_system_prompt(user_message="hi", user_name="xx")
    assert "此刻她只注视着我" in rendered
    assert "此刻你只注视着我" not in rendered


@pytest.mark.asyncio
async def test_user_pronoun_no_context_fallback_she(
    cold_config, cold_engine_mock, mock_long_term, mock_session
):
    """无 _turn_context 时兜底常量"她"（非 cold_config.user_pronoun="你"）。

    spec §5.2：无 per-role 回退——cold 反例角色也应随全局正位改"她"。
    """
    assembler = make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session)
    rendered = await assembler.build_system_prompt(user_message="hi", user_name="xx")
    assert "此刻她只注视着我" in rendered
    assert "此刻你只注视着我" not in rendered


def test_set_personality_engine(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """set_personality_engine 设 self._personality_engine。"""
    a = make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session)
    a.set_personality_engine(cold_engine_mock)
    assert a._personality_engine is cold_engine_mock


def test_set_character_updates_name(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """set_character 更新 _personality_section._name(修正 review: 用真实 fixture + "Cyrene" 有区分度)。"""
    a = make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session)
    assert a._personality_section._name == "Elysia"   # 初始(elysia_config.name)
    a.set_character("chr_builtin_cyrene", "Cyrene")
    assert a._personality_section._name == "Cyrene"   # 有区分度——set_character 真改 name


def test_set_memory_view_rebinds_long_term(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """set_memory_view 重绑 _memory_section._long_term 为 scoped view（Task 8）。"""
    a = make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session)
    assert a._memory_section._long_term is mock_long_term  # 初值 = 构造时注入
    view = MagicMock()
    a.set_memory_view(view)
    assert a._memory_section._long_term is view            # 重绑为本回合 scoped view
