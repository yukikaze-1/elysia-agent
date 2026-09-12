# 测试文件：core/input/tests/test_chat_content_sanitize.py
"""CR-09 回归守卫：`POST /api/chat` 的客户端 `content` 必须按 v2 同源收口。

背景见 `core/schema/tests/test_image_source_security.py` 与评测报告 CR-09。
本文件守的是**入口侧**：`src` 一律由服务端按 `attachment_id` / `sticker_id` 构造，
客户端提供的 `src` 不参与决策；非法部件一律 400，且**不得到达 `process_message`**。
"""

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from core.agent import RunState
from core.input.server import create_app


def _make_capturing_agent(monkeypatch, tmp_path):
    """构造一个记录 process_message 入参的假 agent。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-CANARY\n", encoding="utf-8")

    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    agent._state = RunState.RUNNING
    agent.user_name = "tester"
    seen: list[dict] = []

    async def _process_message(*, text="", user_name="", content=None, **_kw):
        seen.append({"text": text, "user_name": user_name, "content": content})
        return True

    agent.process_message = _process_message
    return agent, seen


async def _post(aiohttp_client, monkeypatch, tmp_path, body):
    agent, seen = _make_capturing_agent(monkeypatch, tmp_path)
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat", data=json.dumps(body),
        headers={"Content-Type": "application/json"})
    return resp, seen


@pytest.mark.service
class TestChatContentSanitize:
    """入口侧收口规则。"""

    @pytest.mark.asyncio
    async def test_relative_src_without_attachment_id_rejected(self, aiohttp_client, monkeypatch, tmp_path):
        """原始漏洞载荷：src=".env" 且无 attachment_id → 400，且不得到达 process_message。"""
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path,
                                 {"content": [{"type": "image", "src": ".env"}]})
        assert resp.status == 400
        assert seen == [], "非法 content 不得投递给 agent"

    @pytest.mark.asyncio
    async def test_relative_src_with_attachment_id_gets_overwritten(self, aiohttp_client, monkeypatch, tmp_path):
        """带合法 attachment_id 时，客户端 src 被服务端覆盖（v2 同源）。"""
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path, {
            "content": [{"type": "image", "src": ".env", "attachment_id": "att_deadbeef"}],
        })
        assert resp.status == 200
        assert len(seen) == 1
        part = seen[0]["content"][0]
        assert part["src"] == "/api/attachments/att_deadbeef"
        assert ".env" not in json.dumps(part), "客户端 src 必须被丢弃"

    @pytest.mark.asyncio
    async def test_traversal_attachment_id_rejected(self, aiohttp_client, monkeypatch, tmp_path):
        """attachment_id 含穿越/分隔符 → 400。"""
        for bad in ("../../etc/passwd", "att_..%2f", "att_x/y", "att_x\\y"):
            resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path,
                                     {"content": [{"type": "image", "attachment_id": bad}]})
            assert resp.status == 400, f"attachment_id={bad!r} 应被拒"
            assert seen == []

    @pytest.mark.asyncio
    async def test_sticker_src_constructed_server_side(self, aiohttp_client, monkeypatch, tmp_path):
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path,
                                 {"content": [{"type": "sticker", "sticker_id": "st_happy",
                                               "src": "/etc/passwd"}]})
        assert resp.status == 200
        assert seen[0]["content"][0]["src"] == "/static/stickers/st_happy.png"

    @pytest.mark.asyncio
    async def test_sticker_without_id_rejected(self, aiohttp_client, monkeypatch, tmp_path):
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path,
                                 {"content": [{"type": "sticker"}]})
        assert resp.status == 400
        assert seen == []

    @pytest.mark.asyncio
    async def test_all_attachment_part_types_require_id(self, aiohttp_client, monkeypatch, tmp_path):
        """四类附件部件一律要求 attachment_id。"""
        for ptype in ("image", "file", "audio", "video"):
            resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path,
                                     {"content": [{"type": ptype, "src": "secret.txt"}]})
            assert resp.status == 400, f"{ptype} 无 attachment_id 应被拒"
            assert seen == []

    @pytest.mark.asyncio
    async def test_unknown_part_type_rejected(self, aiohttp_client, monkeypatch, tmp_path):
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path,
                                 {"content": [{"type": "bogus"}]})
        assert resp.status == 400
        assert seen == []

    @pytest.mark.asyncio
    async def test_non_list_content_rejected(self, aiohttp_client, monkeypatch, tmp_path):
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path, {"content": "notalist"})
        assert resp.status == 400
        assert seen == []


@pytest.mark.service
class TestChatContentLegitPreserved:
    """收口不得误伤合法请求。"""

    @pytest.mark.asyncio
    async def test_text_part_passthrough(self, aiohttp_client, monkeypatch, tmp_path):
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path,
                                 {"content": [{"type": "text", "content": "你好"}]})
        assert resp.status == 200
        assert seen[0]["content"] == [{"type": "text", "content": "你好"}]

    @pytest.mark.asyncio
    async def test_text_only_request_still_works(self, aiohttp_client, monkeypatch, tmp_path):
        """纯 text 请求 content 缺省 → content 为 None。"""
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path, {"text": "你好"})
        assert resp.status == 200
        assert seen[0]["text"] == "你好"
        assert seen[0]["content"] is None

    @pytest.mark.asyncio
    async def test_canonical_attachment_part_mixed_with_text(self, aiohttp_client, monkeypatch, tmp_path):
        resp, seen = await _post(aiohttp_client, monkeypatch, tmp_path, {"content": [
            {"type": "text", "content": "看这张"},
            {"type": "image", "attachment_id": "att_abc"},
        ]})
        assert resp.status == 200
        content = seen[0]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["src"] == "/api/attachments/att_abc"


@pytest.mark.service
class TestChatBodyShape:
    """非 object body 不得 500（S2-F4 同类；此处只守本端点）。"""

    @pytest.mark.asyncio
    async def test_non_object_body_400_not_500(self, aiohttp_client, monkeypatch, tmp_path):
        for raw in ("[1,2]", '"abc"', "123"):
            agent, seen = _make_capturing_agent(monkeypatch, tmp_path)
            app = create_app(agent, serve_static=False)
            client = await aiohttp_client(app)
            resp = await client.post("/api/chat", data=raw,
                                     headers={"Content-Type": "application/json"})
            assert resp.status == 400, f"body={raw} 应为 400（当前 {resp.status}）"
            assert seen == []
