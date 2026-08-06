"""D4：_interrupt_source 只在实际取消任务时记录（spec §3.4）。"""
import asyncio
from unittest.mock import MagicMock

import pytest

from core.cognitive.loop import CognitiveLoop

pytestmark = pytest.mark.unit


def _make_loop() -> CognitiveLoop:
    return CognitiveLoop(
        llm=MagicMock(), fsm=MagicMock(), event_bus=MagicMock(),
        output=MagicMock(), prompt=MagicMock(), session_resolver=MagicMock(),
    )


@pytest.mark.asyncio
async def test_source_recorded_when_task_actually_cancelled():
    loop = _make_loop()

    async def _pending():
        await asyncio.sleep(10)

    task = asyncio.create_task(_pending())
    loop._current_task = task
    assert loop.cancel_current(source="websocket") is True
    assert loop._interrupt_source == "websocket"
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_no_source_recorded_when_nothing_cancelled():
    """IDLE_BG 窗口：task 已 done → cancel_current 不取消也不留 sticky source。"""
    loop = _make_loop()
    loop._interrupt_source = None

    async def _noop():
        pass

    done = asyncio.create_task(_noop())
    await done
    loop._current_task = done
    assert loop.cancel_current(source="disconnect") is False
    assert loop._interrupt_source is None  # 旧代码：先记 source 再查 done → 泄漏


@pytest.mark.asyncio
async def test_no_source_when_no_task():
    loop = _make_loop()
    loop._current_task = None
    assert loop.cancel_current(source="disconnect") is False
    assert loop._interrupt_source is None
