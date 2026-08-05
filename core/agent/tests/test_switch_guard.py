"""批次 5 Task 7：switch 路径守卫（runtime 拦截 + SSRF 校验 + 出站注入 + 剥离 runtime 透传）。

覆盖 switch_llm/switch_stt/switch_tts 三路径：
- runtime != backend → ok=False + PROFILE_RUNTIME_NOT_BACKEND，service_state 不改；
- custom+backend SSRF（base_url=127.0.0.1 等内网）→ ok=False；
- custom+backend 公网 → 注入守卫 http_client_factory（_SSRFTransport）+ service_state 更新 + broadcast；
- 内置 switch 保持：resolve 返回 {} → runtime 缺省 backend、无 http_client_factory → ok=True；
- 剥离 runtime：resolve 返回 runtime 键 → 不透传给 Factory/switch_model（防构造 TypeError）。
"""

import socket
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock
import pytest

from core.agent.agent import Agent
from core.input.ssrf import _SSRFTransport
from core.schema.service_state import ServiceState

pytestmark = pytest.mark.service

# 固定公网解析样例（不触网）——public.example.com 类不可解析 host 会 NXDOMAIN 拒绝，
# mock getaddrinfo 返回公网 IP 使 validate_endpoint 放行。
_PUBLIC_INFOS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


class TestSwitchRuntimeGuard:
    """desktop/local runtime 拦截：ok=False + 错误码，service_state 不改、不建后端。"""

    @pytest.mark.asyncio
    async def test_switch_llm_desktop_runtime_blocked(self, fake_agent):
        """LLM desktop runtime → ok=False + PROFILE_RUNTIME_NOT_BACKEND，service_state 未改。"""
        agent = fake_agent
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        with mock.patch(
            "core.input.custom_providers.resolve_model_params",
            return_value={"base_url": "https://public.example.com", "runtime": "desktop"},
        ):
            result = await Agent.switch_llm(agent, "custom/desktop_prof")
        assert result["ok"] is False
        assert "PROFILE_RUNTIME_NOT_BACKEND" in result["error"]
        agent._llm.switch_model.assert_not_called()
        assert agent.session.service_state.llm_model == ""  # service_state 未改

    @pytest.mark.asyncio
    async def test_switch_stt_desktop_runtime_blocked(self, fake_agent):
        """STT local runtime → ok=False，不建后端。"""
        agent = fake_agent
        agent._stt = None
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        with mock.patch(
            "core.input.custom_providers.resolve_model_params",
            return_value={"base_url": "https://public.example.com", "runtime": "local"},
        ):
            result = await Agent.switch_stt(agent, "custom/desktop_prof")
        assert result["ok"] is False
        assert "PROFILE_RUNTIME_NOT_BACKEND" in result["error"]
        assert agent.session.service_state.stt_model == ""
        assert agent._stt is None  # 未建后端

    @pytest.mark.asyncio
    async def test_switch_tts_desktop_runtime_blocked(self, fake_agent):
        """TTS desktop runtime → ok=False，不建后端。"""
        agent = fake_agent
        agent._tts = None
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        with mock.patch(
            "core.input.custom_providers.resolve_model_params",
            return_value={"base_url": "https://public.example.com", "runtime": "desktop"},
        ):
            result = await Agent.switch_tts(agent, "custom/desktop_prof")
        assert result["ok"] is False
        assert "PROFILE_RUNTIME_NOT_BACKEND" in result["error"]
        assert agent.session.service_state.tts_model == ""
        assert agent._tts is None


class TestSwitchSSRF:
    """custom+backend 内网 base_url → SSRF 拦截（ok=False），不触网（字面量 IP 判类）。"""

    @pytest.mark.asyncio
    async def test_switch_llm_ssrf_blocked(self, fake_agent):
        """LLM base_url=127.0.0.1 → ok=False（SSRFError）。"""
        agent = fake_agent
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        with mock.patch(
            "core.input.custom_providers.resolve_model_params",
            return_value={"base_url": "http://127.0.0.1", "runtime": "backend"},
        ):
            result = await Agent.switch_llm(agent, "custom/bad")
        assert result["ok"] is False
        assert "目标地址被拒绝" in result["error"]
        agent._llm.switch_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_stt_ssrf_blocked(self, fake_agent):
        """STT base_url=169.254.169.254（云元数据）→ ok=False，不建后端。"""
        agent = fake_agent
        agent._stt = None
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        with mock.patch("Module.stt.STTFactory.create") as mc, mock.patch(
            "core.input.custom_providers.resolve_model_params",
            return_value={"base_url": "http://169.254.169.254", "runtime": "backend"},
        ):
            result = await Agent.switch_stt(agent, "custom/bad")
        assert result["ok"] is False
        mc.assert_not_called()  # SSRF 拦截在 create 之前——工厂不被调用
        assert agent._stt is None


class TestSwitchOutboundGuard:
    """custom+backend 公网 → 注入守卫 http_client_factory + service_state 更新 + broadcast。"""

    @pytest.mark.asyncio
    async def test_switch_llm_injects_guarded_client(self, fake_agent):
        """LLM 公网 custom → switch_model 收到守卫 http_client_factory（剥离 runtime），state 更新 + broadcast。"""
        agent = fake_agent
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        agent._llm.switch_model = mock.MagicMock()
        with mock.patch(
            "core.input.custom_providers.resolve_model_params",
            return_value={"base_url": "https://public.example.com", "runtime": "backend"},
        ), mock.patch("core.input.ssrf.socket.getaddrinfo", return_value=_PUBLIC_INFOS), mock.patch(
            "core.input.capabilities.broadcast_capabilities", new_callable=AsyncMock
        ) as mock_bcast:
            result = await Agent.switch_llm(agent, "custom/good")
        assert result["ok"] is True
        agent._llm.switch_model.assert_called_once()
        args, kwargs = agent._llm.switch_model.call_args
        assert args == ("custom/good",)
        assert "runtime" not in kwargs                       # 剥离 runtime 透传
        guard = kwargs["http_client_factory"]()
        assert isinstance(guard._transport, _SSRFTransport)  # 守卫类型
        assert agent.session.service_state.llm_model == "custom/good"
        mock_bcast.assert_awaited_once_with(agent)

    @pytest.mark.asyncio
    async def test_switch_stt_injects_guarded_client(self, fake_agent):
        """STT 公网 custom → STTFactory.create 收到守卫 http_client_factory，后端挂槽 + state 更新。"""
        agent = fake_agent
        agent._stt = None
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        with mock.patch("Module.stt.STTFactory.create") as mc, mock.patch(
            "core.input.custom_providers.resolve_model_params",
            return_value={"base_url": "https://public.example.com", "runtime": "backend"},
        ), mock.patch("core.input.ssrf.socket.getaddrinfo", return_value=_PUBLIC_INFOS):
            backend = MagicMock()
            backend.model = "custom/good"
            backend.test = mock.AsyncMock(return_value={"ok": True, "latency_ms": 1})
            backend.close = mock.AsyncMock()
            mc.return_value = backend
            result = await Agent.switch_stt(agent, "custom/good")
        assert result["ok"] is True
        _, kwargs = mc.call_args
        assert "runtime" not in kwargs
        guard = kwargs["http_client_factory"]()
        assert isinstance(guard._transport, _SSRFTransport)
        assert agent._stt is backend
        assert agent.session.service_state.stt_model == "custom/good"

    @pytest.mark.asyncio
    async def test_switch_tts_injects_guarded_client(self, fake_agent):
        """TTS 公网 custom → TTSFactory.create 收到守卫 http_client_factory（含 rate_limit_rpm），后端挂槽。"""
        agent = fake_agent
        agent._tts = None
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        agent._config.tts.rate_limit_rpm = 10
        with mock.patch("Module.tts.TTSFactory.create") as mc, mock.patch(
            "core.input.custom_providers.resolve_model_params",
            return_value={"base_url": "https://public.example.com", "runtime": "backend"},
        ), mock.patch("core.input.ssrf.socket.getaddrinfo", return_value=_PUBLIC_INFOS):
            backend = MagicMock()
            backend.model = "custom/good"
            backend.test = mock.AsyncMock(return_value={"ok": True, "latency_ms": 1})
            backend.close = mock.AsyncMock()
            mc.return_value = backend
            result = await Agent.switch_tts(agent, "custom/good")
        assert result["ok"] is True
        _, kwargs = mc.call_args
        assert "runtime" not in kwargs
        guard = kwargs["http_client_factory"]()
        assert isinstance(guard._transport, _SSRFTransport)
        assert agent._tts is backend
        assert agent.session.service_state.tts_model == "custom/good"


class TestSwitchBuiltin:
    """内置 switch 保持：resolve 返回 {} → runtime 缺省 backend、无 http_client_factory → ok=True。"""

    @pytest.mark.asyncio
    async def test_switch_llm_builtin_no_http_client(self, fake_agent):
        """内置 LLM switch：resolve 返回 {} → switch_model 只收 model_id，无 http_client_factory。"""
        agent = fake_agent
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        agent._llm.switch_model = mock.MagicMock()
        with mock.patch("core.input.custom_providers.resolve_model_params", return_value={}):
            result = await Agent.switch_llm(agent, "deepseek/deepseek-v4-flash")
        assert result["ok"] is True
        agent._llm.switch_model.assert_called_once_with("deepseek/deepseek-v4-flash")
        assert agent.session.service_state.llm_model == "deepseek/deepseek-v4-flash"
