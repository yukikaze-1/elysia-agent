# 测试文件：core/agent/tests/test_lifecycle_steps.py
"""AgentLifecycle _SHUTDOWN_STEPS / _STARTUP_STEPS 结构验证。"""

import logging
from unittest import mock

import pytest
from core.agent._lifecycle_services import _LifecycleServices
from core.agent.lifecycle import AgentLifecycle
from core.schema.service_state import ServiceState

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

    def test_service_state_step_before_tts_step(self):
        """service_state 步骤必须在 tts 步骤之前（ServiceState 应用先于建后端）。"""
        names = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert names.index("service_state") < names.index("tts")


class TestApplyServiceState:
    """_apply_service_state 启动步骤：读 ServiceState → 设模式槽位 → LLM 应用。"""

    @pytest.mark.asyncio
    async def test_sets_slots_and_switches_llm(self, fake_agent):
        """service_state 显式值 → 槽位直接取显式值；llm_model ≠ config → switch_llm。"""
        agent = fake_agent
        agent.session.service_state = ServiceState(
            llm_model="deepseek/deepseek-v4-pro",
            stt_enabled=True,
            tts_enabled=False,
        )
        agent._config.stt.enabled = True
        agent._config.tts.enabled = True
        agent._config.llm.model = "deepseek/deepseek-v4-flash"
        agent.switch_llm = mock.AsyncMock(return_value={"ok": True})

        await _LifecycleServices._apply_service_state(agent)

        assert agent._stt_enabled is True
        assert agent._tts_enabled is False
        agent.switch_llm.assert_awaited_once_with("deepseek/deepseek-v4-pro")

    @pytest.mark.asyncio
    async def test_effective_enabled_falls_back_to_config(self, fake_agent):
        """三态回退：enabled=None → 有效值 = config.<svc>.enabled。"""
        agent = fake_agent
        agent.session.service_state = ServiceState()  # enabled 全 None
        agent._config.stt.enabled = True
        agent._config.tts.enabled = False
        agent._config.llm.model = "deepseek/deepseek-v4-flash"
        agent.switch_llm = mock.AsyncMock(return_value={"ok": True})

        await _LifecycleServices._apply_service_state(agent)

        assert agent._stt_enabled is True
        assert agent._tts_enabled is False
        agent.switch_llm.assert_not_awaited()  # llm_model 为空 → 不应用

    @pytest.mark.asyncio
    async def test_no_switch_llm_when_same_as_config(self, fake_agent):
        """llm_model == config.llm.model → 不触发 switch_llm。"""
        agent = fake_agent
        agent.session.service_state = ServiceState(llm_model="deepseek/deepseek-v4-flash")
        agent._config.stt.enabled = False
        agent._config.tts.enabled = False
        agent._config.llm.model = "deepseek/deepseek-v4-flash"
        agent.switch_llm = mock.AsyncMock(return_value={"ok": True})

        await _LifecycleServices._apply_service_state(agent)

        agent.switch_llm.assert_not_awaited()


class TestStartTTSGate:
    """_start_tts 建门（有效 enabled 三态）+ startup_tts_model。"""

    def _setup_tts_agent(self, agent, service_state, cfg_enabled):
        agent.session.service_state = service_state
        agent._config.tts.enabled = cfg_enabled
        agent._config.tts.model = "minimax/speech-2.8-hd"
        agent._config.tts.rate_limit_rpm = 10
        agent._tts_enabled = (
            service_state.tts_enabled
            if service_state.tts_enabled is not None else cfg_enabled
        )
        agent._tts = None  # 对齐 Agent.__init__ 初始值——MagicMock 未设时 auto-mock 恒真
        agent._cognitive.configure = mock.MagicMock()
        agent._cognitive._tts_mgr.set_enabled = mock.AsyncMock()

    @pytest.mark.asyncio
    async def test_builds_with_startup_model_when_hotplug_open(self, fake_agent):
        """config=false + 显式 true → 建（热插拔'开'跨重启存活），用 startup_tts_model。"""
        agent = fake_agent
        self._setup_tts_agent(
            agent, ServiceState(tts_model="custom/mytts", tts_enabled=True), cfg_enabled=False)
        backend = mock.MagicMock()
        backend.test = mock.AsyncMock(return_value={"ok": True})
        with mock.patch("Module.tts.TTSFactory.create", return_value=backend) as mc, \
             mock.patch("core.input.custom_providers.resolve_model_params",
                        return_value={"base_url": "http://tts.local"}):
            await _LifecycleServices._start_tts(agent)

        # custom+backend → 注入出站守卫工厂（review 修正：启动路径也出站守卫）
        mc.assert_called_once_with(
            "custom/mytts", base_url="http://tts.local", rate_limit_rpm=10,
            http_client_factory=mock.ANY)
        assert agent._tts is backend
        agent._cognitive.configure.assert_called_once_with(tts=backend)
        agent._cognitive._tts_mgr.set_enabled.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_desktop_runtime_skips_creation(self, fake_agent):
        """review 修正：TTS profile runtime=desktop → 后端启动不代理（D16），
        跳过建 TTS（纯文本模式），不因空 base_url fail-fast。"""
        agent = fake_agent
        self._setup_tts_agent(
            agent, ServiceState(tts_model="custom/desk", tts_enabled=True), cfg_enabled=False)
        with mock.patch("Module.tts.TTSFactory.create") as mc, \
             mock.patch("core.input.custom_providers.resolve_model_params",
                        return_value={"base_url": "", "runtime": "desktop"}):
            await _LifecycleServices._start_tts(agent)
        # 不建后端、不测连通性、不崩
        mc.assert_not_called()
        assert agent._tts is None
        agent._cognitive.configure.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_off_when_config_off_and_none(self, fake_agent):
        """config=false + None → 不建（YAML 硬关保留）。"""
        agent = fake_agent
        self._setup_tts_agent(agent, ServiceState(), cfg_enabled=False)
        with mock.patch("Module.tts.TTSFactory.create") as mc:            await _LifecycleServices._start_tts(agent)
        mc.assert_not_called()
        assert agent._tts is None

    @pytest.mark.asyncio
    async def test_gate_off_when_config_on_and_explicit_false(self, fake_agent):
        """config=true + 显式 false → 不建（尊重用户关闭，免浪费连接测试）。"""
        agent = fake_agent
        self._setup_tts_agent(agent, ServiceState(tts_enabled=False), cfg_enabled=True)
        with mock.patch("Module.tts.TTSFactory.create") as mc:
            await _LifecycleServices._start_tts(agent)
        mc.assert_not_called()
        assert agent._tts is None

    @pytest.mark.asyncio
    async def test_gate_on_when_config_on_and_none(self, fake_agent):
        """config=true + None → 建（现状行为），用 config 默认模型。"""
        agent = fake_agent
        self._setup_tts_agent(agent, ServiceState(), cfg_enabled=True)
        backend = mock.MagicMock()
        backend.test = mock.AsyncMock(return_value={"ok": True})
        with mock.patch("Module.tts.TTSFactory.create", return_value=backend) as mc:
            await _LifecycleServices._start_tts(agent)
        mc.assert_called_once_with("minimax/speech-2.8-hd", rate_limit_rpm=10)
        assert agent._tts is backend

    @pytest.mark.asyncio
    async def test_missing_api_key_degrades_to_text_only(self, fake_agent, caplog):
        """minimax 无 MINIMAX_API_KEY → 自动降级纯文本（warn 不崩），不再 fail-fast。

        修复前：TTSFactory.create 抛 ValueError「需要 api_key」→ _start_tts 的
        except 全吞成 fail-fast raise → 启动崩溃。修复后：缺 key 是配置缺欠而非
        配置错误，降级为 text-only，其他配置错误仍 fail-fast。
        """
        agent = fake_agent
        self._setup_tts_agent(agent, ServiceState(), cfg_enabled=True)
        with mock.patch("Module.tts.TTSFactory.create", side_effect=ValueError(
                "MiniMax TTS 需要 api_key，请设置 MINIMAX_API_KEY 环境变量或显式传入")) as mc, \
             mock.patch("core.input.custom_providers.resolve_model_params",
                        return_value={"base_url": "https://api.minimaxi.com"}):
            with caplog.at_level(logging.WARNING, logger="core.agent._lifecycle_services"):
                await _LifecycleServices._start_tts(agent)
        mc.assert_called_once()
        assert agent._tts is None
        agent._cognitive.configure.assert_not_called()
        assert "纯文本模式" in caplog.text

    @pytest.mark.asyncio
    async def test_non_key_config_error_still_fail_fast(self, fake_agent):
        """非缺-key 配置错误（未知 provider）→ 仍 fail-fast（raise），不误降级。"""
        agent = fake_agent
        self._setup_tts_agent(agent, ServiceState(), cfg_enabled=True)
        with mock.patch("Module.tts.TTSFactory.create", side_effect=ValueError(
                "Unknown provider 'foo'. Known: ...")) as mc, \
             mock.patch("core.input.custom_providers.resolve_model_params",
                        return_value={"base_url": "http://x"}):
            with pytest.raises(ValueError):
                await _LifecycleServices._start_tts(agent)
        mc.assert_called_once()

