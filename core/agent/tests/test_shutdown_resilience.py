# 测试文件：core/agent/tests/test_shutdown_resilience.py
"""关停韧性（CR-02）—— 已进入 stop() 就必须走完，不被中断整段跳过。

缺陷原状：`stop()` 逐步 `except Exception`，而 `CancelledError` /
`KeyboardInterrupt` 是 `BaseException` 子类。关停过程中再按一次 Ctrl+C、超时取消、
或 SIGINT 恰好落在 stop() 上 → **剩余清理步骤整段跳过**（会话落库 / 归档 drain /
3 个 flush / 生成终态 / WS 关闭帧全部不做）。`start()` 的失败回滚同样只捕获
`Exception`，取消路径绕过回滚，留下"半初始化 Agent"（后台 task 已 spawn、
DB 客户端已建、`_state` 却停在 RUNNING）。
"""

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent.server import _shutdown_server

pytestmark = pytest.mark.service


def _make_agent():
    from core.input.tests.test_server import _make_agent
    return _make_agent()


@pytest.mark.asyncio
async def test_stop_runs_all_steps_when_one_is_cancelled():
    """某步抛 CancelledError → 后续步骤**仍全部执行**。"""
    agent = _make_agent()
    await agent.start()
    ran: list[str] = []

    async def _raise_cancel():
        ran.append("a")
        raise asyncio.CancelledError

    async def _ok_b():
        ran.append("b")

    async def _ok_c():
        ran.append("c")

    agent._step_a = _raise_cancel          # noqa: SLF001
    agent._step_b = _ok_b                  # noqa: SLF001
    agent._step_c = _ok_c                  # noqa: SLF001
    agent._SHUTDOWN_STEPS = [               # noqa: SLF001
        ("a", "_step_a"), ("b", "_step_b"), ("c", "_step_c"),
    ]

    with pytest.raises(asyncio.CancelledError):
        await agent.stop()

    assert ran == ["a", "b", "c"], "取消不得截断剩余清理步骤"


@pytest.mark.asyncio
async def test_stop_swallows_ordinary_exception_and_continues():
    """普通 Exception 仍按原语义记录后继续（行为不变）。"""
    agent = _make_agent()
    await agent.start()
    ran: list[str] = []

    async def _boom():
        ran.append("a")
        raise RuntimeError("step failed")

    async def _ok():
        ran.append("b")

    agent._step_a = _boom                  # noqa: SLF001
    agent._step_b = _ok                    # noqa: SLF001
    agent._SHUTDOWN_STEPS = [("a", "_step_a"), ("b", "_step_b")]  # noqa: SLF001

    await agent.stop()          # 不抛
    assert ran == ["a", "b"]


@pytest.mark.asyncio
async def test_stop_rethrows_keyboard_interrupt_after_full_cleanup():
    """KeyboardInterrupt 同样不截断清理，但收尾后必须重抛（不吞中断语义）。"""
    agent = _make_agent()
    await agent.start()
    ran: list[str] = []

    async def _interrupt():
        ran.append("a")
        raise KeyboardInterrupt

    async def _ok():
        ran.append("b")

    agent._step_a = _interrupt             # noqa: SLF001
    agent._step_b = _ok                    # noqa: SLF001
    agent._SHUTDOWN_STEPS = [("a", "_step_a"), ("b", "_step_b")]  # noqa: SLF001

    with pytest.raises(KeyboardInterrupt):
        await agent.stop()
    assert ran == ["a", "b"]


@pytest.mark.asyncio
async def test_stop_is_clean_when_no_step_fails():
    """回归网：全部步骤正常时不抛、不改变 `_state` 语义。"""
    from core.agent import RunState

    agent = _make_agent()
    await agent.start()
    await agent.stop()
    assert agent.state == RunState.STOPPED


@pytest.mark.asyncio
async def test_start_rollback_covers_base_exception():
    """启动期非 Exception 异常（取消）也必须回滚，不留半初始化 Agent。

    用真实 `start()`：让 `_start_persistence` 抛 CancelledError，断言
    `start()` 重抛 CancelledError（非它自己冒出来的别的异常），且 agent 已被
    回滚为 STOPPED —— 原先 `except Exception` 接不住，`_state` 会停在 RUNNING。
    """
    from core.agent import RunState

    agent = _make_agent()

    async def _cancelled():
        raise asyncio.CancelledError

    agent._start_persistence = _cancelled   # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await agent.start()
    assert agent.state == RunState.STOPPED, "取消路径必须触发回滚"


# ═══════════════════════════════════════════════════════════════════
# 服务关停序列：stop() 抛错也必须 cleanup()
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_shutdown_runs_cleanup_even_when_stop_raises():
    """stop() 抛错 → runner.cleanup() 仍必须执行（否则端口/连接泄漏到进程退出）。"""
    agent = MagicMock()
    agent.stop = AsyncMock(side_effect=RuntimeError("stop 炸了"))
    runner = MagicMock()
    runner.cleanup = AsyncMock()

    with pytest.raises(RuntimeError):
        await _shutdown_server(agent, runner)

    assert runner.cleanup.await_count == 1


@pytest.mark.asyncio
async def test_shutdown_runs_both_in_order():
    """正常路径：先 stop() 再 cleanup()。"""
    order: list[str] = []

    async def _stop():
        order.append("stop")

    async def _cleanup():
        order.append("cleanup")

    agent = MagicMock()
    agent.stop = _stop
    runner = MagicMock()
    runner.cleanup = _cleanup

    await _shutdown_server(agent, runner)
    assert order == ["stop", "cleanup"]


@pytest.mark.asyncio
async def test_shutdown_survives_cleanup_timeout():
    """cleanup 超时 → 告警后正常返回，不把超时升级成异常。"""
    agent = MagicMock()
    agent.stop = AsyncMock()

    async def _hang():
        await asyncio.sleep(30)

    runner = MagicMock()
    runner.cleanup = _hang

    await asyncio.wait_for(_shutdown_server(agent, runner), timeout=10.0)


# ═══════════════════════════════════════════════════════════════════
# 真实信号端到端：子进程里发真 SIGTERM
# ═══════════════════════════════════════════════════════════════════

_SIGTERM_DRIVER = r"""
import asyncio, os, signal, sys
sys.path.insert(0, sys.argv[1])
from core.agent.server import _install_signal_handlers

class A:
    def __init__(self):
        self.hits = 0
    def request_shutdown(self):
        self.hits += 1

async def main():
    a = A()
    names = _install_signal_handlers(a, asyncio.get_running_loop())
    if names != ["SIGTERM", "SIGINT"]:
        print("UNSUPPORTED", names)
        return 3
    os.kill(os.getpid(), signal.SIGTERM)   # 真信号
    await asyncio.sleep(0.2)
    print("HITS", a.hits)
    return 0

sys.exit(asyncio.run(main()))
"""


def test_real_sigterm_reaches_handler_in_subprocess():
    """真实 SIGTERM（非回调直调）必须被 handler 接住，进程不能被内核杀掉。

    这条是 CR-02 的核心证据：`kill -TERM` 曾经直接终止进程、20 步清理一步不跑。
    放在子进程里发真信号 —— 主测试进程不受影响。
    """
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[3])
    proc = subprocess.run(
        [sys.executable, "-c", _SIGTERM_DRIVER, root],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "HITS 1" in proc.stdout, proc.stdout

