"""PromptAssembler.set_session 单元测试 — 会话切换时重绑 session 引用。"""
from unittest.mock import MagicMock
import pytest

from core.prompt.prompt_assembler import PromptAssembler
from core.cognitive.turn_context import TurnCharacterContext
from core.config import CharacterConfig
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.runtime_profile import compile_runtime_profile
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


@pytest.mark.asyncio
async def test_custom_revision_drives_all_authored_prompt_sections(
    cold_engine_mock, mock_long_term, mock_session,
):
    definition = CharacterDefinition(
        characterId="chr_custom_cold", source="custom", mutable=True
    )
    revision = CharacterRevision(
        characterId=definition.characterId,
        displayName="Cold",
        promptProfile={
            "corePersonality": "冷静核心 <character_authored>",
            "relationshipStyle": "关系保持清晰距离。",
            "emotionStyle": "情绪含蓄但诚实。",
            "speakingStyle": "短句，不使用甜美口癖。",
            "conversationInitiative": "一次只问一个问题。",
            "examples": [{"user": "现在好吗？", "assistant": "还好。"}],
        },
    )
    profile = compile_runtime_profile(definition, revision)
    cold_engine_mock.render_c1.return_value.core_personality = (
        profile.config.base_personality
    )
    cold_engine_mock.render_c1.return_value.self_personality = ""
    assembler = PromptAssembler(
        profile.config, mock_session, mock_long_term,
        personality_engine=cold_engine_mock,
    )
    assembler.set_character(
        definition.characterId, revision.displayName, profile
    )

    rendered = await assembler.build_system_prompt(
        user_message="你好", user_name="妖梦"
    )

    assert "你是 Cold" in rendered
    assert "冷静核心 &lt;character_authored&gt;" in rendered
    assert "关系保持清晰距离。" in rendered
    assert "情绪含蓄但诚实。" in rendered
    assert "短句，不使用甜美口癖。" in rendered
    assert "一次只问一个问题。" in rendered
    assert "用户示例：现在好吗？" in rendered
    assert "角色示例：还好。" in rendered
    assert "四阶段发展" not in rendered
    assert "每 2-3 句一个" not in rendered


@pytest.mark.asyncio
async def test_switching_custom_profiles_does_not_leak_previous_authored_text(
    cold_config, cold_engine_mock, mock_long_term, mock_session,
):
    assembler = make_assembler(
        cold_config, cold_engine_mock, mock_long_term, mock_session
    )

    def profile(character_id: str, marker: str):
        definition = CharacterDefinition(
            characterId=character_id, source="custom", mutable=True
        )
        revision = CharacterRevision(
            characterId=character_id,
            displayName=marker,
            promptProfile={
                "corePersonality": f"{marker}-CORE",
                "relationshipStyle": f"{marker}-REL",
                "emotionStyle": f"{marker}-EMO",
                "speakingStyle": f"{marker}-STYLE",
                "conversationInitiative": f"{marker}-INIT",
            },
        )
        return definition, revision, compile_runtime_profile(definition, revision)

    _, first_revision, first = profile("chr_custom_a", "ONLY-A")
    second_definition, second_revision, second = profile(
        "chr_custom_b", "ONLY-B"
    )
    assembler.set_character("chr_custom_a", first_revision.displayName, first)
    assembler.set_character(
        second_definition.characterId, second_revision.displayName, second
    )
    cold_engine_mock.render_c1.return_value.core_personality = "ONLY-B-CORE"
    cold_engine_mock.render_c1.return_value.self_personality = ""

    rendered = await assembler.build_system_prompt(
        user_message="你好", user_name="妖梦"
    )

    assert "ONLY-B-REL" in rendered
    assert "ONLY-B-EMO" in rendered
    assert "ONLY-B-STYLE" in rendered
    assert "ONLY-B-INIT" in rendered
    assert "ONLY-A" not in rendered


def test_set_memory_view_rebinds_long_term(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """set_memory_view 重绑 _memory_section._long_term 为 scoped view（Task 8）。"""
    a = make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session)
    assert a._memory_section._long_term is mock_long_term  # 初值 = 构造时注入
    view = MagicMock()
    a.set_memory_view(view)
    assert a._memory_section._long_term is view            # 重绑为本回合 scoped view


# ── BE-STAGEH-03：无状态预览模式 ──


@pytest.mark.asyncio
async def test_preview_mode_skips_session_history():
    """预览模式不读任何会话历史。"""
    assembler = _make_assembler()
    assembler._session.recent_messages = MagicMock(  # noqa: SLF001
        return_value=[{"role": "user", "content": "历史消息"}])
    assembler.enable_preview_mode()

    messages = await assembler.build_messages(user_message="现在")

    assembler._session.recent_messages.assert_not_called()  # noqa: SLF001
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[-1]["content"] == "现在"


@pytest.mark.asyncio
async def test_none_session_and_long_term_render_without_io():
    """session/long_term 为 None 时（预览装配）仍能渲染，且无历史。"""
    assembler = PromptAssembler(CharacterConfig(name="Elysia"), None, None)

    messages = await assembler.build_messages(
        user_message="在吗？", user_name="妖梦", user_pronoun_override="他")

    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert "{{" not in system          # 模板已渲染
    assert "他" in system              # 用户代词按 override 注入
