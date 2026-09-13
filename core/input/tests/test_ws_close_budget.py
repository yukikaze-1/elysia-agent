# 测试文件：core/input/tests/test_ws_close_budget.py
"""S2-E3：连接收尾必须在**远小于单次 STT 上限**的预算内让出。

原先两处叠加，使"停止按钮/停机"最长 30s 无响应：

1. `handle_ws` 里 `except asyncio.CancelledError: pass` —— 取消被吃掉后仍继续执行
   `finally` 的清理；
2. 清理的第一个 await 是 `audio_buf.flush()`，内部 `wait_for(stt_transcribe, 30.0)`。

`STT_TIMEOUT=30.0` 是**单次调用**上限，不是"连接收尾可接受阻塞时间"的预算。
停机时每个持有音频段的连接各把进程退出拖长 ≤30s，期间 `_ws_active` 不减 →
新连接按 `CONNECTION_LIMITED` 503 被拒。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.service


def _conn(**over):
    conn = MagicMock()
    conn.monitor_task = None
    conn.voice_mgr = None
    conn.audio_buf = None
    conn.desktop_conn_id = None
    conn.protocol_version = 1
    conn.sent_message_ids = set()
    conn.channel = MagicMock()
    conn.client_id = "c1"
    for k, v in over.items():
        setattr(conn, k, v)
    return conn


def _agent():
    agent = MagicMock()
    agent.output.remove_channel = MagicMock()
    return agent


@pytest.mark.asyncio
async def test_slow_flush_does_not_block_close():
    """flush 永不返回 → 收尾仍须在预算内完成，且 channel 一定被注销。"""
    from core.input import ws_handler

    async def _hang() -> None:
        await asyncio.sleep(3600)

    conn = _conn()
    conn.audio_buf = MagicMock()
    conn.audio_buf.flush = _hang
    conn.audio_buf.release = MagicMock()   # 放弃尾段路径会显式释放缓冲
    agent = _agent()

    budget = ws_handler._CLOSE_FLUSH_BUDGET_S               # noqa: SLF001
    started = time.monotonic()
    try:
        # **测试自己也要有预算**：缺陷回归时 `_close_ws_connection` 会**永不返回**
        # （flush 是 `sleep(3600)`），裸 await 会让整个 job 挂死而不是失败——
        # 反证第一次就是这样：超时被杀、没有任何输出。快失败才有诊断价值。
        await asyncio.wait_for(
            ws_handler._close_ws_connection(conn, agent),   # noqa: SLF001
            timeout=budget + 3.0,
        )
    except asyncio.TimeoutError:
        pytest.fail(f"收尾被 flush 拖住：超过 {budget + 3.0:.0f}s 仍未返回")
    elapsed = time.monotonic() - started

    assert elapsed < budget + 1.0, f"收尾耗时 {elapsed:.1f}s，预算 {budget}s"
    agent.output.remove_channel.assert_called_once_with(conn.channel)


@pytest.mark.asyncio
async def test_budget_is_far_below_stt_timeout():
    """预算必须**远小于**单次 STT 上限——两者语义不同，不能共用一个数。"""
    from core.input import ws_handler
    from core.input.voice_sessions import STT_TIMEOUT

    assert ws_handler._CLOSE_FLUSH_BUDGET_S <= 5.0            # noqa: SLF001
    assert ws_handler._CLOSE_FLUSH_BUDGET_S < STT_TIMEOUT


@pytest.mark.asyncio
async def test_fast_flush_still_runs():
    """回归网：正常（快）的尾段转写仍然会被执行，不被预算误杀。"""
    from core.input import ws_handler

    conn = _conn()
    conn.audio_buf = MagicMock()
    conn.audio_buf.flush = AsyncMock()
    conn.voice_mgr = MagicMock()
    conn.voice_mgr.close = AsyncMock()
    conn.voice_mgr.emitted_message_ids = set()
    agent = _agent()

    await ws_handler._close_ws_connection(conn, agent)     # noqa: SLF001

    conn.audio_buf.flush.assert_awaited_once()
    conn.voice_mgr.close.assert_awaited_once()
