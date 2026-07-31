"""Agent 实例方法 mock 测试。

注：Agent 是顶层类（继承 AgentLifecycle + AgentServer），
方法定义在 agent.py 中。
"""

from collections import deque
from unittest.mock import MagicMock, AsyncMock
import pytest
from core.agent.agent import Agent

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
