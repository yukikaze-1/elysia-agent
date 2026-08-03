"""批次 2 Task 3：UserInputData/_PendingMessage 的 message_id 字段 + process_message 线程传递（marker=unit/service）。"""

from unittest.mock import MagicMock

import pytest

from core.agent._types import _PendingMessage
from core.schema.event import UserInputData


@pytest.mark.unit
def test_user_input_data_message_id_fields():
    """UserInputData 新可选字段：message_id + protocol_version。"""
    data = UserInputData(
        text="hi", user_name="u", request_id="r",
        message_id="msg_x", protocol_version=2,
    )
    assert data.message_id == "msg_x"
    assert data.protocol_version == 2


@pytest.mark.unit
def test_pending_message_carries_message_id():
    """_PendingMessage 携带预分配 messageId（忙时排队不丢）。"""
    pm = _PendingMessage(
        text="hi", user_name="u", request_id="r", content=None, message_id="msg_x",
    )
    assert pm.message_id == "msg_x"


@pytest.mark.service
class TestProcessMessageThreading:
    @pytest.mark.asyncio
    async def test_process_message_posts_user_input_data_with_message_id(self):
        """process_message(message_id=…) → 发布的 UserInputData 携带该 ID。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        agent._bus.post = MagicMock()
        try:
            await agent.process_message("hello", message_id="msg_pre")
            posted = agent._bus.post.call_args.args[0]
            assert isinstance(posted, UserInputData)
            assert posted.message_id == "msg_pre"
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_process_message_busy_queue_preserves_message_id(self):
        """忙时排队：_PendingMessage 携带 messageId。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        try:
            # 强制忙碌（enter_thinking → is_accepting_input=False）
            await agent.phases.enter_thinking()
            await agent.process_message("hello", message_id="msg_pre")
            assert len(agent._pending_messages) == 1
            assert agent._pending_messages[0].message_id == "msg_pre"
        finally:
            await agent.stop()
