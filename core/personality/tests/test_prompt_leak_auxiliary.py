"""辅助 Prompt（CharacterReflector/SpeakingStyleEdit）的 Elysia 泄漏基线。

注意：
- SpeakingStyleEdit 生产路径是 pm.render()（该模板不含 macro），不是 render_macro()。
- 本文件在 core/personality/tests/，不依赖 core/prompt/tests/conftest 的夹具
  （pytest conftest 目录作用域隔离），词条从 leak_lexicon 导入。
"""

import pytest

from paths import ProjectPath
from Module.system_prompt.manager import PromptManager

pytestmark = pytest.mark.service


def _pm() -> PromptManager:
    """构造指向 core/prompts 目录的 PromptManager（用 ProjectPath 定位，不受 cwd 影响）。"""
    return PromptManager(str(ProjectPath.PROMPTS.resolve()))


# 冷角色视角——故意用中性人格/用户，若结果仍出现她/Elysia 词汇即泄漏
_RECENT_CONVERSATIONS = "用户：你好。\nReservedColdCharacter：你好，请讲。"


def test_character_reflector_leak_pronoun():
    """CharacterReflector 硬编码她——用户是「用户」却出现她。"""
    pm = _pm()
    result = pm.render(
        "CharacterReflector.j2",
        character_name="ReservedColdCharacter",
        user_name="用户",
        l1_core="你是 ReservedColdCharacter，冷静克制的观察者。",
        l2_self="",
        l3_history="",
        last_self_perception="",
        mood_snapshot="当前情绪: neutral (强度: 0.00)",
        time_span="约 1 分钟",
        buffer_size=1,
        recent_conversations=_RECENT_CONVERSATIONS,
    )
    assert "她" in result


def test_speaking_style_edit_leak_forced_structure():
    """SpeakingStyleEdit 不再强制保留 Elysia 五模式结构（Task 6 去 Elysia 化）。"""
    pm = _pm()
    result = pm.render(
        "SpeakingStyleEdit.j2",
        character_name="ReservedColdCharacter",
        self_l2="我说话冷静、正式、克制。",
        current_speaking_style="你说话冷静、正式、克制。",
    )
    assert "五模式切换" not in result
    assert "♪" not in result


def test_speaking_style_edit_leak_immutable_color():
    """SpeakingStyleEdit 不再强制「明快、柔和、自信、亲昵」不可变底色（Task 6 去 Elysia 化）。"""
    pm = _pm()
    result = pm.render(
        "SpeakingStyleEdit.j2",
        character_name="ReservedColdCharacter",
        self_l2="我说话冷静、正式、克制。",
        current_speaking_style="你说话冷静、正式、克制。",
    )
    assert "明快、柔和、自信、亲昵" not in result
