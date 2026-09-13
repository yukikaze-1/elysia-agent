# 测试文件：core/application/tests/test_sessions.py
"""会话用例层单测（ARCH-INC-P1-02）。

**抽这一层买到的是"两个入口只有一个答案"**：原先 REST 与 WS 各自实现「激活会话」，
判定顺序不同（WS 忙门禁在前、REST 存在性在前），于是**同一个未知会话**在 WS 得到
`AGENT_BUSY`、在 REST 得到 `SESSION_NOT_FOUND`。本文件锁住统一后的语义。

第二个买到的：这些断言**不需要 aiohttp**——原先要覆盖同样的规则必须起服务。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.application.sessions import (
    CODE_AGENT_BUSY,
    CODE_SESSION_NOT_FOUND,
    SessionApplicationService,
    SessionUseCaseError,
    service_for_agent,
)

pytestmark = pytest.mark.unit


def _svc(*, exists: bool = True, accepting: bool = True,
         switch_exc: Exception | None = None):
    session = MagicMock()
    session.session_id = "conv_1"
    switch = AsyncMock(side_effect=switch_exc, return_value=session)
    svc = SessionApplicationService(
        has_session=AsyncMock(return_value=exists),
        switch_session=switch,
        is_accepting_input=lambda: accepting,
    )
    return svc, switch


# ═══════════════════════════════════════════════════════════════════
# 正常路径
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_activate_switches_and_returns_session():
    svc, switch = _svc()
    target = await svc.activate("conv_1")
    assert target.session_id == "conv_1"
    switch.assert_awaited_once_with("conv_1")


@pytest.mark.asyncio
async def test_empty_conversation_id_rejected():
    svc, switch = _svc()
    with pytest.raises(SessionUseCaseError) as ei:
        await svc.activate("")
    assert ei.value.code == "CONTRACT_VALIDATION"
    switch.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
# 两条失败路径 + 「谁先判」的语义统一
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unknown_session_is_session_not_found():
    svc, switch = _svc(exists=False)
    with pytest.raises(SessionUseCaseError) as ei:
        await svc.activate("nope")
    assert ei.value.code == CODE_SESSION_NOT_FOUND
    assert ei.value.retryable is False
    switch.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_agent_is_agent_busy_retryable():
    svc, switch = _svc(accepting=False)
    with pytest.raises(SessionUseCaseError) as ei:
        await svc.activate("conv_1")
    assert ei.value.code == CODE_AGENT_BUSY
    assert ei.value.retryable is True, "忙是可重试语义，码名与 retryable 必须一致"
    switch.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonexistent_wins_over_busy():
    """**核心语义**：会话不存在时**必须**报 SESSION_NOT_FOUND，哪怕 agent 正忙。

    原先 WS 入口先判忙 → 未知会话被报成 `AGENT_BUSY`，客户端会去重试一个
    根本不存在的会话；REST 入口先判存在性 → 报 `SESSION_NOT_FOUND`。
    同一事实两套答案，客户端按哪个写都会在另一个入口上错。
    """
    svc, _ = _svc(exists=False, accepting=False)
    with pytest.raises(SessionUseCaseError) as ei:
        await svc.activate("nope")
    assert ei.value.code == CODE_SESSION_NOT_FOUND, "更具体的事实先判"


@pytest.mark.asyncio
async def test_switch_race_busy_is_translated():
    """双保险竞态：switch() 内部再查一次忙 → 同样归一为 AGENT_BUSY。

    原先只有 WS 入口捕获 `SessionBusyError`，REST 入口没有 → 同一竞态
    一个回 AGENT_BUSY、一个回 500。
    """
    from core.memory.session_manager import SessionBusyError

    svc, _ = _svc(switch_exc=SessionBusyError("busy"))
    with pytest.raises(SessionUseCaseError) as ei:
        await svc.activate("conv_1")
    assert ei.value.code == CODE_AGENT_BUSY and ei.value.retryable is True


@pytest.mark.asyncio
async def test_other_switch_errors_propagate():
    """非忙的切换失败**不得**被吞成 AGENT_BUSY——那会把内部错误说成"稍后重试"。"""
    svc, _ = _svc(switch_exc=RuntimeError("db down"))
    with pytest.raises(RuntimeError):
        await svc.activate("conv_1")


# ═══════════════════════════════════════════════════════════════════
# 装配：只取最小面
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_service_for_agent_uses_minimal_surface():
    """按最小面装配（不接收整个 Agent 再做 Service Locator）。"""
    agent = MagicMock()
    agent.session_manager.has_session = AsyncMock(return_value=True)
    agent.switch_session = AsyncMock(return_value=MagicMock(session_id="conv_1"))
    agent.status.is_accepting_input = True

    svc = service_for_agent(agent)
    await svc.activate("conv_1")
    agent.switch_session.assert_awaited_once_with("conv_1")
