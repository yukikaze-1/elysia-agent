# core/cognitive/tests/test_flush.py
"""flush_after_response 单元测试 — shutdown 时内存持久化关键路径。"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive.loop import CognitiveLoop

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════

def _make_loop() -> CognitiveLoop:
    """创建一个最小 mock 的 CognitiveLoop 实例，仅用于测试 flush。"""
    loop = CognitiveLoop(
        llm=MagicMock(),
        fsm=MagicMock(),
        event_bus=MagicMock(),
        output=MagicMock(),
        prompt=MagicMock(),
        session_resolver=AsyncMock(),
    )
    loop._pending_after_response = []
    return loop


# ═══════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_flush_empty_returns_immediately():
    """空列表 → 直接返回。"""
    loop = _make_loop()
    await loop.flush_after_response()
    # 不应抛异常，不应挂起


@pytest.mark.asyncio
async def test_flush_all_done_returns_immediately():
    """全部已完成 → 直接返回。"""
    loop = _make_loop()
    task = asyncio.create_task(asyncio.sleep(0))
    await task  # 等它完成
    loop._pending_after_response = [task]
    await loop.flush_after_response()
    # 已完成的 task 会被 filter 跳过


@pytest.mark.asyncio
async def test_flush_pending_completes():
    """等待 pending task 正常完成。"""
    loop = _make_loop()
    task = asyncio.create_task(asyncio.sleep(0))
    loop._pending_after_response = [task]
    await loop.flush_after_response()
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_flush_timeout_cancels_task():
    """超时 → cancel task + 日志警告。"""
    loop = _make_loop()
    # 创建一个永不会完成的 task
    task = asyncio.create_task(asyncio.sleep(999))
    loop._pending_after_response = [task]

    await loop.flush_after_response(timeout=0.01)

    # task 应被 cancel（flush 内部 catch TimeoutError → cancel）
    assert task.cancelled()


@pytest.mark.asyncio
async def test_flush_exception_is_caught(caplog):
    """task 内部异常 → 捕获 + 日志警告，不崩溃。"""
    loop = _make_loop()

    async def raise_err():
        raise RuntimeError("模拟后处理异常")

    task = asyncio.create_task(raise_err())
    loop._pending_after_response = [task]

    with caplog.at_level(logging.WARNING):
        await loop.flush_after_response(timeout=1.0)

    assert "after-response task 异常" in caplog.text
    # 异常不应向外传播
    assert task.done()
