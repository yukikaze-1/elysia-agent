# 测试文件：core/agent/tests/test_desktop_bridge_integration.py
"""批次 4 Task 6：Agent / ToolAdapter / 生命周期 desktop_bridge 集成测试。

覆盖：
  - Agent 构造后 desktop_bridge 只读属性存在、is_available() is False（无连接）
  - 构造绑定：ToolAdapter._desktop_bridge 与 agent.desktop_bridge 同一实例
  - definitions() D-j 过滤：bridge 未连接时 desktop 工具不出现在 LLM schema，
    连接后出现（filter_runtime 双态）
  - _start_tool_context：bridge.bind_deliver + start(extra_services 含 desktop_bridge)
  - deliver 回调：按 target_connection 精确定位 electron 通道 → send_json(frame)；
    无精确目标立即抛错，由 bridge 收敛为 DESKTOP_UNAVAILABLE
  - _start_persistence：await bridge.connect_db()
  - _stop_persistence：await bridge.save_all()
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.agent import Agent
from core.agent._lifecycle_persistence import _LifecyclePersistence
from core.agent._lifecycle_tools import _LifecycleTools
from core.config import AppConfig
from Module.tool.protocol import to_llm_name

pytestmark = pytest.mark.service

DESKTOP_TOOLS = {
    "desktop.file.select", "desktop.file.read",
    "desktop.file.write", "desktop.screen.capture",
}
# definitions() 暴露脱点名（OpenAI 禁点号，desktop.* → desktop_*）
LLM_DESKTOP_TOOLS = {to_llm_name(n) for n in DESKTOP_TOOLS}


@pytest.fixture
def agent():
    return Agent(config=AppConfig())


class TestDesktopBridgeProperty:
    """Agent 构造：desktop_bridge 属性 + 构造绑定。"""

    def test_desktop_bridge_exists_and_unavailable(self, agent):
        """构造后 desktop_bridge 存在，无连接时 is_available() is False。"""
        assert agent.desktop_bridge is not None
        assert agent.desktop_bridge.is_available() is False

    def test_desktop_bridge_is_tool_adapter_bound(self, agent):
        """构造绑定：ToolAdapter._desktop_bridge 与 agent.desktop_bridge 同一实例。"""
        assert agent.tool_adapter._desktop_bridge is agent.desktop_bridge  # noqa: SLF001

    def test_desktop_bridge_is_readonly(self, agent):
        """只读属性——外部赋值抛 AttributeError（注入走 agent._desktop_bridge）。"""
        with pytest.raises(AttributeError):
            agent.desktop_bridge = None


class TestDefinitionsFilter:
    """ToolAdapter.definitions() 按 bridge.is_available() 过滤 desktop 工具（D-j）。"""

    @pytest.mark.asyncio
    async def test_desktop_tools_hidden_when_not_connected(self, agent):
        """bridge 未连接：desktop 工具不出现在 definitions()，backend 工具保留。"""
        await agent.tool_adapter.start()
        try:
            defs = agent.tool_adapter.definitions()
            names = {d["function"]["name"] for d in defs}
            assert not (names & LLM_DESKTOP_TOOLS)
            assert "read_file" in names  # backend 工具不受影响
        finally:
            await agent.tool_adapter.shutdown()

    @pytest.mark.asyncio
    async def test_desktop_tools_visible_when_connected(self, agent):
        """bridge 连接后：desktop 工具出现在 definitions()（is_available 双态翻转）。"""
        await agent.desktop_bridge.on_client_connect("conn_x")
        await agent.tool_adapter.start()
        try:
            assert agent.desktop_bridge.is_available() is True
            defs = agent.tool_adapter.definitions()
            names = {d["function"]["name"] for d in defs}
            assert LLM_DESKTOP_TOOLS.issubset(names)
        finally:
            await agent.tool_adapter.shutdown()


class TestStartToolContext:
    """_start_tool_context：deliver 绑定 + execute 运行时注入双轨。"""

    @pytest.mark.asyncio
    async def test_binds_deliver_and_injects_bridge(self):
        """bind_deliver 被调 + start(extra_services) 含 desktop_bridge。"""
        agent = MagicMock()
        adapter = MagicMock()
        adapter.start = AsyncMock()
        agent._tool_adapter = adapter
        agent._long_term = object()
        agent._scheduler = MagicMock()
        agent._scheduler.set_tool_adapter = MagicMock()
        agent._config = object()
        session = MagicMock()
        session.messages = []
        agent.session = session
        agent._bus = object()
        agent._started_at = 123.0
        agent._token_tracker = object()
        agent.status = MagicMock()
        agent.status.current_name.return_value = "idle"
        agent._cognitive = MagicMock()
        agent._cognitive.configure = MagicMock()
        bridge = MagicMock()
        bridge.bind_deliver = MagicMock()
        agent._desktop_bridge = bridge

        await _LifecycleTools._start_tool_context(agent)

        bridge.bind_deliver.assert_called_once()
        services = adapter.start.call_args.kwargs["extra_services"]
        assert services["desktop_bridge"] is bridge
        agent._scheduler.set_tool_adapter.assert_called_once_with(adapter)

    @pytest.mark.asyncio
    async def test_bind_deliver_registers_async_callback(self):
        """bind_deliver 注入的回调是 awaitable——bridge 以 await deliver(frame) 消费。"""
        from core.adapter.tool_adapter import ToolAdapter

        adapter = ToolAdapter(plugins=[], desktop_bridge=None)
        agent = MagicMock()
        agent._tool_adapter = adapter
        agent._long_term = object()
        agent._scheduler = MagicMock()
        agent._scheduler.set_tool_adapter = MagicMock()
        agent._config = object()
        session = MagicMock()
        session.messages = []
        agent.session = session
        agent._bus = object()
        agent._started_at = 123.0
        agent._token_tracker = object()
        agent.status = MagicMock()
        agent.status.current_name.return_value = "idle"
        agent._cognitive = MagicMock()
        agent._cognitive.configure = MagicMock()
        agent._output = MagicMock()
        agent._output.channels = []
        agent._desktop_bridge = None

        await _LifecycleTools._start_tool_context(agent)
        # 无 bridge 时 start 照常（extra_services 不含 desktop_bridge），不抛
        services = agent._tool_adapter.tool_context.services
        assert "desktop_bridge" not in services


class TestDeliverCallback:
    """deliver 回调：定向 electron 通道 + 无目标兜底。"""

    @pytest.mark.asyncio
    async def test_delivers_to_electron_channel(self):
        """精确 connection_id 对应的 electron 通道收到 tool.request 帧。"""
        agent = MagicMock()
        target = MagicMock()
        target.client_platform = "electron"
        target.connection_id = "conn_target"
        target.send_json = AsyncMock()
        other = MagicMock()
        other.client_platform = "web"  # 非 electron 不收
        other.connection_id = "conn_target"
        agent._output.channels = [other, target]

        # B-6-05：frame 含 sourceCallId/requestId（D6-07 贯通），deliver 原样转发不改写
        frame = {"type": "tool.request", "callId": "call_x",
                 "sourceCallId": "tc_desktop_1", "requestId": None}
        await _LifecycleTools._deliver_tool_request(agent, "conn_target", frame)

        target.send_json.assert_awaited_once_with(frame, raise_on_error=True)
        other.send_json.assert_not_called()
        assert target.send_json.await_args.args[0]["sourceCallId"] == "tc_desktop_1"
        assert target.send_json.await_args.args[0]["requestId"] is None

    @pytest.mark.asyncio
    async def test_reconnect_overlap_delivers_only_to_latest_target(self):
        """新旧 Electron 并存时按目标 ID 投递，不再误取列表中的旧连接。"""
        agent = MagicMock()
        old = MagicMock()
        old.client_platform = "electron"
        old.connection_id = "conn_old"
        old.send_json = AsyncMock()
        new = MagicMock()
        new.client_platform = "electron"
        new.connection_id = "conn_new"
        new.send_json = AsyncMock()
        agent._output.channels = [old, new]

        frame = {"type": "tool.request", "callId": "call_reconnect"}
        await _LifecycleTools._deliver_tool_request(agent, "conn_new", frame)

        old.send_json.assert_not_awaited()
        new.send_json.assert_awaited_once_with(frame, raise_on_error=True)

    @pytest.mark.asyncio
    async def test_getattr_compatible_before_task7(self):
        """client_platform 属性尚未落地（Task 7）——getattr 缺省 "" 不抛。"""
        agent = MagicMock()
        ch = MagicMock()
        del ch.client_platform  # 模拟无该属性的旧通道
        ch.send_json = AsyncMock()
        agent._output.channels = [ch]

        with pytest.raises(RuntimeError, match="目标连接不可用"):
            await _LifecycleTools._deliver_tool_request(
                agent, "conn_missing", {"type": "tool.request"})

        ch.send_json.assert_not_awaited()  # 无 electron 通道 → 不投递（兜底）

    @pytest.mark.asyncio
    async def test_no_electron_channel_raises_for_bridge_fast_failure(self):
        """无精确目标时抛错，让 bridge 立即返回 UNAVAILABLE 而非挂到超时。"""
        agent = MagicMock()
        agent._output.channels = []
        with pytest.raises(RuntimeError, match="目标连接不可用"):
            await _LifecycleTools._deliver_tool_request(
                agent, "conn_missing", {"type": "tool.request"})

    @pytest.mark.asyncio
    async def test_send_failure_surfaces_instead_of_being_swallowed(self):
        """S2-H6：投递失败必须**真的抛出来**——这是 bridge 快速回退的前提。

        用**真实** `WebSocketChannel`（不是 MagicMock）——因为缺陷恰恰在于
        `send_json` 生产实现的吞异常语义：契约要求 `_deliver_tool_request` 在
        投递失败时抛，而它唯一能用的发送调用不抛，于是 bridge 只能等满 timeout
        （把"桌面不可用"记成"桌面超时"，其快速回退分支成为死代码）。

        反证：把 `_deliver_tool_request` 改回 `send_json(frame)`（不带
        `raise_on_error=True`）→ 本用例必须失败（不抛，异常被吞成 warning）。
        """
        from Module.output.websocket import WebSocketChannel

        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=ConnectionResetError("link down"))
        ch = WebSocketChannel(ws, protocol_version=2, client_platform="electron")
        ch.connection_id = "conn_target"
        agent = MagicMock()
        agent._output.channels = [ch]

        with pytest.raises(ConnectionResetError, match="link down"):
            await _LifecycleTools._deliver_tool_request(
                agent, "conn_target", {"type": "tool.request"})

    @pytest.mark.asyncio
    async def test_successful_delivery_does_not_raise(self):
        """反空转：通道正常时必须照常投出去（不能为了 fail-fast 把成功路径也搞坏）。"""
        from Module.output.websocket import WebSocketChannel

        ws = MagicMock()
        ws.send_json = AsyncMock()
        ch = WebSocketChannel(ws, protocol_version=2, client_platform="electron")
        ch.connection_id = "conn_target"
        agent = MagicMock()
        agent._output.channels = [ch]

        frame = {"type": "tool.request", "callId": "call_x"}
        await _LifecycleTools._deliver_tool_request(agent, "conn_target", frame)

        ws.send_json.assert_awaited_once_with(frame)


class TestStartPersistenceHook:
    """_start_persistence 挂点：await bridge.connect_db()。"""

    @pytest.mark.asyncio
    async def test_start_persistence_connects_desktop_bridge(self):
        """mongo 可用：bridge.connect_db 被 await 一次。"""
        agent = MagicMock()
        sm = MagicMock()
        sm.connect_db = AsyncMock(return_value=True)
        sm.load_active = AsyncMock()
        sm.active.pending_file = "/nonexistent/session.jsonl"
        agent._session_manager = sm
        agent._long_term.connect = AsyncMock(return_value=True)
        agent._message_repo.connect = AsyncMock(return_value=True)
        agent._token_repo.connect = AsyncMock(return_value=True)
        agent._version_repo.connect = AsyncMock(return_value=True)
        agent._character_engine.state = MagicMock()
        agent._character_engine.state.last_awareness_time = None
        agent._character_engine.restore_evolution_state = MagicMock()
        bridge = MagicMock()
        bridge.connect_db = AsyncMock(return_value=True)
        agent._desktop_bridge = bridge

        await _LifecyclePersistence._start_persistence(agent)

        bridge.connect_db.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_persistence_bridge_offline_no_raise(self):
        """Mongo 离线：bridge.connect_db 返回 False，不抛、流程继续。"""
        agent = MagicMock()
        sm = MagicMock()
        sm.connect_db = AsyncMock(return_value=False)
        sm.load_active = AsyncMock()
        agent._session_manager = sm
        agent._long_term.connect = AsyncMock(return_value=False)
        agent._message_repo.connect = AsyncMock(return_value=True)
        agent._token_repo.connect = AsyncMock(return_value=True)
        agent._version_repo.connect = AsyncMock(return_value=True)
        agent._character_engine = MagicMock()
        bridge = MagicMock()
        bridge.connect_db = AsyncMock(return_value=False)
        agent._desktop_bridge = bridge

        mongo_ok, milvus_ok = await _LifecyclePersistence._start_persistence(agent)

        assert mongo_ok is False
        bridge.connect_db.assert_awaited_once()


class TestStopPersistenceHook:
    """_stop_persistence 挂点：await bridge.save_all()。"""

    @pytest.mark.asyncio
    async def test_stop_persistence_saves_desktop_bridge(self):
        """关闭时 bridge.save_all 被 await（no-op 预留，repo 即时 upsert）。"""
        agent = MagicMock()
        agent._session_manager.save_all = AsyncMock()
        agent._long_term.close = AsyncMock()
        agent._session_repo.close = AsyncMock()
        agent._message_repo.close = AsyncMock()
        agent._token_repo.close = AsyncMock()
        agent._version_repo.close = AsyncMock()
        # Task 5：_stop_persistence 现会关闭幂等仓库——MagicMock 的 close 非 awaitable，
        # 须显式提供 AsyncMock（否则 await MagicMock() 抛 TypeError）
        agent.idempotency_repo = MagicMock()
        agent.idempotency_repo.close = AsyncMock()
        bridge = MagicMock()
        bridge.save_all = AsyncMock()
        agent._desktop_bridge = bridge

        await _LifecyclePersistence._stop_persistence(agent)

        bridge.save_all.assert_awaited_once()
        agent.idempotency_repo.close.assert_awaited_once()


class TestSyncDesktopAvailable:
    """sync_desktop_available 同步入口：bridge 可用性写入 ToolManager。"""

    def test_sync_writes_manager_available(self, agent):
        """连接后同步 → tool_adapter._manager 的 desktop 可用性被更新。"""
        # 预置 manager 状态（断言 _desktop_available 翻转）
        agent.tool_adapter._manager.set_desktop_available(False)
        assert agent.tool_adapter._manager._desktop_available is False  # noqa: SLF001

        agent.sync_desktop_available()

        assert agent.tool_adapter._manager._desktop_available is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_sync_flips_with_connection(self, agent):
        """连接后 sync_desktop_available → manager._desktop_available True。"""
        await agent.desktop_bridge.on_client_connect("conn_x")
        agent.sync_desktop_available()
        assert agent.tool_adapter._manager._desktop_available is True  # noqa: SLF001
