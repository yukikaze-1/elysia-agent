"""批次 3 Task 13：/api/messages 全库 `_id` 游标兼容接口 wire 回归锁定（marker=service）。

覆盖 spec §10.3：`/api/messages` 作为兼容接口零改动保留（handler 与
`rest.MessagesResponse` 不改），响应形状恒为 `{messages, has_more, total}`，
游标为消息项上的 `_id`（Mongo ObjectId 字符串），全库（跨会话）分页不重不漏。
`/api/sessions/{conversationId}/messages` 才是产品级会话历史接口——本测试锁定
兼容接口 wire 不回退。

生产数据保护：全部用 FakeDocumentDBBackend 内存库 + 隔离消息 ID，不触碰真实 Mongo，
绝不删/清 default。
"""

import pytest

from core.input.tests.test_session_routes import _FakeAgent, _make_app, _append
from core.schema.message import MessageId, UserMessage
from core.schema.part import TextPart


async def _seed(agent, sid: str, count: int, prefix: str = "归档") -> None:
    """按序播种 count 条 user 文本消息到指定会话（timestamp 递增，确定性排序）。"""
    for i in range(count):
        await _append(agent, sid, UserMessage(
            content=[TextPart(content=f"{prefix}{i}")],
            message_id=MessageId(f"msg_{sid}_{i}"),
            timestamp=f"2026-08-01T10:{i // 60:02d}:{i % 60:02d}+00:00",
        ))


@pytest.mark.service
class TestMessagesApiCompat:
    @pytest.mark.asyncio
    async def test_wire_shape_all_database(self, aiohttp_client):
        """响应形状 {messages, has_more, total} 不变；全库 _id 倒序分页不重不漏。"""
        agent = _FakeAgent()
        await _seed(agent, "conv_test_a", 3)
        await _seed(agent, "conv_test_b", 2)
        client = await aiohttp_client(_make_app(agent))

        resp = await client.get("/api/messages", params={"limit": "2"})
        assert resp.status == 200
        data = await resp.json()
        # 兼容接口响应形状锁定：仅 messages / has_more / total（无 cursor 键）
        assert set(data) == {"messages", "has_more", "total"}
        assert data["has_more"] is True
        assert data["total"] == 5          # 全库统计（跨会话）
        assert len(data["messages"]) == 2
        # 每项带 _id（Mongo 归档游标）+ message_id + parts
        for m in data["messages"]:
            assert m["_id"], "归档消息应携带 _id 游标"
            assert m["message_id"].startswith("msg_")
            assert "parts" in m

        # before=_id 翻页 → 下一页不重叠
        before = data["messages"][-1]["_id"]
        resp2 = await client.get("/api/messages", params={"limit": "2", "before": before})
        data2 = await resp2.json()
        page1_ids = {m["_id"] for m in data["messages"]}
        page2_ids = {m["_id"] for m in data2["messages"]}
        assert len(page2_ids) == 2
        assert not (page1_ids & page2_ids)
        assert data2["has_more"] is True
        assert data2["total"] == 5

        # 翻到底 → has_more=False
        before3 = data2["messages"][-1]["_id"]
        resp3 = await client.get("/api/messages", params={"limit": "10", "before": before3})
        data3 = await resp3.json()
        assert data3["has_more"] is False
        assert len(data3["messages"]) == 1
        assert data3["total"] == 5

    @pytest.mark.asyncio
    async def test_empty_all_database(self, aiohttp_client):
        """空库：{messages: [], has_more: false, total: 0}。"""
        agent = _FakeAgent()
        client = await aiohttp_client(_make_app(agent))
        resp = await client.get("/api/messages")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"messages": [], "has_more": False, "total": 0}

    @pytest.mark.asyncio
    async def test_total_is_all_database_not_per_session(self, aiohttp_client):
        """total 为全库计数（非按会话过滤）——兼容接口语义不变。"""
        agent = _FakeAgent()
        await _seed(agent, "conv_test_x", 4)
        await _seed(agent, "conv_test_y", 6)
        client = await aiohttp_client(_make_app(agent))
        resp = await client.get("/api/messages", params={"limit": "20"})
        data = await resp.json()
        assert data["total"] == 10
        assert data["has_more"] is False
        assert len(data["messages"]) == 10
