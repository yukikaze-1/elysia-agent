# 测试文件：core/agent/tests/test_lifecycle_steps.py
"""AgentLifecycle _SHUTDOWN_STEPS / _STARTUP_STEPS 结构验证。"""

import logging
from unittest import mock

import pytest
from core.agent._lifecycle_services import _LifecycleServices
from core.agent.lifecycle import AgentLifecycle
from core.schema.service_state import ServiceState
from core.schema.types import SessionId

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
        """flush 步骤排在 stop/close 步骤之前（C4/P1-13 新顺序）。"""
        steps = AgentLifecycle._SHUTDOWN_STEPS
        index = {name: i for i, (name, _) in enumerate(steps)}
        flush_indices = [i for i, (name, _) in enumerate(steps)
                         if name.startswith("flush")]
        # C4/P1-13：先关 admission/consumer + generation，再 flush tail——
        # flush 期间不再产生新 after-response tail。
        assert index["stop_event_task"] < min(flush_indices), \
            "stop_event_task 必须先于 flush（关 admission，防 flush 期间新 tail）"
        assert index["stop_cognitive_tts"] < min(flush_indices), \
            "stop_cognitive_tts 必须先于 flush（停 generation，防新 tail）"
        # 资源 close（stop_db_health 起）在 flush 之后
        assert max(flush_indices) < index["stop_db_health"], \
            "flush 必须在关闭持久层资源之前"


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

    def test_character_integrity_step_registered(self):
        """P3：多角色数据完整性自检是启动步骤（把故障提前到启动日志）。"""
        names = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert "character_integrity" in names


class TestCharacterIntegrityStartupStep:
    """_check_character_data_integrity 启动步骤：只读、不阻塞、结果挂 agent。"""

    @pytest.mark.asyncio
    async def test_records_report_and_warns_on_gaps(self, caplog):
        """发现缺口 → 显著告警 + 报告挂到 agent（供排查）。"""
        import logging

        from core.agent._lifecycle_persistence import _LifecyclePersistence

        class _Repo:
            def __init__(self, value):
                self._value = value

            async def count_incomplete_character_binding(self):
                return self._value

            async def count_assistant_without_character(self):
                return self._value

        class _Agent(_LifecyclePersistence):
            character_profiles = True

        agent = _Agent()
        agent._session_repo = _Repo(50)   # noqa: SLF001
        agent._message_repo = _Repo(14)   # noqa: SLF001

        with caplog.at_level(logging.WARNING):
            await agent._check_character_data_integrity()  # noqa: SLF001

        report = agent._character_integrity_report  # noqa: SLF001
        assert report.scanned is True
        assert report.sessions_missing_binding == 50
        assert report.assistant_messages_missing_character == 14
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_storage_failure_does_not_break_startup(self):
        """存储不可用 → 未扫描，且启动步骤不抛异常。"""
        from core.agent._lifecycle_persistence import _LifecyclePersistence

        class _DownRepo:
            async def count_incomplete_character_binding(self):
                raise RuntimeError("mongo down")

            async def count_assistant_without_character(self):
                raise RuntimeError("mongo down")

        class _Agent(_LifecyclePersistence):
            character_profiles = True

        agent = _Agent()
        agent._session_repo = _DownRepo()   # noqa: SLF001
        agent._message_repo = _DownRepo()   # noqa: SLF001

        await agent._check_character_data_integrity()  # noqa: SLF001

        assert agent._character_integrity_report.scanned is False  # noqa: SLF001


class TestSchemaDriftStep:
    """_check_schema_drift 启动步骤：只读、不阻塞、结果挂 agent。

    背景：`ensure_index` 只增不删，改过索引键后旧索引留在库里并静默改变行为
    （实测：sessions 的 30 天 TTL 索引从代码删除后仍存留）。
    """

    def test_schema_drift_step_registered(self):
        """索引 schema 漂移自检是启动步骤（把故障提前到启动日志）。"""
        names = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert "schema_drift" in names

    def test_schema_drift_runs_after_character_integrity(self):
        """置于 character_integrity 之后（同为只读诊断，且那时 repo 已连接）。"""
        names = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert names.index("character_integrity") < names.index("schema_drift")

    @pytest.mark.asyncio
    async def test_orphan_index_logged_as_warning(self, caplog):
        """检出孤儿索引 → 显著 warning，且结果挂到 agent。"""
        from unittest.mock import AsyncMock, MagicMock

        from core.agent._lifecycle_persistence import _LifecyclePersistence
        from core.storage.schema_drift import EXPECTED_INDEXES

        def _indexes(coll: str) -> list[dict]:
            items = [{"name": "_id_"}, *[{"name": n} for n in EXPECTED_INDEXES[coll]]]
            if coll == "sessions":
                items.append({"name": "updated_at_1"})  # 复刻实测残留
            return items

        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=_indexes)
        repo = MagicMock()
        repo.backend = backend

        class _Agent(_LifecyclePersistence):
            pass

        agent = _Agent()
        agent._session_repo = repo  # noqa: SLF001

        with caplog.at_level(logging.WARNING, logger="core.storage.schema_drift"):
            await agent._check_schema_drift()  # noqa: SLF001

        assert agent._schema_drift_report.has_drift is True  # noqa: SLF001
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_clean_schema_is_quiet(self, caplog):
        """无漂移 → 不产生 warning。"""
        from unittest.mock import AsyncMock, MagicMock

        from core.agent._lifecycle_persistence import _LifecyclePersistence
        from core.storage.schema_drift import EXPECTED_INDEXES

        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        backend = MagicMock()

        def _fake_indexes(coll: str) -> list[dict]:
            """"干净"的库必须**连选项也对**——S2-B1 起选项冲突会被报出来。

            只给名字不给选项的假库，在声明了 unique/TTL 的索引上就是"选项不符"，
            不再算干净（如实的判据变了，fixture 必须跟着变）。
            """
            out = [{"name": "_id_"}]
            for name in EXPECTED_INDEXES[coll]:
                idx: dict = {"name": name}
                idx.update(EXPECTED_INDEX_OPTIONS.get(coll, {}).get(name, {}))
                out.append(idx)
            return out

        backend.list_indexes = AsyncMock(side_effect=_fake_indexes)
        repo = MagicMock()
        repo.backend = backend

        class _Agent(_LifecyclePersistence):
            pass

        agent = _Agent()
        agent._session_repo = repo  # noqa: SLF001

        with caplog.at_level(logging.DEBUG, logger="core.storage.schema_drift"):
            await agent._check_schema_drift()  # noqa: SLF001

        assert agent._schema_drift_report.has_drift is False  # noqa: SLF001
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_missing_backend_does_not_break_startup(self):
        """仓储未连接（backend=None）→ 未扫描，启动步骤不抛异常。"""
        from core.agent._lifecycle_persistence import _LifecyclePersistence

        class _Agent(_LifecyclePersistence):
            pass

        agent = _Agent()
        agent._session_repo = None  # noqa: SLF001

        await agent._check_schema_drift()  # noqa: SLF001

        assert agent._schema_drift_report.scanned is False  # noqa: SLF001


def _real_service_agent(*, stt_default: bool = False, tts_default: bool = False):
    """`object.__new__(Agent)` 真身替身——**#43 之后必须用它**。

    `agent.stt_enabled` / `agent.tts_enabled` 现在是**派生 property**（权威 = 会话
    `service_state` 三态 + config 回退）。在 `MagicMock` 上访问它们只会拿到自动属性
    （恒真、且与 session 无关），于是"开关是否跟随会话"这件事根本测不到——本文件
    第一版正是这样假绿的。真身替身让 property 真的执行。
    """
    from core.agent.agent import Agent

    a = object.__new__(Agent)
    # `session` 是只读 property（→ `_session_manager.active`），真身替身上必须从
    # 底层字段造：直接 `a.session = ...` 会 AttributeError。
    a._session_manager = mock.MagicMock()
    a._session_manager.active = mock.MagicMock()
    a._session_manager.active.save = mock.AsyncMock()
    a._config = mock.MagicMock()
    a._config.stt.enabled = stt_default
    a._config.tts.enabled = tts_default
    a._cognitive = mock.MagicMock()
    a._cognitive._tts_mgr.set_enabled = mock.AsyncMock()
    a._tts = None
    # S2-A5：`Agent.__init__` 设置的"重启时以配置为准"标志。替身绕过 `__init__`，
    # 必须显式补上——否则 `_apply_service_state` 会 AttributeError（它不是可选能力，
    # 真身上恒有值；用 getattr 兜底会把"忘了设"变成静默回退，正是本仓忌讳的那种写法）。
    a._prefer_config_model = False
    return a


def _wire_service_enabled(agent) -> None:
    """把真实的 `_apply_service_enabled` 接到 mock agent 上。

    这些测试把 `_LifecycleServices` 当**非绑定 mixin** 用（`_LifecycleServices._x(agent)`），
    而 `agent` 是 `MagicMock`——`self._apply_service_enabled()` 只会拿到一个自动属性
    （不是 awaitable，直接 TypeError）。接上真实实现，测试才仍然覆盖三态逻辑本身，
    而不是降级成"某个 mock 被调用过"。
    """
    async def _apply() -> None:
        await _LifecycleServices._apply_service_enabled(agent)

    async def _sync() -> None:
        await _LifecycleServices._sync_tts_gate(agent)

    agent._apply_service_enabled = mock.AsyncMock(side_effect=_apply)   # noqa: SLF001
    # #43：`_apply_service_enabled` 现在只调 `_sync_tts_gate`（唯一门控写点）——
    # MagicMock 上的自动属性不可 await，必须一并接上真实实现。
    agent._sync_tts_gate = mock.AsyncMock(side_effect=_sync)            # noqa: SLF001


class TestApplyServiceState:
    """_apply_service_state 启动步骤：读 ServiceState → 应用开关 → LLM 应用。"""

    @pytest.mark.asyncio
    async def test_explicit_values_win_and_switch_llm(self, fake_agent):
        """service_state 显式值 → 有效开关取显式值；llm_model ≠ config → switch_llm。"""
        agent = _real_service_agent(stt_default=True, tts_default=True)
        agent.session.service_state = ServiceState(
            llm_model="deepseek/deepseek-v4-pro",
            stt_enabled=True,
            tts_enabled=False,
        )
        agent._config.stt.enabled = True
        agent._config.tts.enabled = True
        agent._config.llm.model = "deepseek/deepseek-v4-flash"
        agent.switch_llm = mock.AsyncMock(return_value={"ok": True})

        _wire_service_enabled(agent)
        await _LifecycleServices._apply_service_state(agent)

        # 显式值压过 config（config 两个都设成 True）
        assert agent.stt_enabled is True
        assert agent.tts_enabled is False
        agent.switch_llm.assert_awaited_once_with("deepseek/deepseek-v4-pro")

    @pytest.mark.asyncio
    async def test_effective_enabled_falls_back_to_config(self, fake_agent):
        """三态回退：enabled=None → 有效值 = config.<svc>.enabled。"""
        agent = _real_service_agent()
        agent.session.service_state = ServiceState()  # enabled 全 None
        agent._config.stt.enabled = True
        agent._config.tts.enabled = False
        agent._config.llm.model = "deepseek/deepseek-v4-flash"
        agent.switch_llm = mock.AsyncMock(return_value={"ok": True})

        _wire_service_enabled(agent)
        await _LifecycleServices._apply_service_state(agent)

        assert agent.stt_enabled is True       # 派生：None → config.stt.enabled
        assert agent.tts_enabled is False      # 派生：None → config.tts.enabled
        agent.switch_llm.assert_not_awaited()  # llm_model 为空 → 不应用

    @pytest.mark.asyncio
    async def test_no_switch_llm_when_same_as_config(self, fake_agent):
        """llm_model == config.llm.model → 不触发 switch_llm。"""
        agent = _real_service_agent()
        agent.session.service_state = ServiceState(llm_model="deepseek/deepseek-v4-flash")
        agent._config.stt.enabled = False
        agent._config.tts.enabled = False
        agent._config.llm.model = "deepseek/deepseek-v4-flash"
        agent.switch_llm = mock.AsyncMock(return_value={"ok": True})

        _wire_service_enabled(agent)
        await _LifecycleServices._apply_service_state(agent)

        agent.switch_llm.assert_not_awaited()


class TestStartTTSGate:
    """_start_tts 建门（有效 enabled 三态）+ startup_tts_model。"""

    def _setup_tts_agent(self, agent, service_state, cfg_enabled):
        agent._session_manager.active.service_state = service_state
        agent._config.tts.enabled = cfg_enabled
        agent._config.tts.model = "minimax/speech-2.8-hd"
        agent._config.tts.rate_limit_rpm = 10
        # #43：建门直接读派生的 `agent.tts_enabled`（= service_state 三态 → config 回退），
        # 不再有 Agent 内存槽位需要预置。
        agent._tts = None  # 对齐 Agent.__init__ 初始值
        agent._cognitive.configure = mock.MagicMock()
        agent._cognitive._tts_mgr.set_enabled = mock.AsyncMock()

    @pytest.mark.asyncio
    async def test_builds_with_startup_model_when_hotplug_open(self, fake_agent):
        """config=false + 显式 true → 建（热插拔'开'跨重启存活），用 startup_tts_model。"""
        agent = _real_service_agent()
        self._setup_tts_agent(
            agent, ServiceState(tts_model="custom/mytts", tts_enabled=True), cfg_enabled=False)
        backend = mock.MagicMock()
        backend.test = mock.AsyncMock(return_value={"ok": True})
        with mock.patch("Module.tts.TTSFactory.create", return_value=backend) as mc, \
             mock.patch("core.providers.custom_providers.resolve_model_params",
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
             mock.patch("core.providers.custom_providers.resolve_model_params",
                        return_value={"base_url": "", "runtime": "desktop"}):
            await _LifecycleServices._start_tts(agent)
        # 不建后端、不测连通性、不崩
        mc.assert_not_called()
        assert agent._tts is None
        agent._cognitive.configure.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_off_when_config_off_and_none(self, fake_agent):
        """config=false + None → 不建（YAML 硬关保留）。"""
        agent = _real_service_agent()
        self._setup_tts_agent(agent, ServiceState(), cfg_enabled=False)
        with mock.patch("Module.tts.TTSFactory.create") as mc:
            await _LifecycleServices._start_tts(agent)
        mc.assert_not_called()
        assert agent._tts is None

    @pytest.mark.asyncio
    async def test_gate_off_when_config_on_and_explicit_false(self, fake_agent):
        """config=true + 显式 false → 不建（尊重用户关闭，免浪费连接测试）。"""
        agent = _real_service_agent()
        self._setup_tts_agent(agent, ServiceState(tts_enabled=False), cfg_enabled=True)
        with mock.patch("Module.tts.TTSFactory.create") as mc:
            await _LifecycleServices._start_tts(agent)
        mc.assert_not_called()
        assert agent._tts is None

    @pytest.mark.asyncio
    async def test_gate_on_when_config_on_and_none(self, fake_agent):
        """config=true + None → 建（现状行为），用 config 默认模型。"""
        agent = _real_service_agent()
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
        agent = _real_service_agent()
        self._setup_tts_agent(agent, ServiceState(), cfg_enabled=True)
        with mock.patch("Module.tts.TTSFactory.create", side_effect=ValueError(
                "MiniMax TTS 需要 api_key，请设置 MINIMAX_API_KEY 环境变量或显式传入")) as mc, \
             mock.patch("core.providers.custom_providers.resolve_model_params",
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
             mock.patch("core.providers.custom_providers.resolve_model_params",
                        return_value={"base_url": "http://x"}):
            with pytest.raises(ValueError):
                await _LifecycleServices._start_tts(agent)
        mc.assert_called_once()



def _bind_switch_session(agent) -> None:
    """把真实的 `Agent.switch_session` 绑到 mock agent 上。

    `agent` 是 `MagicMock`——`agent.switch_session(...)` 只会调用一个自动生成的
    MagicMock 属性，**真实方法根本不执行**。接线后，'切会话是否重放服务开关'
    这件事才真的被测到。
    """
    from core.agent.agent import Agent

    async def _switch(sid):
        return await Agent.switch_session(agent, sid)

    agent.switch_session = _switch


class TestServiceFlagsFollowSessionOnSwitch:
    """S2-C3：切会话后运行时槽位必须跟随**该会话**的 `service_state`。

    `ServiceState` 是 `SessionMemory` 的字段（每会话持久化），而它原先**只在启动步骤**
    被应用一次（`lifecycle.py` 的 `_STARTUP_STEPS`）。切到另一个会话后内存槽位保持
    上一个会话的值，而 `session.service_state` 已经是新会话的值 → 前端读 capabilities
    （取自内存槽位）与服务端持久化值不一致；重启后 `_apply_service_state` 改用新会话的
    值，用户视角就是"开关回退了"。

    实测（修复前）：切到 `stt_enabled=True` 的会话后 `agent.stt_enabled` 仍是 False。
    """

    @pytest.mark.asyncio
    async def test_apply_service_enabled_syncs_gate_from_session(self, fake_agent):
        """#43：有效开关是**派生**的（会话三态优先，None 回退 config）。

        修复前后对比：原先写两份（Agent 内存槽位 + TTS 管理器标志），本用例断言槽位；
        现在权威只有会话 `service_state` 一份，`agent.tts_enabled` 现算，断言的是
        派生值与门控投影。
        """
        agent = _real_service_agent()
        agent.session.service_state = ServiceState(stt_enabled=True, tts_enabled=True)
        agent._config.stt.enabled = False
        agent._config.tts.enabled = False

        await _LifecycleServices._apply_service_enabled(agent)

        assert agent.stt_enabled is True                       # 显式值压过 config
        assert agent.tts_enabled is True
        # TTS 管理器标志是 loop 的实际门控——派生投影必须跟着走，否则"开关显示开着
        # 但不会说话"
        agent._cognitive._tts_mgr.set_enabled.assert_awaited_once_with(True)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_apply_service_enabled_does_not_switch_llm(self, fake_agent):
        """**有意取舍**：会话激活路径不做模型热切换（要建/拆后端，延迟与失败面大）。

        这条断言是**记录设计决定**而不只是防回归——下次审计看到"切会话不切模型"
        时，应当先看到这里写明的理由。
        """
        agent = _real_service_agent()
        agent.session.service_state = ServiceState(
            llm_model="deepseek/deepseek-v4-pro", stt_enabled=True)
        agent._config.llm.model = "deepseek/deepseek-v4-flash"
        agent.switch_llm = mock.AsyncMock(return_value={"ok": True})

        await _LifecycleServices._apply_service_enabled(agent)

        agent.switch_llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_switch_session_applies_new_session_state(self, fake_agent):
        """接线：`switch_session` 必须真的调用它（否则等于没修）。"""
        agent = fake_agent
        target = mock.MagicMock()
        target.character_binding = {"instance_id": "inst_1"}
        agent._session_manager.ensure_loaded = mock.AsyncMock(return_value=target)
        agent._session_manager.switch = mock.AsyncMock(return_value={"ok": True})
        agent.ensure_character_runtime = mock.AsyncMock()
        agent._apply_service_enabled = mock.AsyncMock()
        # **接上真实方法**：`agent` 是 MagicMock，直接 `agent.switch_session(...)` 只会
        # 调用一个自动属性，等于什么都没测（本用例第一版就是这样假绿的）。
        _bind_switch_session(agent)

        await agent.switch_session(SessionId("conv_x"))

        agent._apply_service_enabled.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_end_to_end_two_sessions_disagree(self, fake_agent):
        """端到端：两个会话的开关不同 → 切换后派生值必须跟着变（往返都一致）。"""
        agent = _real_service_agent()
        agent._config.stt.enabled = False

        states = {"conv_a": ServiceState(stt_enabled=True),
                  "conv_b": ServiceState(stt_enabled=False)}

        def _ensure(sid):
            target = mock.MagicMock()
            target.character_binding = {"instance_id": "inst_1"}
            target.service_state = states[str(sid)]
            return target

        agent._session_manager.ensure_loaded = mock.AsyncMock(side_effect=_ensure)
        agent.ensure_character_runtime = mock.AsyncMock()
        _wire_service_enabled(agent)          # 接真实的三态同步逻辑

        async def _switch(sid):
            # 活动会话指针 + 活动会话对象一起换（真实 SessionManager 的行为）。
            # `agent.session` 是只读 property → 必须换底层 `_session_manager.active`。
            active = mock.MagicMock()
            active.service_state = states[str(sid)]
            agent._session_manager.active = active
            return {"ok": True}

        agent._session_manager.switch = mock.AsyncMock(side_effect=_switch)
        _bind_switch_session(agent)

        await agent.switch_session(SessionId("conv_a"))
        assert agent.stt_enabled is True, "切到显式开启的会话 → 有效开关应为 True"
        await agent.switch_session(SessionId("conv_b"))
        assert agent.stt_enabled is False, "切回显式关闭的会话 → 有效开关应为 False"
