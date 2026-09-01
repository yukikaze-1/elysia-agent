# core/prompt/tests/conftest.py
"""多角色阶段 A 共享夹具——ReservedColdCharacter 反例角色 + mock 引擎。

作用域说明：本 conftest 只对 core/prompt/tests/ 及其子目录生效（pytest conftest
目录作用域隔离）。core/personality/tests/ 与 core/memory/tests/ 无法使用本文件的
夹具，辅助 Prompt 泄漏测试需各自定义本地夹具（见 Task 3/4）。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config.character_config import CharacterConfig
from core.memory.session import SessionMemory
from core.personality.engine import C1Data, C6Data, C9Data


# ReservedColdCharacter 的基准人格文本——不含任何 Elysia 词汇
COLDSHARD_BASE_PERSONALITY = (
    "你是 ReservedColdCharacter，一名冷静、克制、态度中立的观察者。"
    "你习惯使用正式、书面化的措辞，不主动制造话题，不表达亲昵，"
    "不调情，不使用语气词或装饰符号。"
)


@pytest.fixture
def cold_config() -> CharacterConfig:
    """构造 ReservedColdCharacter 反例角色的 CharacterConfig。"""
    return CharacterConfig(
        name="ReservedColdCharacter",
        user_pronoun="你",
        base_personality=COLDSHARD_BASE_PERSONALITY,
    )


@pytest.fixture
def cold_engine_mock() -> MagicMock:
    """构造最小 CharacterEngine mock，供 PromptAssembler.build_system_prompt 使用。

    必须显式设置 state.mood / render_c6 / render_c1 / render_c9 的返回值，
    否则它们返回裸 MagicMock，导致渲染出 <MagicMock> 而非真实内容。
    """
    engine = MagicMock()
    engine.state.mood = "neutral"
    engine.render_c6.return_value = C6Data(speaking_style_c6="")
    engine.render_c1.return_value = C1Data(
        core_personality=COLDSHARD_BASE_PERSONALITY, self_personality=""
    )
    engine.render_c9.return_value = C9Data(awareness_text="")
    return engine


@pytest.fixture
def mock_long_term() -> MagicMock:
    """mock 长时记忆，search() 返回空列表（C4 渲染为空，避免时间相关 non-determinism）。"""
    lt = MagicMock()
    lt.search = AsyncMock(return_value=[])
    return lt


@pytest.fixture
def mock_session() -> SessionMemory:
    """空会话（无 own_life、无 mood 覆盖）。"""
    return SessionMemory()


# ---- 共享 helper（供泄漏测试 + 快照测试复用）----


def make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """构造用冷角色 + mock 引擎的 PromptAssembler。"""
    from core.prompt.prompt_assembler import PromptAssembler
    return PromptAssembler(
        cold_config, mock_session, mock_long_term,
        personality_engine=cold_engine_mock,
    )


async def render_system_prompt(assembler, user_message="今天天气不错。",
                               user_name="用户", stt_emotion="", stt_events=None,
                               tts_provider="minimax") -> str:
    """渲染完整 12 段 system prompt。"""
    return await assembler.build_system_prompt(
        user_message=user_message, user_name=user_name,
        stt_emotion=stt_emotion, stt_events=stt_events or [],
        tts_provider=tts_provider,
    )
