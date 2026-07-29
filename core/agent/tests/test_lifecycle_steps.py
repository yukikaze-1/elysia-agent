# core/agent/tests/test_lifecycle_steps.py
"""AgentLifecycle _SHUTDOWN_STEPS / _STARTUP_STEPS 结构验证。"""

import pytest
from core.agent.lifecycle import AgentLifecycle

pytestmark = pytest.mark.unit


class TestShutdownSteps:
    """验证 _SHUTDOWN_STEPS 数据驱动列表"""

    def test_steps_not_empty(self):
        assert len(AgentLifecycle._SHUTDOWN_STEPS) > 0

    def test_all_step_names_unique(self):
        names = [name for name, _ in AgentLifecycle._SHUTDOWN_STEPS]
        assert len(names) == len(set(names))

    def test_all_method_names_exist_on_class(self):
        """每个 method_name 对应 AgentLifecycle 或其父类 mixin 的方法"""
        for name, method_name in AgentLifecycle._SHUTDOWN_STEPS:
            assert hasattr(AgentLifecycle, method_name), \
                f"Step '{name}': method '{method_name}' not found"

    def test_flush_steps_before_stop_steps(self):
        """flush 步骤排在 stop/close 步骤之前"""
        flush_indices = [
            i for i, (name, _) in enumerate(AgentLifecycle._SHUTDOWN_STEPS)
            if name.startswith("flush")
        ]
        stop_indices = [
            i for i, (name, _) in enumerate(AgentLifecycle._SHUTDOWN_STEPS)
            if name.startswith("stop")
        ]
        if flush_indices and stop_indices:
            assert max(flush_indices) < min(stop_indices), \
                "flush steps must run before stop steps"


class TestStartupSteps:
    """验证 _STARTUP_STEPS 数据驱动列表"""

    def test_steps_not_empty(self):
        assert len(AgentLifecycle._STARTUP_STEPS) > 0

    def test_all_step_names_unique(self):
        names = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert len(names) == len(set(names))

    def test_all_method_names_exist_on_class(self):
        for name, method_name in AgentLifecycle._STARTUP_STEPS:
            assert hasattr(AgentLifecycle, method_name), \
                f"Step '{name}': method '{method_name}' not found"
