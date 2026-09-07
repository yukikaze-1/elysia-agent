"""core/input/ws_handler.py 测试 —— v2 建连/激活发 character.state.changed 快照（阶段 G Task 5）。

Run: python -m pytest core/input/tests/test_ws_handler.py -k "state_snapshot" -v
"""

import asyncio
import json

import pytest
from aiohttp import WSMsgType

from core.input.tests.test_server import _make_agent


@pytest.mark.service
class TestV2StateSnapshot:
    @pytest.mark.asyncio
    async def test_v2_connect_emits_state_snapshot(self, aiohttp_client):
        """v2 建连发 server_info 后应发一条 character.state.changed 快照。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001 空会话
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": "2"},
            ) as ws:
                # 排空建连帧: 读到 server_info 后,应再收一条 character.state.changed
                saw_server_info = False
                saw_state_changed = False
                expression = None
                for _ in range(10):
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    if msg.type != WSMsgType.TEXT:
                        continue
                    t = json.loads(msg.data).get("type")
                    if t == "server_info":
                        saw_server_info = True
                    if t == "character.state.changed":
                        saw_state_changed = True
                    if t == "expression":
                        expression = json.loads(msg.data)
                    if saw_state_changed and expression is not None:
                        break
                assert saw_server_info, "v2 建连应先收 server_info"
                assert saw_state_changed, "v2 建连应发 character.state.changed 快照"
                assert expression is not None
                assert expression["conversationId"] == "default"
                assert expression["characterId"] == "chr_builtin_elysia"
                assert expression["characterInstanceId"] == "chi_builtin_elysia"
                assert expression["characterRevision"] == 1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_activate_session_emits_state_snapshot(self, aiohttp_client):
        """v2 activate_session 切换成功后应再发一条 character.state.changed 快照。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001 空会话
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": "2"},
            ) as ws:
                # 排空建连帧（含 connect 快照）
                for _ in range(10):
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    if msg.type != WSMsgType.TEXT:
                        continue
                    if json.loads(msg.data).get("type") == "character.state.changed":
                        break
                await ws.send_json({"type": "activate_session", "conversationId": "default"})
                saw_state_changed = False
                for _ in range(10):
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    if msg.type != WSMsgType.TEXT:
                        continue
                    if json.loads(msg.data).get("type") == "character.state.changed":
                        saw_state_changed = True
                        break
                assert saw_state_changed, "v2 activate_session 应发 character.state.changed 快照"
        finally:
            await agent.stop()
