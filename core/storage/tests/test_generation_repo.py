"""GenerationRepository 单元测试（批次 3 Task 9，marker=service）。

使用内存版 FakeDocumentDBBackend（core/storage/tests/_fake_documentdb.py），
绝不触碰真实 Mongo / 生产 "default" 会话。覆盖：
- upsert → find_one（register 建档 running）
- upsert 是 upsert（mark-before-register 也建档，二次写入不重复插入）
- find_by_message（assistant 父消息 ID 反查最近一条）
- find_by_session status 过滤 + keyset 分页（created_at DESC, generation_id DESC）
- mark_stale（旧 running → interrupted，其余不动）
- db=None 安全降级
"""

import asyncio

import pytest

from core.schema.types import GenerationId, MessageId, SessionId
from core.schema.utils import now_iso
from core.storage.generation_repo import GenerationRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service

_NOW = "2026-08-04T00:00:00+00:00"


def _doc(*, generation_id: str, session_id: str = "s1", message_id: str = "msg_a1",
         status: str = "running", created_at: str = _NOW, **extra) -> dict:
    """构造最小 generation 文档（对齐 generations 集合 schema）。"""
    doc = {
        "generation_id": generation_id,
        "session_id": session_id,
        "user_message_id": "msg_u1",
        "message_id": message_id,
        "model": "mock",
        "status": status,
        "finish_reason": None,
        "error": None,
        "created_at": created_at,
        "updated_at": created_at,
        "finished_at": None,
    }
    doc.update(extra)
    return doc


def _upsert(repo: GenerationRepository, **kw) -> None:
    asyncio.run(repo.upsert(_doc(**kw)))


class TestGenerationRepository:
    def test_upsert_then_find_one_running(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g1")
        doc = asyncio.run(repo.find_one(GenerationId("g1")))
        assert doc is not None
        assert doc["generation_id"] == "g1"
        assert doc["status"] == "running"
        assert doc["session_id"] == "s1"
        assert doc["created_at"]  # 建档时间已写入

    def test_upsert_creates_on_first_write_mark_before_register(self):
        """mark-before-register：先写终态也能建档（upsert 语义）。"""
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        asyncio.run(repo.upsert(_doc(generation_id="g1", status="cancelled")))
        doc = asyncio.run(repo.find_one(GenerationId("g1")))
        assert doc["status"] == "cancelled"
        # 二次 upsert 更新而非重复插入
        asyncio.run(repo.upsert(_doc(generation_id="g1", status="completed",
                                     created_at="2026-08-04T01:00:00+00:00")))
        all_docs = asyncio.run(repo._db.find("generations", {}))
        assert len(all_docs) == 1
        assert all_docs[0]["status"] == "completed"

    def test_upsert_update_preserves_other_fields(self):
        """register 后 mark：$set 只更新 status，不覆盖 session/message/model。"""
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g1", session_id="s9", message_id="msg_x",
                model="deepseek/test")
        asyncio.run(repo.upsert({
            "generation_id": "g1",
            "status": "completed",
            "finish_reason": "stop",
            "updated_at": "2026-08-04T02:00:00+00:00",
        }))
        doc = asyncio.run(repo.find_one(GenerationId("g1")))
        assert doc["status"] == "completed"
        assert doc["finish_reason"] == "stop"
        assert doc["session_id"] == "s9"     # 不被覆盖
        assert doc["message_id"] == "msg_x"  # 不被覆盖
        assert doc["model"] == "deepseek/test"

    def test_find_by_message_most_recent(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g1", message_id="msg_a1",
                created_at="2026-08-04T10:00:00+00:00")
        _upsert(repo, generation_id="g2", message_id="msg_a1",
                created_at="2026-08-04T11:00:00+00:00")
        _upsert(repo, generation_id="g3", message_id="msg_a2",
                created_at="2026-08-04T12:00:00+00:00")
        doc = asyncio.run(repo.find_by_message(MessageId("msg_a1")))
        assert doc["generation_id"] == "g2"  # 最近一条按 assistant 父消息 ID 反查

    def test_find_by_message_missing_returns_none(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        assert asyncio.run(repo.find_by_message(MessageId("nope"))) is None

    def test_find_by_user_message_most_recent(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g1", user_message_id="msg_u1",
                created_at="2026-08-04T10:00:00+00:00")
        _upsert(repo, generation_id="g2", user_message_id="msg_u1",
                created_at="2026-08-04T11:00:00+00:00")
        _upsert(repo, generation_id="g3", user_message_id="msg_u2",
                created_at="2026-08-04T12:00:00+00:00")
        doc = asyncio.run(repo.find_by_user_message(MessageId("msg_u1")))
        assert doc["generation_id"] == "g2"  # 最近一条按 user_message_id 反查

    def test_find_by_user_message_missing_returns_none(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        assert asyncio.run(repo.find_by_user_message(MessageId("nope"))) is None

    def test_find_by_session_status_filter(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g1", status="running")
        _upsert(repo, generation_id="g2", status="completed")
        _upsert(repo, generation_id="g3", status="failed")
        docs, has_more, cursor = asyncio.run(
            repo.find_by_session(SessionId("s1"), status="running"))
        assert [d["generation_id"] for d in docs] == ["g1"]
        assert has_more is False
        assert cursor is None

    def test_find_by_session_keyset_pagination(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        timestamps = {
            "g1": "2026-08-04T10:00:00+00:00",
            "g2": "2026-08-04T11:00:00+00:00",
            "g3": "2026-08-04T12:00:00+00:00",
            "g4": "2026-08-04T13:00:00+00:00",
        }
        for gid, ts in timestamps.items():
            _upsert(repo, generation_id=gid, created_at=ts)

        page1, has_more1, cursor1 = asyncio.run(repo.find_by_session(SessionId("s1"), limit=2))
        assert [d["generation_id"] for d in page1] == ["g4", "g3"]
        assert has_more1 is True
        assert cursor1 is not None

        page2, has_more2, cursor2 = asyncio.run(
            repo.find_by_session(SessionId("s1"), cursor=cursor1, limit=2))
        assert [d["generation_id"] for d in page2] == ["g2", "g1"]
        assert has_more2 is False
        assert cursor2 is None

        ids1 = {d["generation_id"] for d in page1}
        ids2 = {d["generation_id"] for d in page2}
        assert not (ids1 & ids2)  # 无重叠

    def test_find_by_session_scopes_to_session(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g1", session_id="s1")
        _upsert(repo, generation_id="g2", session_id="s2")
        docs, _, _ = asyncio.run(repo.find_by_session(SessionId("s1")))
        assert [d["generation_id"] for d in docs] == ["g1"]

    def test_mark_stale_interrupts_old_running(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g_old", status="running",
                created_at="2026-08-01T00:00:00+00:00")
        _upsert(repo, generation_id="g_recent", status="running",
                created_at="2026-08-04T00:00:00+00:00")
        _upsert(repo, generation_id="g_done", status="completed",
                created_at="2026-08-01T00:00:00+00:00")
        modified = asyncio.run(repo.mark_stale("running", "2026-08-03T00:00:00+00:00"))
        assert modified == 1
        assert asyncio.run(repo.find_one(GenerationId("g_old")))["status"] == "interrupted"
        assert asyncio.run(repo.find_one(GenerationId("g_recent")))["status"] == "running"  # 未过期不动
        assert asyncio.run(repo.find_one(GenerationId("g_done")))["status"] == "completed"  # 非 running 不动

    def test_mark_stale_writes_finished_at(self):
        """批次 C：mark_stale 标 interrupted 时写 finished_at（终态语义与 mark() 一致）。

        修复前只更新 status+updated_at，finished_at 保持 None——契约
        GenerationStatus.finishedAt 对 stale 清理的 generation 恒空，前端无法
        区分「中断于何时」。
        """
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g_stale", status="running",
                created_at="2026-08-01T00:00:00+00:00")
        asyncio.run(repo.mark_stale("running", "2026-08-03T00:00:00+00:00"))
        doc = asyncio.run(repo.find_one(GenerationId("g_stale")))
        assert doc["status"] == "interrupted"
        assert doc["finished_at"], "stale 清理标 interrupted 应写 finished_at"

    def test_db_none_safe_defaults(self):
        """未连接时的降级语义——**除提交证据探测外**都安全返回默认值。

        `find_by_user_message` 是唯一的例外（S2-B6）：它是幂等恢复的**提交证据**，
        "查不了"与"真的不存在"必须分开——前者若被读成"没处理过"，调用方会 TAKEOVER
        重新处理，导致同一 clientRequestId 产出两条 assistant 回复、归档两次、
        token 翻倍。其余方法（搜索/分页/标记）失败返回默认值是正常降级。
        """
        from core.storage.base import StorageUnavailableError

        repo = GenerationRepository(db=None)
        assert asyncio.run(repo.find_one(GenerationId("g1"))) is None
        assert asyncio.run(repo.find_by_message(MessageId("m1"))) is None
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.find_by_user_message(MessageId("m2")))
        docs, has_more, cursor = asyncio.run(repo.find_by_session(SessionId("s1")))
        assert (docs, has_more, cursor) == ([], False, None)
        assert asyncio.run(repo.mark_stale("running", "iso")) == 0


class TestDeleteBySession:
    """夹具/会话级收尾：按 session 删净该会话的 generation 生命周期记录。"""

    def test_removes_only_that_session(self):
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        _upsert(repo, generation_id="g1", session_id="conv_target")
        _upsert(repo, generation_id="g2", session_id="conv_target")
        _upsert(repo, generation_id="g3", session_id="conv_other")

        removed = asyncio.run(repo.delete_by_session(SessionId("conv_target")))

        assert removed == 2
        docs, _, _ = asyncio.run(repo.find_by_session(SessionId("conv_target")))
        assert docs == []
        others, _, _ = asyncio.run(repo.find_by_session(SessionId("conv_other")))
        assert [d["generation_id"] for d in others] == ["g3"]

    def test_is_idempotent(self):
        """第二次删除返回 0，不抛。"""
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        assert asyncio.run(repo.delete_by_session(SessionId("conv_missing"))) == 0

    def test_raises_when_db_none(self):
        """db=None 抛 StorageUnavailableError——离线删除不得静默返回 0 被当成功。"""
        from core.storage.base import StorageUnavailableError

        repo = GenerationRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.delete_by_session(SessionId("conv_x")))


class TestTtlExpiry:
    """B6/P2-01：generation 临时记录 TTL 基于真实 BSON Date ttl_expires_at。"""

    def test_upsert_sets_bson_expires(self):
        """upsert 自动补 ttl_expires_at（datetime → Mongo BSON Date，TTL 才生效）。"""
        from datetime import datetime, timezone
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        asyncio.run(repo.upsert({"generation_id": "g_ttl", "status": "running"}))
        doc = asyncio.run(repo.find_one(GenerationId("g_ttl")))
        expires = doc.get("ttl_expires_at")
        assert isinstance(expires, datetime), f"ttl_expires_at 应为 BSON Date，实际 {type(expires)}"
        assert expires.tzinfo is not None
        assert expires > datetime.now(timezone.utc)  # 未来 30 天

    def test_backfill_fills_missing_ttl(self):
        """旧记录缺 ttl_expires_at → 启动回填（幂等）。"""
        from datetime import datetime
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        asyncio.run(repo.upsert({"generation_id": "g_old", "status": "completed"}))
        # 手动去掉 ttl_expires_at 模拟旧数据
        coll = repo._db.collections["generations"]  # noqa: SLF001
        for d in coll:
            d.pop("ttl_expires_at", None)

        asyncio.run(repo._backfill_ttl())  # noqa: SLF001

        doc = asyncio.run(repo.find_one(GenerationId("g_old")))
        assert isinstance(doc.get("ttl_expires_at"), datetime)


class TestMarkStaleDoesNotClobberCompleted:
    """S2-C2：`mark_stale` 的读谓词必须**重复出现在写侧**，且返回值必须是真实改动数。

    原实现：读用 `{status, updated_at < cutoff}`、写只剩 `{generation_id}` ——
    find 与 update 之间正常完成的 generation 被**无条件覆写**成 interrupted +
    finished_at（前端当失败、终态被篡改、token 统计对账不上）。返回值还写成
    `modified += 1`（迭代次数）——**一条都没改也会报 N**。
    """

    def _seed(self, repo, gen_id: str, *, status: str, updated_at: str) -> None:
        asyncio.run(repo._db.insert_one(  # noqa: SLF001
            "generations",
            {"generation_id": gen_id, "session_id": "conv_1", "status": status,
             "updated_at": updated_at, "created_at": updated_at,
             "user_message_id": "msg_u", "message_id": "msg_a"},
        ))

    def test_completed_between_find_and_update_is_not_clobbered(self):
        """**TOCTOU**：读之后、写之前完成的 generation 不得被改成 interrupted。"""
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        self._seed(repo, "g_stale", status="running",
                   updated_at="2020-01-01T00:00:00+00:00")
        self._seed(repo, "g_done", status="running",
                   updated_at="2020-01-01T00:00:00+00:00")

        real_find = repo._db.find                       # noqa: SLF001
        flipped = {"done": False}

        async def _find_then_complete(collection, filter_expr, **kw):
            docs = await real_find(collection, filter_expr, **kw)
            if not flipped["done"] and docs:
                # 模拟"find 之后、update 之前"另一条路径把 g_done 正常收尾
                flipped["done"] = True
                await repo._db.update_one(     # noqa: SLF001
                    "generations", {"generation_id": "g_done"},
                    {"$set": {"status": "completed", "updated_at": now_iso()}})
            return docs

        repo._db.find = _find_then_complete            # type: ignore[method-assign]  # noqa: SLF001
        modified = asyncio.run(repo.mark_stale(
            "running", "2026-01-01T00:00:00+00:00"))

        done = asyncio.run(repo.find_one(GenerationId("g_done")))
        assert done is not None and done["status"] == "completed", (
            f"期间完成的 generation 被覆写了：{done}")
        assert modified == 1, f"返回值必须是**真实改动数**（实得 {modified}）"

    def test_counts_only_actual_modifications(self):
        """返回值 = modified_count，不是迭代次数。"""
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        self._seed(repo, "g1", status="running", updated_at="2020-01-01T00:00:00+00:00")
        self._seed(repo, "g2", status="running", updated_at="2020-01-01T00:00:00+00:00")
        self._seed(repo, "g_new", status="running",
                   updated_at="2030-01-01T00:00:00+00:00")   # 未过期

        modified = asyncio.run(repo.mark_stale(
            "running", "2026-01-01T00:00:00+00:00"))

        assert modified == 2, f"只应改动 2 条过期记录，实得 {modified}"
        assert asyncio.run(repo.find_one(GenerationId("g_new")))["status"] == "running"

    def test_scans_past_backend_default_limit(self):
        """分页扫描：超过后端默认 limit=100 的残留也要清完。"""
        repo = GenerationRepository(db=FakeDocumentDBBackend())
        for i in range(150):
            self._seed(repo, f"g{i:03d}", status="running",
                       updated_at="2020-01-01T00:00:00+00:00")

        modified = asyncio.run(repo.mark_stale(
            "running", "2026-01-01T00:00:00+00:00"))

        assert modified == 150, f"默认 limit=100 会漏掉后半批，实得 {modified}"
