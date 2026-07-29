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


class TestBackgroundTaskEntry:
    def test_fields(self):
        from asyncio import Queue
        q = Queue()
        entry = _BackgroundTaskEntry(
            intent="test intent",
            dispatched_at="2026-01-01T00:00:00Z",
            mailbox=q,
        )
        assert entry.intent == "test intent"
        assert entry.dispatched_at == "2026-01-01T00:00:00Z"
        assert entry.mailbox is q


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
