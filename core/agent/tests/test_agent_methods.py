"""Agent 实例方法 mock 测试。

注：Agent 是顶层类（继承 AgentLifecycle + AgentServer），
方法定义在 agent.py 中。
"""

from collections import deque
import unittest.mock as mock
from unittest.mock import MagicMock, AsyncMock
import pytest
from core.agent.agent import Agent
from core.adapter.llm_adapter import LLMAdapter
from core.schema.service_state import ServiceState

pytestmark = pytest.mark.service


class TestPurgeMessageFromSession:
    def test_purge_existing_message(self, fake_agent):
        """从 session deque 移除匹配 message_id 的消息"""
        msg1 = MagicMock(message_id="id-1")
        msg2 = MagicMock(message_id="id-2")
        msg3 = MagicMock(message_id="id-3")
        fake_agent._session.messages = deque([msg1, msg2, msg3], maxlen=20)

        purged = Agent.purge_message_from_session(fake_agent, "id-2")
        assert purged == 1
        assert len(fake_agent._session.messages) == 2
        remaining_ids = [m.message_id for m in fake_agent._session.messages]
        assert "id-2" not in remaining_ids
        assert "id-1" in remaining_ids
        assert "id-3" in remaining_ids

    def test_purge_nonexistent_message(self, fake_agent):
        msg1 = MagicMock(message_id="id-1")
        fake_agent._session.messages = deque([msg1], maxlen=20)

        purged = Agent.purge_message_from_session(fake_agent, "no-such-id")
        assert purged == 0
        assert len(fake_agent._session.messages) == 1

    def test_purge_all_messages(self, fake_agent):
        msg1 = MagicMock(message_id="id-1")
        fake_agent._session.messages = deque([msg1], maxlen=20)

        purged = Agent.purge_message_from_session(fake_agent, "id-1")
        assert purged == 1
        assert len(fake_agent._session.messages) == 0


class TestRegisterBackgroundTask:
    def test_register_stores_entry(self, fake_agent):
        fake_agent._background_tasks = {}

        Agent._register_background_task(
            fake_agent, "task-1",
            mailbox=MagicMock(),
            intent="test intent",
            dispatched_at="2026-01-01T00:00:00Z",
        )
        assert "task-1" in fake_agent._background_tasks
        entry = fake_agent._background_tasks["task-1"]
        assert entry.intent == "test intent"
        assert entry.dispatched_at == "2026-01-01T00:00:00Z"

    def test_register_multiple_tasks(self, fake_agent):
        fake_agent._background_tasks = {}

        Agent._register_background_task(
            fake_agent, "task-1", MagicMock(), "intent-1", "t1")
        Agent._register_background_task(
            fake_agent, "task-2", MagicMock(), "intent-2", "t2")
        assert len(fake_agent._background_tasks) == 2


class TestSwitchServiceState:
    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_switch_stt_writes_service_state_and_ok(self, fake_agent):
        """switch_stt 先建后关：成功后才挂新后端 + 写 ServiceState，config 不改写。"""
        agent = fake_agent
        agent._stt = None                            # 必须——auto-create 的 _stt truthy 会让"close 旧后端"走 await MagicMock.close() TypeError
        agent.session.save = mock.AsyncMock()        # 必须——真实 switch_stt await self.session.save()
        agent.session.service_state = ServiceState() # 必须——auto-mock 会建不同对象，写读不一致
        orig_cfg_model = agent.config.stt.model
        with mock.patch("Module.stt.STTFactory.create") as mc, \
             mock.patch("core.input.custom_providers.resolve_model_params", return_value={}):
            backend = mock.MagicMock(); backend.model = "sensevoice/sense-voice"
            backend.test = mock.AsyncMock(return_value={"ok": True, "latency_ms": 1})
            mc.return_value = backend
            result = await Agent.switch_stt(agent, "sensevoice/sense-voice")  # 绑定方法
        assert result["ok"] is True
        assert agent._stt is backend                 # 新后端已挂槽（switch_stt 写 self._stt）
        assert agent.session.service_state.stt_model == "sensevoice/sense-voice"
        assert agent.config.stt.model == orig_cfg_model   # config 不被改写（同对象 → == 成立）
        assert agent._stt_ok is True                 # 断言后备字段（agent.stt_ok 在 mock 上是 auto-mock，is True 恒 False）

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_switch_llm_create_failure_keeps_old(self, fake_agent):
        """switch_llm 先建后换：LLMFactory.create 失败时旧后端 + _requested_model 均保留。"""
        agent = fake_agent
        agent.session.save = mock.AsyncMock()
        agent.session.service_state = ServiceState()
        test_backend = mock.MagicMock()
        # 用 _test_backend 构造真实 LLMAdapter（llm_adapter.py:40 测试参）——避免构造时真建网络 client；
        # mock 的 _llm.switch_model 不会走 LLMFactory.create，无法测 create 失败
        agent._llm = LLMAdapter(model="deepseek/deepseek-v4-flash", _test_backend=test_backend)
        with mock.patch("Module.llm.LLMFactory.create", side_effect=RuntimeError("boom")):
            result = await Agent.switch_llm(agent, "deepseek/deepseek-v4-pro")  # 绑定方法
        assert result["ok"] is False
        assert agent._llm._backend is test_backend   # 先建后换：create 失败 _backend 未动
        # 关键：_requested_model 必须保留旧值——当前代码（llm_adapter.py:195-196）先
        # _requested_model = new 再 create，create 抛错时 _requested_model 被 clobber 成
        # "deepseek/deepseek-v4-pro"；先建后换保留旧值。**没有这条断言，该测试对现状代码也是绿的**
        # （_backend 未赋值 + ok is False 现状已成立），TDD 红阶段空转。
        assert agent._llm._requested_model == "deepseek/deepseek-v4-flash"
