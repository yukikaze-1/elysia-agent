# 测试文件：core/agent/tests/test_restart_rebuild.py
"""`/restart` = **重建 Agent 实例**（S2-A5，marker=service）。

**为什么必须重建**：绝大多数配置在 `Agent.__init__` 就被值拷贝进组件
（`LLMAdapter(model=…)`、各 Repository(`base_url=db_cfg.mongodb_uri`)、
`LongTermMemory(max_entries/max_age_days)`、`SessionManager(max_messages)`、
`CharacterConfig.from_yaml`、`self._user_name`）。旧实现只换 `self._config` 再
`await self.start()`，却照样打印"配置已重新加载"——实测生效面只有
`server.*`/`wake_word`/`stt.*`/`tts.*`/`agent.max_pending_messages`，
`database.*`/`agent.character`/`agent.user_name`/`memory.*`/`llm.model` **全不生效**。

两条守卫：
1. 重启必须**new 一个新实例**，且新配置来自**重新读取的 YAML**；
2. 文件里的 `llm.model` 变了 → **文件优先**（`prefer_config_model=True`），否则
   `_apply_service_state` 会拿持久化的运行期选择把它切回去。
"""

import pytest
from types import SimpleNamespace

from core.agent._lifecycle_services import _LifecycleServices
from core.agent.server import run_agent_service

pytestmark = pytest.mark.service


class TestRestartRebuildsInstance:
    @pytest.mark.asyncio
    async def test_restart_constructs_a_new_agent_with_reloaded_config(self, monkeypatch):
        """第一轮请求重启 → 必须 new 第二个实例，且吃的是重新加载的配置。"""
        models = iter(["model_a", "model_b"])

        class _Loader:
            def __init__(self, *a, **k) -> None:
                pass

            def load(self):
                return SimpleNamespace(llm=SimpleNamespace(model=next(models)))

        monkeypatch.setattr("Module.config.loader.ConfigLoader", _Loader)

        built: list[dict] = []

        class _FakeAgent:
            def __init__(self, **kw) -> None:
                built.append(kw)

            async def run_server(self, *, app_factory):
                return len(built) == 1          # 第一轮要重启，第二轮正常退出

        await run_agent_service(
            config=SimpleNamespace(llm=SimpleNamespace(model="model_a")),
            config_path="config.yaml",
            app_factory=lambda *a, **k: None,
            agent_factory=_FakeAgent,
        )

        assert len(built) == 2, "重启必须**重建实例**（原地 start() 不会让构造期配置生效）"
        assert built[0]["config"].llm.model == "model_a"
        assert built[1]["config"].llm.model == "model_b", "新实例必须吃重新加载的 YAML"
        assert built[0]["config_path"] == "config.yaml"
        assert built[0]["prefer_config_model"] is False, "首次启动没有'文件变了'可言"
        assert built[1]["prefer_config_model"] is True, "YAML 里的模型变了 → 文件优先"

    @pytest.mark.asyncio
    async def test_no_restart_constructs_exactly_one_agent(self, monkeypatch):
        """对照：正常退出（无重启）**不得**多造实例。"""
        class _Loader:
            def __init__(self, *a, **k) -> None:
                pass

            def load(self):
                return SimpleNamespace(llm=SimpleNamespace(model="m"))

        monkeypatch.setattr("Module.config.loader.ConfigLoader", _Loader)
        built: list[dict] = []

        class _FakeAgent:
            def __init__(self, **kw) -> None:
                built.append(kw)

            async def run_server(self, *, app_factory):
                return False

        await run_agent_service(
            config=SimpleNamespace(llm=SimpleNamespace(model="m")),
            config_path=None,          # 无 config_path → 不重新加载
            app_factory=lambda *a, **k: None,
            agent_factory=_FakeAgent,
        )
        assert len(built) == 1
        assert built[0]["prefer_config_model"] is False


class TestConfigWinsWhenFileChanged:
    """`_apply_service_state` 的"文件优先"分支（S2-A5 第二半）。"""

    class _Stub:
        _apply_service_state = _LifecycleServices._apply_service_state

        def __init__(self, *, config_model: str, persisted: str | None,
                     prefer: bool) -> None:
            self._config = SimpleNamespace(llm=SimpleNamespace(model=config_model))
            self.session = SimpleNamespace(
                service_state=SimpleNamespace(llm_model=persisted))
            self._prefer_config_model = prefer
            self.switched: list[str] = []

        async def _apply_service_enabled(self) -> None:
            return None

        async def switch_llm(self, model: str) -> None:
            self.switched.append(model)

    @pytest.mark.asyncio
    async def test_persisted_choice_wins_by_default(self):
        """默认（非重启场景）：用户的运行期选择优先——旧语义不变。"""
        stub = self._Stub(config_model="m_new", persisted="m_old", prefer=False)
        await stub._apply_service_state()          # noqa: SLF001
        assert stub.switched == ["m_old"]

    @pytest.mark.asyncio
    async def test_config_wins_after_restart_when_file_changed(self):
        """重启且 YAML 里的模型变了 → 切到 **config 的模型**（并写回持久化）。"""
        stub = self._Stub(config_model="m_new", persisted="m_old", prefer=True)
        await stub._apply_service_state()          # noqa: SLF001
        assert stub.switched == ["m_new"], "文件里的模型必须生效，而不是被旧选择覆盖"

    @pytest.mark.asyncio
    async def test_no_switch_when_already_agreed(self):
        """两边一致 → 不切换（别制造无谓的 LLM 重建）。"""
        for prefer in (True, False):
            stub = self._Stub(config_model="m", persisted="m", prefer=prefer)
            await stub._apply_service_state()      # noqa: SLF001
            assert stub.switched == []

    @pytest.mark.asyncio
    async def test_no_persisted_model_means_no_switch(self):
        """持久化里没有模型（首次启动）→ 不切换。"""
        stub = self._Stub(config_model="m", persisted=None, prefer=True)
        await stub._apply_service_state()          # noqa: SLF001
        assert stub.switched == []
