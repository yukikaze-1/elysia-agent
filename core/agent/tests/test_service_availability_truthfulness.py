"""F7：服务"可用"的报告必须只有一个权威——运行期句柄。

原始 finding（canonical **F7**）：

    `/debug` 的 `tts_ok = agent.tts is not None`（存在性），
    而 `stt_ok = agent.stt_ok`（上一次建连/切换探测的结果）⇒ 面板混用两种语义。

本文件把 F7 拆成两半，分别给证据：

**(A) 「存在性冒充可用」——不成立（重分类，非缺陷）。**
`agent.<svc> is not None` 在本仓**就是** capability 是否落实的判据：
`LLMFactory` / `STTFactory` 对未实现的 provider 在**构造期**就 `NotImplementedError`
（`_UNIMPLEMENTED_PROVIDERS`），STT/TTS 后端又都是 `ABC`——抽象方法没实现根本实例化不出来。
所以"对象存在"⇒"实现支持该 capability"是结构性成立的，不是巧合。
`capabilities.services.*.available`（下发给前端的**产品契约**）用的也正是这个判据
（`core/services/capabilities.py`）。判据固化见本文件
`TestInstantiatedImpliesImplemented`。

**(B) 「两种语义混用」——成立（真缺陷）。**
`agent.stt_ok` 读的是一个**粘性标志** `_stt_ok`：`switch_stt` 失败时置 False
**却保留仍然可用的旧后端**（先建后关的失败路径不拆旧后端）。于是同一个受控状态下：

    /debug          → stt_ok = False（"STT 不可用"）
    /debug 面板渲染  → ✗
    capabilities    → available = True（旧后端还在跑）
    真实能力        → STT 仍能转写

两个面互相矛盾，且**错的是 debug**。修法不是再给 `_stt_ok` 打补丁（那会让"上一次
探测结果"与"现在能不能用"永远两套真相），而是让报告读**同一个权威**：运行期句柄。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class _FakeSTT:
    """最小 STT 后端替身。"""

    provider_name = "sensevoice"

    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def model(self) -> str:
        return f"{self.provider_name}/{self._model}"

    async def transcribe(self, audio: bytes, **kwargs) -> str:
        return "测试转写"

    async def transcribe_stream(self, audio: bytes, **kwargs):
        yield "测试转写"

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 1, "error": ""}

    async def close(self) -> None:
        return None


class _FakeTTS:
    """最小 TTS 后端替身（报告只读 `.model`，故只需一个真字符串）。"""

    provider_name = "gpt-sovits"

    def __init__(self, model: str = "default") -> None:
        self._model = model

    @property
    def model(self) -> str:
        return f"{self.provider_name}/{self._model}"


def _make_agent():
    from core.agent import Agent
    from core.config import AppConfig

    return Agent(config=AppConfig())


def _debug_availability(agent) -> dict:
    """走真实投影：`_collect_runtime_stats` 就是 /debug 里 tts_ok/stt_ok 的产生处。"""
    from core.debug.info import DebugInfo, _collect_runtime_stats

    info = DebugInfo()
    _collect_runtime_stats(agent, info)
    return {"tts_ok": info.tts_ok, "stt_ok": info.stt_ok}


def _capabilities_availability(agent) -> dict:
    from core.services.capabilities import build_capabilities

    services = build_capabilities(agent).services
    return {
        "tts_available": services["tts"].available,
        "stt_available": services["stt"].available,
    }


async def _mount_working_stt(agent, model: str = "sensevoice/mounted") -> None:
    with patch("Module.stt.registry.STTFactory.create",
               side_effect=lambda model_id, **kw: _FakeSTT(model_id.split("/", 1)[1])):
        result = await agent.switch_stt(model)
    assert result.get("ok") is True, result


# ---- (B) 两种语义混用：失败切换后 /debug 与真实能力互相矛盾 --------------------


class TestAvailabilityHasOneAuthority:
    """F7-B（PRE_FIX_RED）：`stt_ok` 必须与 capabilities / 真实句柄同源。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_failed_switch_keeps_working_backend_and_reports_available(self):
        """失败的热切换**保留旧后端**（先建后关）——报告必须说"可用"，因为确实可用。"""
        agent = _make_agent()
        await _mount_working_stt(agent, "sensevoice/working")
        assert agent.stt is not None

        # 切到一个建不出来的模型：switch_stt 失败，但**旧后端不被拆除**
        with patch("Module.stt.registry.STTFactory.create",
                   side_effect=RuntimeError("服务不可用")):
            result = await agent.switch_stt("sensevoice/broken")

        assert result.get("ok") is False
        # 事实锚点：运行期句柄仍在（这正是"还能用"的权威）
        assert agent.stt is not None
        assert agent.stt.model == "sensevoice/working"

        assert agent.stt_ok is True, (
            "switch_stt 失败后 stt_ok=False，但旧后端仍在运行期槽位里——"
            "报告与真实能力矛盾（F7-B）"
        )

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_debug_and_capabilities_agree_after_failed_switch(self):
        """跨面一致（§56）：/debug 的 stt_ok 与 capabilities.available 不得互相矛盾。"""
        agent = _make_agent()
        await _mount_working_stt(agent, "sensevoice/working")

        with patch("Module.stt.registry.STTFactory.create",
                   side_effect=RuntimeError("服务不可用")):
            await agent.switch_stt("sensevoice/broken")

        debug = _debug_availability(agent)
        caps = _capabilities_availability(agent)

        assert debug["stt_ok"] == caps["stt_available"] == (agent.stt is not None), (
            f"/debug 说 {debug}，capabilities 说 {caps}，"
            f"真实句柄存在={agent.stt is not None}——同一事实三个答案（F7-B）"
        )

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_successful_switch_reports_available(self):
        """正向对照：切换成功后两面都说可用（防"一律报 False"的假修）。"""
        agent = _make_agent()
        await _mount_working_stt(agent, "sensevoice/working")

        assert agent.stt_ok is True
        assert _debug_availability(agent)["stt_ok"] is True
        assert _capabilities_availability(agent)["stt_available"] is True


# ---- (A)/F7-3：句柄不存在 ⇒ 不可用 -------------------------------------------


class TestNoHandleMeansNotAvailable:
    """F7-3：后端不存在时两处都必须报不可用。"""

    @pytest.mark.service
    def test_fresh_agent_reports_both_unavailable(self):
        agent = _make_agent()
        assert agent.stt is None and agent.tts is None

        assert agent.stt_ok is False
        assert _debug_availability(agent) == {"tts_ok": False, "stt_ok": False}
        assert _capabilities_availability(agent) == {
            "tts_available": False, "stt_available": False}

    @pytest.mark.service
    def test_tts_availability_follows_its_handle(self):
        """tts_ok 本来就读运行期句柄：这里是**产品契约守卫**，防止修 F7 时改坏它。"""
        agent = _make_agent()
        assert _debug_availability(agent)["tts_ok"] is False
        agent._tts = _FakeTTS()      # noqa: SLF001 — 模拟"后端已建"
        assert _debug_availability(agent)["tts_ok"] is True
        assert _capabilities_availability(agent)["tts_available"] is True


# ---- §26/PCT-4：报告不得自己发起网络探测 --------------------------------------


class TestAvailabilityReportingNeverProbes:
    """`/debug` 与 capabilities 是**纯投影**：读运行期已知状态，不发网络请求。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_reporting_does_no_network_io(self):
        import socket

        def _boom(*args, **kwargs):
            raise AssertionError("可用性报告发起了网络连接（PCT-4/§26 违规）")

        agent = _make_agent()
        await _mount_working_stt(agent, "sensevoice/working")

        with patch.object(socket.socket, "connect", _boom), \
             patch.object(socket, "create_connection", _boom):
            _debug_availability(agent)
            _capabilities_availability(agent)
            assert agent.stt_ok is True


# ---- (A) 「实例化 ⇒ 实现存在」：把重分类的依据钉成守卫 ------------------------


class TestInstantiatedImpliesImplemented:
    """`object is not None` 之所以能代表 capability，是因为工厂在构造期就把未实现的
    provider 挡住了。这条不变量必须被守卫，否则"存在性"会重新变成弱判据（PCT-2/§65）。
    """

    @pytest.mark.unit
    def test_factory_refuses_unimplemented_providers(self):
        from Module.llm import LLMFactory
        from Module.stt import STTFactory
        from Module.tts import TTSFactory

        with pytest.raises(NotImplementedError):
            LLMFactory.create("gemini/gemini-1.5-pro")
        with pytest.raises(NotImplementedError):
            STTFactory.create("openai/whisper-1")
        with pytest.raises(NotImplementedError):
            STTFactory.create("deepgram/nova-2")

        assert LLMFactory.is_implemented("gemini") is False
        assert STTFactory.is_implemented("openai") is False
        assert TTSFactory.is_implemented("gpt-sovits") is True

    @pytest.mark.service
    def test_every_instantiable_provider_declares_its_capability(self):
        """遍历 providers.yaml：`is_implemented` 与工厂真实行为必须逐项一致。"""
        from Module.config.providers import load_provider_defaults
        from Module.llm import LLMFactory
        from Module.stt import STTFactory
        from Module.tts import TTSFactory

        cases = [
            ("llm", LLMFactory, {"deepseek/deepseek-v4-flash"},
             {"deepseek/deepseek-v4-flash": {}}),
            ("stt", STTFactory, {"sensevoice/sense-voice"}, {"sensevoice/sense-voice": {}}),
            ("tts", TTSFactory, {"gpt-sovits/default"}, {"gpt-sovits/default": {}}),
        ]
        for service, factory, ok_models, kwargs_by_model in cases:
            declared = set(load_provider_defaults(service))
            for name in declared:
                if name == "custom":
                    continue  # meta-provider：需要调用方提供 base_url
                model_id = f"{name}/probe-model"
                implemented = factory.is_implemented(model_id)
                # 未实现 ⇔ 工厂抛 NotImplementedError（不是"看代码会抛"）
                raised = False
                try:
                    factory.create(model_id, **kwargs_by_model.get(model_id, {}))
                except NotImplementedError:
                    raised = True
                except Exception:
                    raised = False
                assert raised is (not implemented), (
                    f"{service}/{name}: is_implemented={implemented} 但 create() "
                    f"NotImplementedError={raised}"
                )
            for model_id in ok_models:
                assert factory.is_implemented(model_id) is True
