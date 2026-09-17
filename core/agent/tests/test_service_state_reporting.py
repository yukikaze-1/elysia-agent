# 测试文件：core/agent/tests/test_service_state_reporting.py
"""F2（Batch 1.6）：**自述**类消费者必须读运行期权威，而不是 bootstrap 配置。

## 缺陷（Batch 1.5 已确认，本批修复）

`get_runtime_config` 工具（`core/tools/runtime_config/plugin.py`）向用户报告
`config.llm.model` / `config.{tts,stt}.model|enabled`——**bootstrap / YAML**。
于是热切换或 `set_service` 之后它的输出逐字节不变，而同一时刻 banner / capabilities /
`/debug/api` 全都报运行期值：**agent 主动告诉用户错误的自身状态**。
冷启动时两者相等，所以这个谎只在切换之后才看得见（这就是它长期存活的原因）。

## 本文件钉住的三件事

1. `Agent.service_runtime_state()` 读的是**运行期槽位**（`_llm.model` / `_stt` /
   `_tts` / 派生开关），不是持久化字段、更不是 config；
2. 走**真实**的 `switch_llm` / `switch_stt` / `switch_tts` / `set_service` /
   `_apply_service_state` 之后，快照跟着变；
3. 各类报告器（工具 / capabilities / banner 帮助方法）在**同一时刻**报同一个模型
   ——它们读的是同一个权威。

详见 `docs/review/2026-09-16-Batch1.5-config-truthfulness-执行记录.md` §2 F2。
"""

from __future__ import annotations

import ast
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent.agent import Agent
from core.schema.service_state import ServiceState

pytestmark = pytest.mark.service


# ═══════════════════════════════════════════════════════════════════════════════
# 测试装置
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeBackend:
    """LLM/STT/TTS 后端的统一替身（只需 model / test / close / raw_client）。"""

    def __init__(self, model_id: str) -> None:
        self.model = model_id
        self.raw_client = SimpleNamespace(model=model_id)

    async def test(self) -> dict:
        return {"ok": True}

    async def close(self) -> None:
        return None

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        return ""


class _Factories:
    """把三个 registry 的 `create` 换成假后端，并记录收到过哪些模型 id。"""

    def __init__(self) -> None:
        self.created: dict[str, list[str]] = {"llm": [], "stt": [], "tts": []}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from Module.llm import LLMFactory
        from Module.stt import STTFactory
        from Module.tts import TTSFactory

        def _make(svc: str):
            def _create(model_id: str, **kwargs: Any) -> _FakeBackend:
                self.created[svc].append(model_id)
                return _FakeBackend(model_id)
            return _create

        monkeypatch.setattr(LLMFactory, "create", staticmethod(_make("llm")))
        monkeypatch.setattr(STTFactory, "create", staticmethod(_make("stt")))
        monkeypatch.setattr(TTSFactory, "create", staticmethod(_make("tts")))


def _host(
    *,
    config_model: str = "deepseek/deepseek-v4-flash",
    tts_config_model: str = "minimax/speech-2.8-hd",
    stt_config_model: str = "sensevoice/sense-voice",
    tts_config_enabled: bool = True,
    stt_config_enabled: bool = True,
) -> Agent:
    """`object.__new__(Agent)` 真身替身——property 会真的执行（MagicMock 不会）。

    只用**真** mixin 方法：`switch_llm`/`switch_stt`/`switch_tts`/`set_service`/
    `_apply_service_state`/`service_runtime_state`/`_start_tool_context`。
    外围子系统（cognitive / output / scheduler / tool_adapter）用替身。
    """
    a = object.__new__(Agent)
    a._config = SimpleNamespace(
        llm=SimpleNamespace(model=config_model),
        stt=SimpleNamespace(model=stt_config_model, enabled=stt_config_enabled),
        tts=SimpleNamespace(model=tts_config_model, enabled=tts_config_enabled,
                            rate_limit_rpm=0),
        live2d=SimpleNamespace(static_image=""),
        server=SimpleNamespace(host="127.0.0.1", port=8765),
    )
    session = SimpleNamespace(
        service_state=ServiceState(), save=AsyncMock(), save_state=AsyncMock(),
        messages=[],
    )
    a._session_manager = SimpleNamespace(active=session)
    a._llm = None                    # 由 _install_llm 填
    a._stt = None
    a._tts = None
    a._stt_ok = False
    a._cognitive = MagicMock()
    a._cognitive.configure = MagicMock()
    # `_sync_tts_gate` 会 await 它（真实的 TTSStreamManager.set_enabled 是协程）
    a._cognitive.tts_manager.set_enabled = AsyncMock()
    a._bus = MagicMock()
    a._output = MagicMock()
    a._output.channels = []
    a._scheduler = MagicMock()
    a._tool_adapter = MagicMock()
    a._tool_adapter.start = AsyncMock()
    a._tool_adapter.tool_context = SimpleNamespace(services={})
    a._token_tracker = MagicMock()
    a._started_at = 0.0
    a.status = MagicMock()
    a.status.current_name.return_value = "idle"
    a._prefer_config_model = False
    return a


def _install_llm(host: Agent, model_id: str) -> Any:
    """给 host 装一个**真** LLMAdapter（后端由已安装的假工厂产生）。"""
    from core.adapter.llm_adapter import LLMAdapter

    adapter = LLMAdapter(model_id)
    host._llm = adapter
    return adapter


def _chain_mentions_config(node: Any) -> bool:
    """`…model` 属性读点的 value 链上有没有 `config` / `_config` 段。

    必须走**整条链**并且也认链尾的**裸名**：真实写法有两种——
    `self._config.llm.model`（链上是 `_config` 属性）与 `config.tts.model`
    （链尾是 `Name("config")`）。只匹配"父属性就叫 config"会漏掉前者（Challenger
    用注入实验证伪过第一版），只认属性会漏掉后者（本文件的自证用例抓到过）。
    """
    cursor: Any = node
    while True:
        if isinstance(cursor, ast.Attribute):
            if cursor.attr in ("config", "_config"):
                return True
            cursor = cursor.value
        elif isinstance(cursor, ast.Name):
            return cursor.id in ("config", "_config")
        else:
            return False


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.delenv("TASK_SYSTEM_MODEL", raising=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 快照读运行期槽位
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuntimeSnapshotReadsLiveSlots:
    def test_reports_live_slots_not_persisted_or_config(self, monkeypatch):
        """三处权威同时不同 → 快照必须报**运行期**那一份。

        构造：config=YAML 值，service_state=持久化值，运行期槽位=第三个值。
        只有运行期是对的（持久化是"上次的选择"，config 是 bootstrap）。
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
        _Factories().install(monkeypatch)
        host = _host()
        _install_llm(host, "deepseek/deepseek-v4-pro")          # 运行期
        host.session.service_state = ServiceState(llm_model="deepseek/deepseek-v4-flash")
        host._stt = _FakeBackend("sensevoice/sense-voice-runtime")
        host._tts = _FakeBackend("minimax/speech-2.8-turbo")

        state = host.service_runtime_state()

        assert state.llm_model == "deepseek/deepseek-v4-pro"
        assert state.stt_model == "sensevoice/sense-voice-runtime"
        assert state.tts_model == "minimax/speech-2.8-turbo"
        assert state.stt_enabled is True and state.tts_enabled is True

    def test_unconnected_backends_report_none(self, monkeypatch):
        """后端对象不存在 → 模型字段为 None（与 capabilities 的 available=False 同判据）。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
        _Factories().install(monkeypatch)
        host = _host()
        _install_llm(host, "deepseek/deepseek-v4-pro")

        state = host.service_runtime_state()

        assert state.stt_model is None and state.tts_model is None

    def test_enabled_comes_from_the_derived_property(self, monkeypatch):
        """开关字段是**派生**值（会话三态 → config 回退），不是 config 本身。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
        _Factories().install(monkeypatch)
        host = _host(tts_config_enabled=True)
        _install_llm(host, "deepseek/deepseek-v4-pro")
        host.session.service_state.tts_enabled = False     # 用户显式关掉（压过 config）

        assert host.service_runtime_state().tts_enabled is False

    def test_snapshot_never_reads_config_models(self, monkeypatch):
        """结构性（belt）：本方法**不得**引用 `config.*.model`——那正是 F2 的形态。

        主要守卫是上面那条**行为**用例（三处权威同时不同 → 只报运行期那个）。
        本条的判据是 AST：对每个 `…model` 属性读点，沿 `value` 链向上找有没有
        `config` / `_config` 段。

        ⚠️ **判据必须走整条链**：真实写法是 `self._config.llm.model`（父属性是 `llm`，
        不是 `config`）——第一版只匹配"父属性就叫 config"，被 Challenger 用注入实验
        证伪（注入缺陷 → 命中 0 处，**假绿**）。本用例的判据自己也做过注入反证：
        `self._config.llm.model` / `self._config.stt.model` / `config.tts.model`
        三种写法都能命中。
        """
        import pathlib

        src = pathlib.Path("core/agent/_lifecycle_services.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "service_runtime_state"
        )
        offenders = [
            f"line {n.lineno}: {ast.dump(n)}"
            for n in ast.walk(fn)
            if isinstance(n, ast.Attribute) and n.attr == "model"
            and _chain_mentions_config(n.value)
        ]
        assert not offenders, f"运行期快照不得读 config 的模型字段：{offenders}"

    def test_the_ast_guard_actually_sees_the_defect_shape(self, monkeypatch):
        """反证：把**真实缺陷写法**喂给上面那条判据 → 必须命中。

        断言"某个缺陷存在"的用例必须把期望值现算（`CLAUDE.md` 的门禁三假绿之一）：
        这里直接把 `self._config.llm.model` / `config.tts.model` 这两种写法编译成
        AST 再跑同一个判据，证明它不是空网。
        """
        def _hits(source: str) -> int:
            tree = ast.parse(source)
            return sum(
                1 for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == "model"
                and _chain_mentions_config(n.value)
            )

        assert _hits("x = self._config.llm.model") == 1
        assert _hits("x = self._config.stt.model") == 1
        assert _hits("x = config.tts.model") == 1
        # 反向：运行期槽位写法不该被误判
        assert _hits("x = self._llm.model") == 0
        assert _hits("x = self._stt.model if self._stt is not None else None") == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 真实 switch 之后快照跟着变（F2-2 / F2-3）
# ═══════════════════════════════════════════════════════════════════════════════


class TestHotSwitchUpdatesTheReportedState:
    @pytest.mark.asyncio
    async def test_switch_llm(self, monkeypatch):
        """F2-2：A → B 之后，自述报 **B**（config 仍是 A）。"""
        _env(monkeypatch)
        factories = _Factories()
        factories.install(monkeypatch)
        host = _host()
        llm = _install_llm(host, "deepseek/deepseek-v4-flash")

        assert host.service_runtime_state().llm_model == "deepseek/deepseek-v4-flash"
        assert (await host.switch_llm("deepseek/deepseek-v4-pro"))["ok"] is True

        assert host.service_runtime_state().llm_model == "deepseek/deepseek-v4-pro"
        assert host.config.llm.model == "deepseek/deepseek-v4-flash", "config 不得被改写"
        assert llm.model == "deepseek/deepseek-v4-pro"
        await llm.close()

    @pytest.mark.asyncio
    async def test_switch_stt_and_tts(self, monkeypatch):
        """F2-3：STT/TTS 同样——切换后的值就是自述报的值。"""
        _env(monkeypatch)
        factories = _Factories()
        factories.install(monkeypatch)
        host = _host()
        llm = _install_llm(host, "deepseek/deepseek-v4-flash")

        assert (await host.switch_stt("sensevoice/sense-voice-runtime"))["ok"] is True
        assert (await host.switch_tts("minimax/speech-2.8-turbo"))["ok"] is True

        state = host.service_runtime_state()
        assert state.stt_model == "sensevoice/sense-voice-runtime"
        assert state.tts_model == "minimax/speech-2.8-turbo"
        assert host.config.stt.model == "sensevoice/sense-voice", "config 不得被改写"
        assert host.config.tts.model == "minimax/speech-2.8-hd", "config 不得被改写"
        await llm.close()

    @pytest.mark.asyncio
    async def test_set_service_updates_reported_enabled(self, monkeypatch):
        """`set_service('tts', False)` 之后自述必须说"禁用"（config 仍是 True）。"""
        _env(monkeypatch)
        _Factories().install(monkeypatch)
        host = _host(tts_config_enabled=True)
        llm = _install_llm(host, "deepseek/deepseek-v4-flash")
        host._tts = _FakeBackend("minimax/speech-2.8-hd")

        assert host.service_runtime_state().tts_enabled is True
        assert (await host.set_service("tts", False))["ok"] is True

        assert host.service_runtime_state().tts_enabled is False
        assert host.config.tts.enabled is True, "config 不得被改写"
        await llm.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 重启应用规则之后快照报的是**被应用**的那个（F2-4）
# ═══════════════════════════════════════════════════════════════════════════════


class TestRestartAuthorityIsReflected:
    @pytest.mark.asyncio
    async def test_persisted_choice_wins_by_default(self, monkeypatch):
        """无"文件变了"信号 → 应用**持久化**选择，自述报它（config 仍是旧值）。"""
        _env(monkeypatch)
        _Factories().install(monkeypatch)
        host = _host(config_model="deepseek/deepseek-v4-flash")
        llm = _install_llm(host, "deepseek/deepseek-v4-flash")
        host.session.service_state = ServiceState(llm_model="deepseek/deepseek-v4-pro")

        await host._apply_service_state()  # noqa: SLF001 —— 被测算的启动步骤

        assert host.service_runtime_state().llm_model == "deepseek/deepseek-v4-pro"
        await llm.close()

    @pytest.mark.asyncio
    async def test_config_wins_when_the_file_changed(self, monkeypatch):
        """`prefer_config_model` → **文件优先**，自述报 config 的模型（覆盖持久化选择）。"""
        _env(monkeypatch)
        _Factories().install(monkeypatch)
        host = _host(config_model="deepseek/deepseek-v4-flash")
        llm = _install_llm(host, "deepseek/deepseek-v4-pro")
        host.session.service_state = ServiceState(llm_model="deepseek/deepseek-v4-pro")
        host._prefer_config_model = True

        await host._apply_service_state()  # noqa: SLF001

        assert host.service_runtime_state().llm_model == "deepseek/deepseek-v4-flash"
        await llm.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 接线与"报告器不分歧"（F2-5）
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolContextWiring:
    @pytest.mark.asyncio
    async def test_tool_context_gets_a_live_runtime_provider(self, monkeypatch):
        """`_start_tool_context` 注入的是**活口**（bound method），不是启动期快照。

        判据：注入的可调用对象在**切换之后**返回新值。写死的快照必然失败——
        本步骤是启动第 2 步，而决定性步骤（service_state / tts）在第 5/6 步。
        """
        _env(monkeypatch)
        _Factories().install(monkeypatch)
        host = _host()
        llm = _install_llm(host, "deepseek/deepseek-v4-flash")

        await host._start_tool_context()  # noqa: SLF001

        services = host._tool_adapter.start.await_args.kwargs["extra_services"]
        provider = services["_service_runtime"]
        assert callable(provider)
        assert provider().llm_model == "deepseek/deepseek-v4-flash"

        assert (await host.switch_llm("deepseek/deepseek-v4-pro"))["ok"] is True
        assert provider().llm_model == "deepseek/deepseek-v4-pro", (
            "注入的必须是活口——启动期快照再过两步就过期了")
        await llm.close()


class TestReportersAgree:
    @pytest.mark.asyncio
    async def test_tool_capabilities_and_banner_report_the_same_model(self, monkeypatch):
        """F2-5：同一时刻工具 / capabilities / banner 不得报不同模型。

        三者的实现点不同（工具读 `_service_runtime`；capabilities 读 `agent.llm.model`
        等公开属性；banner 读 `self._llm.model`），但**权威只有一个**——运行期槽位。
        本用例在热切换之后同时取三处输出并逐项比对。
        """
        from core.services.capabilities import build_capabilities
        from core.tools.runtime_config.plugin import RuntimeConfigPlugin, RuntimeConfigParams
        from Module.tool.protocol import ToolContext

        _env(monkeypatch)
        _Factories().install(monkeypatch)
        host = _host()
        llm = _install_llm(host, "deepseek/deepseek-v4-flash")
        host._stt = _FakeBackend("sensevoice/sense-voice")
        host._tts = _FakeBackend("minimax/speech-2.8-hd")

        assert (await host.switch_llm("deepseek/deepseek-v4-pro"))["ok"] is True
        assert (await host.switch_stt("sensevoice/sense-voice-runtime"))["ok"] is True
        assert (await host.switch_tts("minimax/speech-2.8-turbo"))["ok"] is True
        assert (await host.set_service("tts", False))["ok"] is True

        services = host.service_runtime_state()
        ctx = ToolContext(session_id="s1", services={
            "_service_runtime": host.service_runtime_state,
            "config": host.config,
        })
        tool_out = await RuntimeConfigPlugin().execute(
            ctx=ctx, params=RuntimeConfigParams())
        caps = build_capabilities(host).services

        # 工具 vs capabilities
        assert services.llm_model in tool_out
        assert caps["llm"].model == services.llm_model
        assert caps["stt"].model == services.stt_model
        assert caps["tts"].model == services.tts_model
        assert caps["stt"].enabled is services.stt_enabled
        assert caps["tts"].enabled is services.tts_enabled
        # banner（两个助手方法）
        assert host._stt_banner_status() == f"已连接 (model={services.stt_model})"  # noqa: SLF001
        assert host._tts_banner_status() == f"已连接 (model={services.tts_model})"  # noqa: SLF001
        # 工具报的开关与派生值一致
        assert "TTS: 禁用" in tool_out and services.tts_enabled is False
        await llm.close()
