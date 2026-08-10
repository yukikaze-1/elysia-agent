"""批次 2 Task 3：UserInputData/_PendingMessage 的 message_id 字段 + process_message 线程传递（marker=unit/service）。"""

from unittest.mock import MagicMock

import pytest

from core.agent._types import _PendingMessage
from core.schema import MessageId, RequestId
from core.schema.event import UserInputData


@pytest.mark.unit
def test_user_input_data_message_id_fields():
    """UserInputData 新可选字段：message_id + protocol_version。"""
    data = UserInputData(
        text="hi", user_name="u", request_id=RequestId("r"),
        message_id=MessageId("msg_x"), protocol_version=2,
    )
    assert data.message_id == "msg_x"
    assert data.protocol_version == 2


@pytest.mark.unit
def test_pending_message_carries_message_id():
    """_PendingMessage 携带预分配 messageId（忙时排队不丢）。"""
    pm = _PendingMessage(
        text="hi", user_name="u", request_id=RequestId("r"), content=None, message_id=MessageId("msg_x"),
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


@pytest.mark.service
class TestProcessMessageBurstCapacity:
    """review 复核 #4：同 tick 突发——容量检查必须在接受时生效，不得先 ACK 后丢弃。"""

    @pytest.mark.asyncio
    async def test_burst_same_tick_no_ack_then_drop(self):
        """事件未派发（同 tick）时连续三条 process_message：队列容量 1 → 第三条
        必须在接受时返回 False（WS 据此发 QUEUE_FULL rejected），而不是先返回 True
        （accepted ACK）再在事件处理时静默丢弃。"""
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        try:
            agent._config.agent.max_pending_messages = 1  # noqa: SLF001
            # 暂停事件派发——复现同 tick 突发窗口（bus 中 USER_INPUT 不被消费）
            if agent._event_task is not None:  # noqa: SLF001
                agent._event_task.cancel()  # noqa: SLF001

            r1 = await agent.process_message("first", protocol_version=1)
            r2 = await agent.process_message("second", protocol_version=1)
            r3 = await agent.process_message("third", protocol_version=1)

            assert (r1, r2, r3) == (True, True, False)
            # 第三条未接受、未入队——不产生任何 pending 残留
            assert len(agent._pending_messages) == 1  # noqa: SLF001
        finally:
            await agent.stop()
