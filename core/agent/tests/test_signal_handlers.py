# 测试文件：core/agent/tests/test_signal_handlers.py
"""进程信号处理（CR-02）—— SIGTERM/SIGINT 必须走优雅关停。

缺陷原状：全仓零信号处理。`kill -TERM` / `docker stop` / `systemctl stop` 走内核
默认动作**立即终止进程**，`Agent.stop()` 的 20 步清理（会话落库 / 归档 drain /
3 个 flush / 生成终态 / WS 关闭帧 / embedding shutdown）一步都不执行。

本文件只测"信号 → 谁被调用"这一层（loop 用替身，不真的发信号）；"stop() 被打断
仍走完"在 `test_shutdown_resilience.py`。
"""

from unittest.mock import MagicMock

import pytest

from core.agent.server import _install_signal_handlers

pytestmark = pytest.mark.unit


class _FakeLoop:
    """只实现 add_signal_handler 的 loop 替身，捕获 (信号, 回调)。"""

    def __init__(self, *, unsupported: bool = False) -> None:
        self.handlers: dict[object, object] = {}
        self._unsupported = unsupported

    def add_signal_handler(self, sig, callback) -> None:
        if self._unsupported:
            raise NotImplementedError
        self.handlers[sig] = callback


def _fire(loop: _FakeLoop, signame: str) -> None:
    import signal
    loop.handlers[getattr(signal, signame)]()


class TestSignalRegistration:
    def test_registers_sigterm_and_sigint(self):
        """两个信号都要注册——SIGTERM 是 docker/systemd/kill 的实际路径。"""
        loop = _FakeLoop()
        agent = MagicMock()
        names = _install_signal_handlers(agent, loop)
        assert names == ["SIGTERM", "SIGINT"]
        assert len(loop.handlers) == 2
        assert agent.request_shutdown.call_count == 0, "注册本身不得触发关停"

    def test_sigterm_requests_graceful_shutdown(self):
        """第一次 SIGTERM → request_shutdown（主循环退出后走完整 stop()）。"""
        loop = _FakeLoop()
        agent = MagicMock()
        _install_signal_handlers(agent, loop)
        _fire(loop, "SIGTERM")
        assert agent.request_shutdown.call_count == 1

    def test_second_sigterm_forces_exit(self):
        """第二次 SIGTERM → 强制退出（说明第一次之后卡住了）。"""
        loop = _FakeLoop()
        agent = MagicMock()
        forced: list[int] = []
        _install_signal_handlers(agent, loop, _exit=forced.append)
        _fire(loop, "SIGTERM")
        assert forced == []
        _fire(loop, "SIGTERM")
        assert forced == [1]
        assert agent.request_shutdown.call_count == 1, "强退不再重复请求关停"

    def test_sigint_first_graceful_then_force(self):
        """SIGINT 保留「第一次优雅、第二次强退」语义。"""
        loop = _FakeLoop()
        agent = MagicMock()
        forced: list[int] = []
        _install_signal_handlers(agent, loop, _exit=forced.append)
        _fire(loop, "SIGINT")
        assert agent.request_shutdown.call_count == 1 and forced == []
        _fire(loop, "SIGINT")
        assert forced == [1]

    def test_sigint_and_sigterm_counters_are_independent(self):
        """SIGINT 之后的第一发 SIGTERM 仍应是优雅关停，不因"已见过信号"强退。"""
        loop = _FakeLoop()
        agent = MagicMock()
        forced: list[int] = []
        _install_signal_handlers(agent, loop, _exit=forced.append)
        _fire(loop, "SIGINT")
        _fire(loop, "SIGTERM")
        assert forced == [], "两个信号各自计数，互不代偿"
        assert agent.request_shutdown.call_count == 2

    def test_unsupported_platform_degrades_without_raising(self, caplog):
        """Windows ProactorEventLoop 不支持信号处理器 → 告警降级，不阻断启动。"""
        import logging
        loop = _FakeLoop(unsupported=True)
        agent = MagicMock()
        with caplog.at_level(logging.WARNING, logger="core.agent.server"):
            names = _install_signal_handlers(agent, loop)
        assert names == []
        assert any("无法注册" in r.getMessage() for r in caplog.records)

    def test_second_signal_raises_keyboard_interrupt_without_exit_hook(self):
        """生产路径（无 _exit 钩子）第二次信号必须抛 KeyboardInterrupt ——
        不能是普通 Exception（会被 "Exception in callback" 静默记录而吞掉）。"""
        loop = _FakeLoop()
        agent = MagicMock()
        _install_signal_handlers(agent, loop)
        _fire(loop, "SIGTERM")
        with pytest.raises(KeyboardInterrupt):
            _fire(loop, "SIGTERM")
