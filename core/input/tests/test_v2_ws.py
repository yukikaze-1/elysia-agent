"""批次 2 Task 7：ws_handler v2 文本路由 + ACK + 幂等（marker=service）。

覆盖 spec §4/§10 测试 3：
- v2 文本 → user_message.ack（messageId/clientRequestId）
- 重复 clientRequestId 同内容 → 重发原 ACK 不重复生成
- 不同内容 → rejected + error
- 缺 clientRequestId → CONTRACT_VALIDATION
"""

import asyncio
import json

import pytest
from aiohttp import WSMsgType

from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
from core.input.idempotency import reset_store


@pytest.fixture
def fresh_idempotency():
    """每个测试独立幂等表。"""
    reset_store()
    yield


async def _drain(client, headers):
    """建连并排空建连帧，返回 ws。"""
    ws = await client.session.ws_connect(client.make_url("/ws"), headers=headers)
    for _ in range(5):
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            if m.type == WSMsgType.TEXT:
                t = json.loads(m.data).get("type")
                if t in ("capabilities", "server_info"):
                    break
        except asyncio.TimeoutError:
            break
    return ws


async def _send_and_read_ack(ws, content: str, client_request_id: str) -> dict:
    await ws.send_json({
        "type": "text", "content": content,
        "clientRequestId": client_request_id,
    })
    for _ in range(8):
        m = await asyncio.wait_for(ws.receive(), timeout=5)
        if m.type == WSMsgType.TEXT:
            d = json.loads(m.data)
            if d.get("type") == "user_message.ack":
                return d
    raise AssertionError("未收到 user_message.ack")


@pytest.mark.service
class TestV2AckIdempotency:
    @pytest.mark.asyncio
    async def test_v2_text_gets_ack(self, aiohttp_client, fresh_idempotency):
        """v2 文本 → ACK（messageId/clientRequestId/accepted）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            ack = await _send_and_read_ack(ws, "你好", "req_1")
            assert ack["status"] == "accepted"
            assert ack["messageId"].startswith("msg_")
            assert ack["clientRequestId"] == "req_1"
            assert ack["receivedAt"]
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_duplicate_same_content_replays_ack_no_regeneration(self, aiohttp_client, fresh_idempotency):
        """重复 clientRequestId 同内容 → 重发原 ACK（同 messageId），不重复生成。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            ack1 = await _send_and_read_ack(ws, "同一内容", "req_same")
            # 第二次同 ID 同内容 → 重发原 ACK（同 messageId）
            ack2 = await _send_and_read_ack(ws, "同一内容", "req_same")
            assert ack2["messageId"] == ack1["messageId"]
            assert ack2["status"] == "accepted"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_duplicate_different_content_rejected(self, aiohttp_client, fresh_idempotency):
        """重复 clientRequestId 不同内容 → rejected + CONTRACT_VALIDATION。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await _send_and_read_ack(ws, "内容A", "req_conflict")
            ack2 = await _send_and_read_ack(ws, "内容B", "req_conflict")
            assert ack2["status"] == "rejected"
            assert ack2["error"]["code"] == "CONTRACT_VALIDATION"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_missing_client_request_id_rejected(self, aiohttp_client, fresh_idempotency):
        """缺 clientRequestId → rejected + CONTRACT_VALIDATION。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "text", "content": "没有 requestId"})
            for _ in range(8):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "user_message.ack":
                        assert d["status"] == "rejected"
                        assert d["error"]["code"] == "CONTRACT_VALIDATION"
                        break
            else:
                raise AssertionError("未收到 rejected ACK")
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v1_text_no_ack(self, aiohttp_client, fresh_idempotency):
        """v1 文本 → 无 ACK（v1 路径不变）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, None)
            await ws.send_json({"type": "text", "content": "v1 消息"})
            got_ack = False
            for _ in range(10):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    if json.loads(m.data).get("type") == "user_message.ack":
                        got_ack = True
                        break
            assert not got_ack, "v1 客户端不应收到 ACK"
            await ws.close()
        finally:
            await agent.stop()
