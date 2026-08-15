"""PromptAssembler.set_session 单元测试 — 会话切换时重绑 session 引用。"""
from unittest.mock import MagicMock
import pytest

from core.prompt.prompt_assembler import PromptAssembler
from core.config import CharacterConfig
from core.memory.session import SessionMemory
from Module.system_prompt.sections.emotion import EmotionSection
from Module.system_prompt.sections.memory import MemorySection
from Module.system_prompt.sections.own_life import OwnLifeSection


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
