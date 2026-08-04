# core/agent/tests/test_types.py
"""Agent _types dataclass 验证。"""

import pytest
from core.agent._types import _PendingMessage, _BackgroundTaskEntry, RunState

pytestmark = pytest.mark.unit


class TestPendingMessage:
    def test_fields_defaults(self):
        msg = _PendingMessage(text="hello", user_name="test", request_id="req-1")
        assert msg.text == "hello"
        assert msg.user_name == "test"
        assert msg.request_id == "req-1"
        assert msg.content is None

    def test_with_content(self):
        content = [{"type": "text", "content": "hi"}]
        msg = _PendingMessage(text="", user_name="u", request_id="r",
                              content=content)
        assert msg.content == content

    def test_default_conversation_id_none(self):
        """批次 3：conversation_id 默认 None。"""
        msg = _PendingMessage(text="hello", user_name="test", request_id="req-1")
        assert msg.conversation_id is None

    def test_with_conversation_id_does_not_break_construction(self):
        """批次 3：带 conversation_id 构造不破坏现有字段。"""
        content = [{"type": "text", "content": "hi"}]
        msg = _PendingMessage(text="hello", user_name="test", request_id="req-1",
                              content=content, message_id="msg-1",
                              conversation_id="sess-9")
        assert msg.conversation_id == "sess-9"
        assert msg.text == "hello"
        assert msg.content == content
        assert msg.message_id == "msg-1"


class TestBackgroundTaskEntry:
    def test_fields(self):
        from Module.workflow_engine.mailbox import WorkflowMailbox
        from Module.workflow_contract import WorkflowId
        mailbox = WorkflowMailbox(task_id=WorkflowId("test-task"))
        entry = _BackgroundTaskEntry(
            intent="test intent",
            dispatched_at="2026-01-01T00:00:00Z",
            mailbox=mailbox,
        )
        assert entry.intent == "test intent"
        assert entry.dispatched_at == "2026-01-01T00:00:00Z"
        assert entry.mailbox is mailbox


class TestRunState:
    def test_four_states(self):
        states = set(RunState)
        assert RunState.STOPPED in states
        assert RunState.RUNNING in states
        assert RunState.STOPPING in states
        assert RunState.RESTARTING in states
        assert len(states) == 4

    def test_state_uniqueness(self):
        values = [s.value for s in RunState]
        assert len(values) == len(set(values))
