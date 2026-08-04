"""GenerationRegistry 单元测试（批次 3 Task 9，marker=service）。

GenerationRegistry 是 GenerationRepository 之上的薄封装。覆盖：
- register → get(status=running)
- mark(completed) → 终态 + finish_reason + finished_at
- mark upsert 语义（先 mark 后 register 也建档）
- query_by_session status 过滤
- find_by_message
- mark_stale_interrupted（旧 running → interrupted）
- Mongo 离线 register 不抛（repo db=None / db 调用抛异常均降级为 no-op）
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive.generation_registry import GenerationRegistry
from core.storage.generation_repo import GenerationRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service

_NOW = "2026-08-04T00:00:00+00:00"


def _registry() -> GenerationRegistry:
    return GenerationRegistry(repo=GenerationRepository(db=FakeDocumentDBBackend()))


class TestGenerationRegistry:
    def test_register_get_running(self):
        reg = _registry()
        asyncio.run(reg.register(
            "gen_1", session_id="s1", user_message_id="msg_u1",
            message_id="msg_a1", model="mock"))
        doc = asyncio.run(reg.get("gen_1"))
        assert doc is not None
        assert doc["status"] == "running"
        assert doc["session_id"] == "s1"
        assert doc["message_id"] == "msg_a1"
        assert doc["model"] == "mock"
        assert doc["created_at"]
        assert doc["updated_at"]
        assert doc["finished_at"] is None

    def test_mark_completed_sets_terminal_state(self):
        reg = _registry()
        asyncio.run(reg.register(
            "gen_1", session_id="s1", user_message_id="msg_u1",
            message_id="msg_a1", model="mock"))
        asyncio.run(reg.mark("gen_1", status="completed", finish_reason="stop"))
        doc = asyncio.run(reg.get("gen_1"))
        assert doc["status"] == "completed"
        assert doc["finish_reason"] == "stop"
        assert doc["finished_at"]  # 终态落 finished_at

    def test_mark_before_register_creates_doc(self):
        """mark upsert 语义：先 mark 后 register 也建档（mark-before-register）。"""
        reg = _registry()
        asyncio.run(reg.mark("gen_orphan", status="cancelled"))
        doc = asyncio.run(reg.get("gen_orphan"))
        assert doc is not None
        assert doc["status"] == "cancelled"

    def test_query_by_session_status_filter(self):
        reg = _registry()
        asyncio.run(reg.register(
            "g1", session_id="s1", user_message_id="u1", message_id="a1", model="m"))
        asyncio.run(reg.register(
            "g2", session_id="s1", user_message_id="u2", message_id="a2", model="m"))
        asyncio.run(reg.mark("g2", status="completed"))
        docs, has_more, cursor = asyncio.run(reg.query_by_session("s1", status="running"))
        assert [d["generation_id"] for d in docs] == ["g1"]
        assert has_more is False
        assert cursor is None

    def test_find_by_message(self):
        reg = _registry()
        asyncio.run(reg.register(
            "g1", session_id="s1", user_message_id="u1", message_id="msg_a1", model="m"))
        asyncio.run(reg.register(
            "g2", session_id="s1", user_message_id="u2", message_id="msg_a2", model="m"))
        doc = asyncio.run(reg.find_by_message("msg_a2"))
        assert doc["generation_id"] == "g2"

    def test_mark_stale_interrupted(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        reg = GenerationRegistry(repo=repo)
        # 直接插入一条旧 running（模拟崩溃残留）
        asyncio.run(repo.upsert({
            "generation_id": "g_stale",
            "session_id": "s1",
            "user_message_id": "u1",
            "message_id": "a1",
            "model": "m",
            "status": "running",
            "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": "2026-08-01T00:00:00+00:00",
            "finished_at": None,
        }))
        modified = asyncio.run(reg.mark_stale_interrupted("2026-08-03T00:00:00+00:00"))
        assert modified == 1
        assert asyncio.run(reg.get("g_stale"))["status"] == "interrupted"

    def test_mongo_offline_register_no_throw(self):
        """Mongo 离线（repo db=None）register 不抛——降级为 no-op。"""
        reg = GenerationRegistry(repo=GenerationRepository(db=None))
        asyncio.run(reg.register(
            "gen_1", session_id="s1", user_message_id="u1",
            message_id="a1", model="mock"))  # 不抛
        assert asyncio.run(reg.get("gen_1")) is None

    def test_mongo_db_error_no_throw(self):
        """Mongo 后端调用抛异常时 register/mark 不抛（try/except 兜底）。"""
        repo = GenerationRepository(db=MagicMock())
        repo._db.update_one = AsyncMock(side_effect=RuntimeError("mongo down"))
        reg = GenerationRegistry(repo=repo)
        asyncio.run(reg.register(
            "gen_1", session_id="s1", user_message_id="u1",
            message_id="a1", model="mock"))  # 不抛
        asyncio.run(reg.mark("gen_1", status="completed"))  # 不抛
        assert asyncio.run(reg.get("gen_1")) is None  # find_one 也降级为 None
