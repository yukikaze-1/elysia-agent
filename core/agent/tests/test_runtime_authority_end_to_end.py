# 测试文件：core/agent/tests/test_runtime_authority_end_to_end.py
"""Batch 1.6 / §26 + §27：**整只 Agent** 上的热切换与重启一致性。

前三个 commit 的用例都在"缝"上测（`_start_task_system` / `service_runtime_state` /
`run_agent_service`）。本条把三件事合起来跑一遍**真实生命周期**：

    start（真 `Agent.start()`，20 步）→ switch_llm/switch_stt/switch_tts/set_service
    → 检查 runtime truth / 持久化 / 各 reporter 一致 → stop → start（同实例重启）
    → 再检查一遍

它回答的是任务书 §26（"不要只测 LLM"：三类服务都要满足
`switch → runtime truth 更新 → persisted 更新 → debug/report 更新 → 依赖方不持旧资源`）
与 §27（`start → switch → persist → stop → restart` 之后 main model / workflow model /
tts / stt / runtime config / banner 一致）。

存储用现成的**真实库防线**（`fake_storage`：Mongo 导流到内存 fake、Milvus 拦截为桩），
出站用三个 registry 的假后端——不连任何外部服务，但走的是**真**的生产代码路径。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.agent.agent import Agent
from core.config import AppConfig

#: `fake_storage` 是"本测试不碰真实库"的显式声明（见根 conftest 的「真实库防线」）。
pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]


class _RawClient:
    """够用的 raw client 替身——**只**要 `is_closed()` 语义正确（F1 的全部判据）。"""

    def __init__(self, model_id: str) -> None:
        self.model = model_id
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


class _FakeBackend:
    def __init__(self, model_id: str) -> None:
        self.model = model_id
        self.raw_client = _RawClient(model_id)

    async def test(self) -> dict:
        return {"ok": True}

    def get_preprocess(self) -> None:
        """TTS 后端协议的一部分（`_cognitive.configure(tts=…)` 会读它）。"""
        return None

    async def close(self) -> None:
        await self.raw_client.close()

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        return ""

    async def chat_stream(self, messages: list[dict], **kwargs: Any) -> Any:
        yield ""

    async def stream_think(self, messages: list[dict], **kwargs: Any) -> Any:
        yield {"content": "", "tool_calls": None}


def _install_fake_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    from Module.llm import LLMFactory
    from Module.stt import STTFactory
    from Module.tts import TTSFactory

    def _create(model_id: str, **kwargs: Any) -> _FakeBackend:
        return _FakeBackend(model_id)

    monkeypatch.setattr(LLMFactory, "create", staticmethod(_create))
    monkeypatch.setattr(STTFactory, "create", staticmethod(_create))
    monkeypatch.setattr(TTSFactory, "create", staticmethod(_create))


async def _tool_report(agent: Agent) -> str:
    """走**真**工具（经真 `ToolContext`），拿到 agent 会看到的自述文本。"""
    from core.tools.runtime_config.plugin import RuntimeConfigPlugin, RuntimeConfigParams

    plugin = RuntimeConfigPlugin()
    ctx = agent.tool_adapter.tool_context.clone_for_call(call_id="e2e-check")
    return str(await plugin.execute(ctx=ctx, params=RuntimeConfigParams()))


def _assert_reporters_agree(agent: Agent, tool_output: str, *, label: str) -> None:
    """同一时刻：runtime 快照 / capabilities / banner / 工具输出 必须互相一致。"""
    from core.services.capabilities import build_capabilities

    state = agent.service_runtime_state()
    caps = build_capabilities(agent).services

    assert caps["llm"].model == state.llm_model == agent.llm.model, label
    assert caps["stt"].model == state.stt_model, label
    assert caps["tts"].model == state.tts_model, label
    assert caps["stt"].enabled is state.stt_enabled, label
    assert caps["tts"].enabled is state.tts_enabled, label
    # banner 两行（STT/TTS）读的是同一批运行期槽位
    expected_stt = (f"已连接 (model={state.stt_model})" if state.stt_model
                    else "未连接")
    expected_tts = (f"已连接 (model={state.tts_model})" if state.tts_model
                    else "未连接")
    assert agent._stt_banner_status() == expected_stt, label  # noqa: SLF001
    assert agent._tts_banner_status() == expected_tts, label  # noqa: SLF001
    # 工具（名字叫 runtime 的那个）必须报同一批值
    assert f"模型: {state.llm_model}" in tool_output, label


class TestFullLifecycleRuntimeAuthority:
    @pytest.mark.asyncio
    async def test_switch_then_restart_keeps_every_reporter_consistent(self, monkeypatch):
        """§26 + §27 的合并序列（真 `Agent.start()` / `stop()` / `start()`）。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
        monkeypatch.delenv("TASK_SYSTEM_MODEL", raising=False)
        _install_fake_factories(monkeypatch)

        agent = Agent(config=AppConfig())
        await agent.start()
        try:
            # ── 启动态：三方一致 ──
            _assert_reporters_agree(agent, await _tool_report(agent), label="startup")
            task_llm_first = agent._task_llm_backend  # noqa: SLF001
            assert task_llm_first is not None, "F1：工作流的 task LLM 必须由工作流自己持有"
            assert task_llm_first is not agent._llm._backend  # noqa: SLF001
            assert task_llm_first.raw_client.is_closed() is False

            # ── 热切换三类服务（走真 switch_* / set_service）──
            assert (await agent.switch_llm("deepseek/deepseek-v4-pro"))["ok"] is True
            assert (await agent.switch_stt("sensevoice/sense-voice-runtime"))["ok"] is True
            assert (await agent.switch_tts("minimax/speech-2.8-turbo"))["ok"] is True
            assert (await agent.set_service("tts", True))["ok"] is True

            state = agent.service_runtime_state()
            assert state.llm_model == "deepseek/deepseek-v4-pro"
            assert state.stt_model == "sensevoice/sense-voice-runtime"
            assert state.tts_model == "minimax/speech-2.8-turbo"
            assert state.tts_enabled is True
            # 持久化权威（跨重启的那份）已更新
            ss = agent.session.service_state
            assert (ss.llm_model, ss.stt_model, ss.tts_model) == (
                "deepseek/deepseek-v4-pro", "sensevoice/sense-voice-runtime",
                "minimax/speech-2.8-turbo")
            assert ss.tts_enabled is True
            _assert_reporters_agree(agent, await _tool_report(agent), label="after switch")

            # F1：热切换**不得**关掉工作流的 LLM 资源
            assert task_llm_first.raw_client.is_closed() is False, (
                "主 LLM 热切换关闭了工作流的 client")
        finally:
            await agent.stop()

        # ── 同实例重启（stop → start，§27 的 restart 半）──
        await agent.start()
        try:
            state = agent.service_runtime_state()
            # 重启规则：持久化选择优先（config 的 llm.model 没变过）
            assert state.llm_model == "deepseek/deepseek-v4-pro", (
                "重启后运行期模型应回到持久化的运行期选择")
            # TTS 走 `ss.tts_model or config.tts.model` 重建 → 持久化的那个
            assert state.tts_model == "minimax/speech-2.8-turbo"
            assert state.tts_enabled is True
            # STT 后端只在 server 模式建（`run_server` 的启动段），本测试没跑服务循环
            # ⇒ None；关键是**所有 reporter 都同意它不是"在用一个 STT 模型"**
            assert state.stt_model is None

            # F1：task LLM 是**新建**的，且旧的那个已随停机关闭
            task_llm_second = agent._task_llm_backend  # noqa: SLF001
            assert task_llm_second is not None
            assert task_llm_second is not task_llm_first, "重启必须重建 task LLM"
            assert task_llm_first.raw_client.is_closed() is True, "旧的必须被关掉"
            assert task_llm_second.raw_client.is_closed() is False

            tool_output = await _tool_report(agent)
            _assert_reporters_agree(agent, tool_output, label="after restart")
            assert "未连接" in tool_output, "STT 未连接必须如实说，不得拿 YAML 模型顶上"
        finally:
            await agent.stop()
