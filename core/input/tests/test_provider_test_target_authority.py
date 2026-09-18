"""F6：provider test 必须测**运行期真正在用的** provider/model。

原始 finding（Batch 1.6 F5 → canonical **F6**）：

    `/api/providers/test` 的 **STT 缺省模型读 config**，而 llm/tts 读 runtime
    ⇒ 测的不是跑的那个。

运行期有权热切换（`Agent.switch_stt`），而切换**有意不改写 config**
（config 是"默认"，`ServiceState`/运行期槽位是"当前"——见 `switch_stt` docstring）。
于是 `config.stt.model` 与 `agent.stt.model` 可以长期不同：

    bootstrap = A（config） → switch_stt(B) → 运行期跑 B，config 仍是 A
    修前：probe 打 A；修后：probe 打 B

本文件钉住：

* **F6-1** 运行期 ≠ config 时，probe 目标 = 运行期；
* **F6-2** 同一 provider、不同 model 时，probe 目标跟运行期 model；
* **F6-3** hot-switch A→B 之后 probe 跟 B（§19 的关键 regression case）；
* **F6-4** 后端未连接时回退 config（不回归"agent 尚未启动"的正常用法）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class _FakeSTT:
    """最小 STT 后端替身：`model` 走协议的真实派生（provider_name + `_model`）。"""

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


def _make_agent():
    """真实 Agent（不 start，无 DB）——与 test_providers_api 同款装配。"""
    from core.agent import Agent
    from core.config import AppConfig

    return Agent(config=AppConfig())


def _request(agent):
    request = MagicMock()
    request.app.get.return_value = agent
    return request


def _probe_target(agent, service: str) -> str:
    """走端点真正用的那个解析函数（`providers_test_handler` 的第一步）。"""
    from core.input.provider_routes import _resolve_test_model

    (provider, model), response = _resolve_test_model(_request(agent), service, "", "")
    assert response is None, "缺省解析不应返回错误信封"
    return f"{provider}/{model}"


# ---- F6-1 / F6-2：probe 目标跟运行期 provider + model --------------------------


class TestProbeTargetFollowsRuntime:
    """F6-1/F6-2（PRE_FIX_RED）：默认（body 不带 provider/model）必须解析到运行期。"""

    @pytest.mark.service
    def test_stt_probe_targets_runtime_provider_not_config(self):
        agent = _make_agent()
        agent.config.stt.model = "sensevoice/config-model"      # 配置权威（bootstrap）
        agent._stt = _FakeSTT("runtime-model")                  # noqa: SLF001 — 运行期句柄

        runtime_target = agent.stt.model
        assert runtime_target == "sensevoice/runtime-model"      # 事实锚点 A
        assert agent.config.stt.model == "sensevoice/config-model"  # 事实锚点 B（两者确实分叉）

        assert _probe_target(agent, "stt") == runtime_target, (
            "provider test 打的是 config 里的模型，而运行期在跑另一个（F6）"
        )

    @pytest.mark.service
    def test_stt_probe_targets_runtime_model_within_same_provider(self):
        """F6-2：provider 相同、model 不同，也必须跟运行期。"""
        agent = _make_agent()
        agent.config.stt.model = "sensevoice/sense-voice"
        agent._stt = _FakeSTT("switched-voice")                 # noqa: SLF001

        assert _probe_target(agent, "stt") == "sensevoice/switched-voice"

    @pytest.mark.service
    def test_llm_and_tts_stay_on_the_runtime_authority(self):
        """llm/tts 本来就对：本修复不得把它们改坏（ALREADY_GREEN_GUARD）。"""
        agent = _make_agent()
        agent.config.tts.model = "minimax/speech-2.8-hd"
        agent.config.stt.model = "sensevoice/sense-voice"
        agent._tts = MagicMock()                                # noqa: SLF001
        agent._tts.model = "gpt-sovits/default"

        assert _probe_target(agent, "llm") == agent.llm.model
        assert _probe_target(agent, "tts") == "gpt-sovits/default"


# ---- F6-3：hot-switch 之后 probe 跟 B（§19 关键 regression） -------------------


class TestProbeTargetFollowsHotSwitch:
    """F6-3（PRE_FIX_RED）：bootstrap=A → switch_stt(B) → probe 必须测 B。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_after_switch_stt_probe_targets_the_new_backend(self):
        agent = _make_agent()
        agent.config.stt.model = "sensevoice/bootstrap-model"

        # 走**真实** `switch_stt` 公开生命周期（含 1s 连通性探测），只把工厂换成替身。
        with patch("Module.stt.registry.STTFactory.create",
                   side_effect=lambda model_id, **kw: _FakeSTT(model_id.split("/", 1)[1])):
            result = await agent.switch_stt("sensevoice/switched-model")

        assert result.get("ok") is True, f"switch_stt 未成功：{result}"
        # 切换**有意不写 config**：这正是 F6 分叉的产生机制
        assert agent.config.stt.model == "sensevoice/bootstrap-model"
        assert agent.stt.model == "sensevoice/switched-model"

        assert _probe_target(agent, "stt") == "sensevoice/switched-model", (
            "热切换后 provider test 仍在测 bootstrap 模型（F6 的关键形态）"
        )

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_switch_then_probe_target_matches_capabilities_model(self):
        """跨面一致（§56）：capabilities 报的 stt model 与 probe 目标必须同源。"""
        from core.services.capabilities import build_capabilities

        agent = _make_agent()
        agent.config.stt.model = "sensevoice/bootstrap-model"
        with patch("Module.stt.registry.STTFactory.create",
                   side_effect=lambda model_id, **kw: _FakeSTT(model_id.split("/", 1)[1])):
            await agent.switch_stt("sensevoice/switched-model")

        caps_model = build_capabilities(agent).services["stt"].model
        assert caps_model == "sensevoice/switched-model"
        assert _probe_target(agent, "stt") == caps_model


# ---- F6-4：后端未连接 → 回退 config（不回归正常用法） -------------------------


class TestProbeTargetFallsBackWhenNoRuntime:
    """F6-4（ALREADY_GREEN_GUARD）：未连接后端时按 config 默认值解析。"""

    @pytest.mark.service
    def test_stt_without_backend_uses_config_default(self):
        agent = _make_agent()
        agent.config.stt.model = "sensevoice/sense-voice"
        assert agent.stt is None

        assert _probe_target(agent, "stt") == "sensevoice/sense-voice"

    @pytest.mark.service
    def test_tts_without_backend_uses_config_default(self):
        agent = _make_agent()
        agent.config.tts.model = "minimax/speech-2.8-hd"
        assert agent.tts is None

        assert _probe_target(agent, "tts") == "minimax/speech-2.8-hd"


# ---- F6-5：端到端（POST /api/providers/test 真的建哪个后端） ------------------


class TestEndpointBuildsTheRuntimeTarget:
    """F6 端到端：端点最终调用 `Factory.create(<probe target>)`。

    §53 要求记录"runtime active = B / probe target = A"两个实际值；
    这里把两者都换成**观察到的工厂入参**，证明修复穿透到真实调用。
    """

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_post_providers_test_creates_runtime_model(self, aiohttp_client):
        from core.input.server import create_app

        agent = _make_agent()
        agent.config.stt.model = "sensevoice/config-model"
        agent._stt = _FakeSTT("runtime-model")                  # noqa: SLF001

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        seen: list[str] = []

        def _capture(model_id: str, **kwargs):
            seen.append(model_id)
            return _FakeSTT(model_id.split("/", 1)[1])

        with patch("Module.stt.registry.STTFactory.create", side_effect=_capture):
            resp = await client.post("/api/providers/test", json={
                "service": "stt", "level": "connectivity",
            })
            data = await resp.json()

        assert data["ok"] is True, data
        assert seen == ["sensevoice/runtime-model"], (
            f"端点建的后端是 {seen}，而运行期在跑 {agent.stt.model}"
        )
