# core/prompt/tests/test_identity_projection.py
"""P0-2：本轮 UserIdentity 快照投影到普通聊天 Prompt。

覆盖：
- 有效称呼 = preferredAddress 非空优先，否则 displayName；
- 代词/时区来自快照，而非 config.agent.user_name 或进程本地时区；
- 快照存在时显式传入的 user_name 不再影响渲染（调用方无法绕过）。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.cognitive.turn_context import TurnCharacterContext
from core.prompt.prompt_assembler import PromptAssembler
from core.prompt.tests.conftest import (  # noqa: F401
    cold_engine_mock,
    elysia_config,
    mock_long_term,
    mock_session,
)

pytestmark = pytest.mark.service


def _assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session,
               ctx: TurnCharacterContext) -> PromptAssembler:
    assembler = PromptAssembler(
        elysia_config, mock_session, mock_long_term,
        personality_engine=cold_engine_mock,
    )
    assembler._turn_context = ctx
    return assembler


def _ctx(**kwargs) -> TurnCharacterContext:
    base = dict(
        conversationId="c", characterInstanceId="chi", characterId="chr",
        characterRevision=1,
    )
    base.update(kwargs)
    return TurnCharacterContext(**base)


@pytest.mark.asyncio
async def test_preferred_address_wins_over_display_name(
    elysia_config, cold_engine_mock, mock_long_term, mock_session
):
    """preferredAddress 非空时优先作为称呼，displayName 不直接出现在称呼位。"""
    assembler = _assembler(
        elysia_config, cold_engine_mock, mock_long_term, mock_session,
        _ctx(userDisplayName="妖梦", userPreferredAddress="主人",
             userPronouns="她"),
    )
    rendered = await assembler.build_system_prompt(
        user_message="hi", user_name="CALLER_NAME")
    assert "关于 主人：" in rendered
    assert "CALLER_NAME" not in rendered
    assert "关于 妖梦：" not in rendered


@pytest.mark.asyncio
async def test_preferred_address_null_falls_back_to_display_name(
    elysia_config, cold_engine_mock, mock_long_term, mock_session
):
    """preferredAddress=None → 回退 displayName。"""
    assembler = _assembler(
        elysia_config, cold_engine_mock, mock_long_term, mock_session,
        _ctx(userDisplayName="妖梦", userPreferredAddress=None),
    )
    rendered = await assembler.build_system_prompt(
        user_message="hi", user_name="CALLER_NAME")
    assert "关于 妖梦：" in rendered
    assert "CALLER_NAME" not in rendered


@pytest.mark.asyncio
async def test_preferred_address_blank_falls_back_to_display_name(
    elysia_config, cold_engine_mock, mock_long_term, mock_session
):
    """空白字符串等同未设置（前端可能提交空串）。"""
    assembler = _assembler(
        elysia_config, cold_engine_mock, mock_long_term, mock_session,
        _ctx(userDisplayName="妖梦", userPreferredAddress="   "),
    )
    rendered = await assembler.build_system_prompt(user_message="hi", user_name="x")
    assert "关于 妖梦：" in rendered


@pytest.mark.asyncio
async def test_pronouns_from_snapshot(
    elysia_config, cold_engine_mock, mock_long_term, mock_session
):
    """代词取自本轮快照（他），不是默认「她」。"""
    assembler = _assembler(
        elysia_config, cold_engine_mock, mock_long_term, mock_session,
        _ctx(userDisplayName="妖梦", userPronouns="他"),
    )
    rendered = await assembler.build_system_prompt(user_message="hi", user_name="x")
    assert "此刻他只注视着我" in rendered
    assert "此刻她只注视着我" not in rendered


@pytest.mark.asyncio
async def test_timezone_from_snapshot(
    elysia_config, cold_engine_mock, mock_long_term, mock_session
):
    """C5 感官时间按快照时区渲染。"""
    assembler = _assembler(
        elysia_config, cold_engine_mock, mock_long_term, mock_session,
        _ctx(userDisplayName="妖梦", userTimezone="Pacific/Kiritimati"),
    )
    rendered = await assembler.build_system_prompt(user_message="hi", user_name="x")
    expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d %H:%M")
    assert f"当前时间: {expected}" in rendered


@pytest.mark.asyncio
async def test_explicit_timezone_override_used_without_context(
    elysia_config, cold_engine_mock, mock_long_term, mock_session
):
    """无回合上下文（预览路径）时显式 override 生效。"""
    assembler = PromptAssembler(
        elysia_config, mock_session, mock_long_term,
        personality_engine=cold_engine_mock,
    )
    rendered = await assembler.build_system_prompt(
        user_message="hi", user_name="妖梦",
        user_timezone_override="Pacific/Kiritimati",
    )
    expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d %H:%M")
    assert f"当前时间: {expected}" in rendered


def test_effective_address_property():
    """effectiveUserAddress：preferredAddress → displayName → 中性占位。"""
    assert _ctx(userDisplayName="妖梦", userPreferredAddress="主人").effectiveUserAddress == "主人"
    assert _ctx(userDisplayName="妖梦").effectiveUserAddress == "妖梦"
    assert _ctx(userDisplayName="", userPreferredAddress=None).effectiveUserAddress == "用户"
