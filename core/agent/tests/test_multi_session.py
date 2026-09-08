"""批次 3 Task 7：Agent 多会话集成测试（marker=service/unit）。

覆盖新公开 API：
  - agent.session → session_manager.active（同实例）
  - active_session_id / session_manager property
  - resolve_session(conversation_id, protocol_version)
  - switch_session(sid) — active_session_id 更新 + engine attach
  - pending_messages（只读视图）
  - process_message(conversation_id=…) → _PendingMessage / UserInputData 贯通
  - _start_persistence 走 session_manager.connect_db + load_active
"""

import asyncio
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.agent._types import _PendingMessage
from core.agent.agent import Agent
from core.agent._lifecycle_persistence import _LifecyclePersistence
from core.config import AppConfig
from core.memory.session_manager import SessionBusyError
from core.schema.event import UserInputData, InterruptData, StateChangeData
from core.schema.types import RequestId

pytestmark = pytest.mark.service


@pytest.fixture
def agent():
    return Agent(config=AppConfig())


class TestSessionProperty:
    def test_session_is_session_manager_active(self, agent):
        """session property → session_manager.active 同实例。"""
        assert agent.session is agent.session_manager.active

    def test_session_manager_exposed(self, agent):
        """session_manager 公开只读访问。"""
        assert agent.session_manager is not None
        assert agent.session_manager.active_id == "default"

    def test_active_session_id_default(self, agent):
        """默认活动会话 ID = default。"""
        assert agent.active_session_id == "default"


class TestSwitchSession:
    @pytest.mark.asyncio
    async def test_switch_updates_active(self, agent):
        """switch_session 后 active_session_id 更新。M3：不再 attach engine（engine per-instance）。"""
        conv = agent.session_manager.get("conv_x")  # 预建缓存，跳过 DB load
        agent.session_manager._loaded.add("conv_x")  # noqa: SLF001 — 测试确定性：避免 JSONL fallback

        result = await agent.switch_session("conv_x")

        assert result is conv
        assert agent.active_session_id == "conv_x"
        assert agent.session is conv

    @pytest.mark.asyncio
    async def test_switch_keeps_engine_state_on_default_instance(self, agent):
        """真实引擎：switch 不重绑 engine.state（M3：engine per-instance 恒定）。

        agent.character_engine 经 registry 解析默认 instance——切换会话后仍返回同一
        (default Elysia) engine；state 视图不随 session 切换改绑。
        """
        conv = agent.session_manager.get("conv_y")
        agent.session_manager._loaded.add("conv_y")  # noqa: SLF001
        engine_before = agent.character_engine
        await agent.switch_session("conv_y")
        # M3：engine 恒定（default instance），state 不随 session 切换改绑
        assert agent.character_engine is engine_before


class TestResolveSession:
    @pytest.mark.asyncio
    async def test_resolve_v1_always_default(self, agent):
        """resolve_session(None, 1) 恒 default 会话。"""
        result = await agent.resolve_session(None, 1)
        assert result is agent.session_manager.active
        assert result.session_id == "default"

    @pytest.mark.asyncio
    async def test_resolve_v2_none_uses_active(self, agent):
        """resolve_session(None, 2) → 当前活动会话。"""
        result = await agent.resolve_session(None, 2)
        assert result is agent.session_manager.active

    @pytest.mark.asyncio
    async def test_resolve_v2_conversation_id(self, agent):
        """resolve_session("conv_z", 2) → 指定会话（已知会话；9abc P2-1 后未知抛错）。"""
        agent.session_manager.get("conv_z")  # 预建缓存——严格 resolver 只解析已知会话
        result = await agent.resolve_session("conv_z", 2)
        assert result.session_id == "conv_z"


class TestPendingMessages:
    def test_pending_messages_returns_queued(self, agent):
        """pending_messages 返回未出队列表。"""
        agent._pending_messages.append(
            _PendingMessage(text="hi", user_name="u", request_id=RequestId("r")))
        assert len(agent.pending_messages) == 1
        assert agent.pending_messages[0].text == "hi"

    def test_pending_messages_is_readonly_snapshot(self, agent):
        """返回的是快照——外部 append 不污染内部队列。"""
        agent._pending_messages.append(
            _PendingMessage(text="hi", user_name="u", request_id=RequestId("r")))
        view = agent.pending_messages
        view.append(_PendingMessage(text="x", user_name="u", request_id=RequestId("x")))
        assert len(agent._pending_messages) == 1


class TestProcessMessageConversationId:
    @pytest.mark.asyncio
    async def test_posts_conversation_id(self):
        """process_message(conversation_id=…) → 发布的 UserInputData 携带。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        agent._bus.post = MagicMock()
        try:
            await agent.process_message("hello", conversation_id="conv_x")
            posted = agent._bus.post.call_args.args[0]
            assert isinstance(posted, UserInputData)
            assert posted.conversation_id == "conv_x"
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_busy_queue_threads_conversation_id(self):
        """忙时排队：_PendingMessage 携带 conversation_id。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        try:
            await agent.phases.enter_thinking()
            await agent.process_message("hello", conversation_id="conv_x")
            assert len(agent.pending_messages) == 1
            assert agent.pending_messages[0].conversation_id == "conv_x"
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_dequeue_preserves_conversation_and_protocol_version(self):
        """review 修复（F3/链路#1）：busy 排队 v2 消息带 conversationId → 出队后
        UserInputData 透传 conversation_id + protocol_version（不降级为 v1 恒 default）。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()  # 未 start——纯内存调用，不连 DB
        agent._bus.post = MagicMock()
        agent._pending_messages.append(_PendingMessage(
            text="hi", user_name="u", request_id="req_1",
            message_id="msg_1", conversation_id="conv_x", protocol_version=2))
        await agent._handle_state_change(StateChangeData(state="idle", request_id="req_1"))
        posted = agent._bus.post.call_args.args[0]
        assert isinstance(posted, UserInputData)
        assert posted.conversation_id == "conv_x"
        assert posted.protocol_version == 2

    @pytest.mark.asyncio
    async def test_v2_eventbus_work_blocks_delete_and_releases_after_cognitive_terminal(self):
        """真实 Agent：EventBus 窗口也持有会话引用，CognitiveLoop 终态后成对释放。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        summary = await agent.session_manager.create(title="eventbus")
        agent._bus.post = MagicMock()
        agent._cognitive.process = AsyncMock(return_value=None)
        try:
            accepted = await agent.process_message(
                "hello", protocol_version=2,
                conversation_id=str(summary.session_id))
            assert accepted is True
            posted = agent._bus.post.call_args.args[0]
            assert posted.conversation_id == summary.session_id
            assert posted.session_work_reserved is True
            assert agent.session_manager.work_count(summary.session_id) == 1
            with pytest.raises(SessionBusyError):
                await agent.session_manager.delete(summary.session_id)

            agent._handle_user_input(posted)
            await agent._cognitive._current_task
            assert agent.session_manager.work_count(summary.session_id) == 0
            await agent.session_manager.delete(summary.session_id)
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_prestart_cancel_releases_lease_via_done_callback(self):
        """C1/P1-06：pre-start 取消的 cognitive task 也释放 lease（done callback 兜底）。

        修复前 pre-start cancel 不进 coroutine finally → lease 永久泄漏，
        该会话后续删除持续 409 busy。
        """
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        agent._bus.post = MagicMock()
        agent._cognitive.process = AsyncMock(return_value=None)
        try:
            s = await agent.session_manager.create(title="lease")
            sid = str(s.session_id)
            await agent.process_message("hello", protocol_version=2,
                                        conversation_id=sid)
            posted = agent._bus.post.call_args.args[0]
            assert agent.session_manager.work_count(sid) == 1

            agent._handle_user_input(posted)
            task = agent._cognitive._current_task
            task.cancel()  # pre-start 取消（coroutine 尚未进入）
            await asyncio.sleep(0.05)  # done callback 触发
            assert agent.session_manager.work_count(sid) == 0  # lease 已释放
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_same_tick_second_message_queues_not_cancels(self):
        """C1/P1-06：同 tick 第二条 v2 消息不取消 pre-start 第一条，入队等待。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        agent._bus.post = MagicMock()
        agent._cognitive.process = AsyncMock(return_value=None)
        try:
            s = await agent.session_manager.create(title="burst")
            sid = str(s.session_id)
            await agent.process_message("m1", protocol_version=2,
                                        conversation_id=sid)
            await agent.process_message("m2", protocol_version=2,
                                        conversation_id=sid)
            posts = agent._bus.post.call_args_list
            assert len(posts) == 2  # 同 tick 都见 IDLE，两条都 accepted

            agent._handle_user_input(posts[0].args[0])  # m1 → task（pre-start）
            agent._handle_user_input(posts[1].args[0])  # m2 → 应排队
            assert len(agent._pending_messages) == 1     # 未取消 m1，m2 入队
            assert agent.session_manager.work_count(sid) == 2  # 两 lease 都在

            await agent._cognitive._current_task  # m1 执行完
            await asyncio.sleep(0.05)
            assert agent._cognitive.process.await_count == 1  # m1 未被取消
            assert agent.session_manager.work_count(sid) == 1  # m2 lease 仍在
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_missing_conversation_id_is_frozen_to_active_before_accept(self):
        """ACK 时省略 ID 的消息不会在 EventBus 消费前随 active 切换改投 default。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        summary = await agent.session_manager.create(title="active")
        await agent.switch_session(summary.session_id)
        agent._bus.post = MagicMock()
        try:
            await agent.process_message("hello", protocol_version=2, conversation_id=None)
            posted = agent._bus.post.call_args.args[0]
            assert posted.conversation_id == summary.session_id
            agent.session_manager.release_work(summary.session_id)
        finally:
            await agent.stop()


class TestInterruptSource:
    def test_handle_interrupt_threads_source(self, fake_agent):
        """_handle_interrupt 把 data.source 传给 loop 的 cancel 路径。"""
        from core.agent.agent import Agent

        fake_agent._cognitive = MagicMock()
        fake_agent._cognitive.cancel_current = MagicMock(return_value=False)
        fake_agent._bus.drain = MagicMock(return_value=[])
        fake_agent._pending_messages = []
        Agent._handle_interrupt(fake_agent, InterruptData(source="vad"))
        fake_agent._cognitive.cancel_current.assert_called_once_with(source="vad")


class TestStartPersistence:
    @pytest.mark.asyncio
    async def test_load_active_when_mongo_ok(self):
        """mongo 可用：_start_persistence 走 connect_db + load_active。"""
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
        agent._character_engine._reflector._restore_from_state = MagicMock()

        mongo_ok, milvus_ok = await _LifecyclePersistence._start_persistence(agent)

        assert mongo_ok is True
        assert milvus_ok is True
        sm.connect_db.assert_awaited_once()
        sm.load_active.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_load_active_even_when_mongo_down(self):
        """mongo 不可用：仍调用 load_active（确保默认会话进 _loaded，shutdown save_all 落 JSONL）。"""
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

        mongo_ok, milvus_ok = await _LifecyclePersistence._start_persistence(agent)

        assert mongo_ok is False
        sm.connect_db.assert_awaited_once()
        sm.load_active.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_persistence_connects_generation_registry_and_marks_stale(self):
        """review 修复（兼容#2）：_start_persistence 对 generation registry connect，
        connect 成功后调 mark_stale_interrupted（cutoff=启动时刻-5min）。"""
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
        agent._character_engine._reflector._restore_from_state = MagicMock()
        gen_reg = MagicMock()
        gen_reg.connect = AsyncMock(return_value=True)
        gen_reg.mark_stale_interrupted = AsyncMock(return_value=0)
        agent._generation_registry = gen_reg

        await _LifecyclePersistence._start_persistence(agent)

        gen_reg.connect.assert_awaited_once()
        gen_reg.mark_stale_interrupted.assert_awaited_once()
        cutoff = gen_reg.mark_stale_interrupted.call_args.args[0]
        assert isinstance(cutoff, str) and "T" in cutoff  # ISO UTC cutoff

    @pytest.mark.asyncio
    async def test_start_persistence_skips_mark_stale_when_connect_fails(self):
        """connect 失败（Mongo 离线）：不调 mark_stale，不抛。"""
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
        gen_reg = MagicMock()
        gen_reg.connect = AsyncMock(return_value=False)
        gen_reg.mark_stale_interrupted = AsyncMock(return_value=0)
        agent._generation_registry = gen_reg

        await _LifecyclePersistence._start_persistence(agent)

        gen_reg.connect.assert_awaited_once()
        assert gen_reg.mark_stale_interrupted.await_count == 0


@pytest.mark.asyncio
class TestStartPersistenceTokenRepo:
    """根因修复：_start_persistence 对 token_repo connect（此前从未连接，health 恒离线）。"""

    def _agent(self, token_connect_result=True):
        agent = MagicMock()
        sm = MagicMock()
        sm.connect_db = AsyncMock(return_value=True)
        sm.load_active = AsyncMock()
        sm.active.pending_file = "/nonexistent/session.jsonl"
        agent._session_manager = sm
        agent._long_term.connect = AsyncMock(return_value=True)
        agent._message_repo.connect = AsyncMock(return_value=True)
        token_repo = MagicMock()
        token_repo.connect = AsyncMock(return_value=token_connect_result)
        agent._token_repo = token_repo
        agent._version_repo.connect = AsyncMock(return_value=True)
        agent._character_engine.state = MagicMock()
        agent._character_engine.state.last_awareness_time = None
        agent._character_engine._reflector._restore_from_state = MagicMock()
        # 无关挂点置空，避免 MagicMock 触发的噪音 warning
        agent._desktop_bridge = None
        agent._generation_registry = None
        return agent, token_repo

    async def test_start_persistence_connects_token_repo(self):
        """token_repo.connect 被调用（修复前从未 connect → _db 恒 None → 120s WARN）。"""
        agent, token_repo = self._agent()

        await _LifecyclePersistence._start_persistence(agent)

        token_repo.connect.assert_awaited_once()

    async def test_start_persistence_token_repo_offline_logs_warning(self, caplog):
        """connect 失败：记 "TokenRepository 离线" warning、不抛、流程继续。"""
        agent, token_repo = self._agent(token_connect_result=False)

        with caplog.at_level(logging.WARNING, logger="core.agent._lifecycle_persistence"):
            await _LifecyclePersistence._start_persistence(agent)

        token_repo.connect.assert_awaited_once()
        assert "TokenRepository 离线" in caplog.text


class TestCharacterPublicAccessors:
    def test_character_runtime_registry_exposed(self, agent):
        """character_runtime_registry 公开访问 self._runtime_registry。"""
        assert agent.character_runtime_registry is agent._runtime_registry  # noqa: SLF001

    def test_character_state_stream_true(self, agent):
        """character_state_stream 恒 true(本批,与 build_server_info 一致)。"""
        assert agent.character_state_stream is True

    def test_character_profiles_true(self, agent):
        """Stage H 完成 runtime/prompt/state 门禁后公开角色资料能力。"""
        assert agent.character_profiles is True


class TestRestartReentrancy:
    """C4/P1-07：同一 Agent 实例 restart 可重入——状态复位，不重复/不丢失。"""

    @pytest.mark.asyncio
    async def test_consecutive_restart_stable(self):
        """连续多次 start/stop：工具数量稳定、_shutting_down 复位、memory 不重复。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        try:
            tool_counts: list[int] = []
            for _ in range(3):
                await agent.start()
                tool_counts.append(len(agent.tool_adapter.definitions()))
                # C4/P1-07：restart 后 _shutting_down 必须复位（修复前恒 True，
                # after_response 永久跳过）
                assert agent._cognitive._shutting_down is False  # noqa: SLF001
                await agent.stop()
            assert len(set(tool_counts)) == 1, f"工具数量不稳定: {tool_counts}"
            assert tool_counts[0] > 0  # 非空（restart 后未丢工具）
        finally:
            await agent.stop()
