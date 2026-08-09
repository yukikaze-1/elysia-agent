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
    async def test_switch_updates_active_and_attaches_engine(self, agent):
        """switch_session 后 active_session_id 更新 + 绑定的 engine 收到 attach_session。"""
        conv = agent.session_manager.get("conv_x")  # 预建缓存，跳过 DB load
        agent.session_manager._loaded.add("conv_x")  # noqa: SLF001 — 测试确定性：避免 JSONL fallback
        mock_engine = MagicMock()
        agent.session_manager._character_engine = mock_engine  # noqa: SLF001 — mock engine 断言

        result = await agent.switch_session("conv_x")

        assert result is conv
        assert agent.active_session_id == "conv_x"
        assert agent.session is conv
        mock_engine.attach_session.assert_called_once_with(conv)

    @pytest.mark.asyncio
    async def test_switch_real_engine_rebinds_state(self, agent):
        """真实引擎：switch 后 engine.state 指向新会话的 character_state。"""
        conv = agent.session_manager.get("conv_y")
        agent.session_manager._loaded.add("conv_y")  # noqa: SLF001
        await agent.switch_session("conv_y")
        assert agent.character_engine.state is conv.character_state


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
