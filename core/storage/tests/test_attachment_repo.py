"""AttachmentRepository 单元测试（批次 7 Task 2，marker=service）。

使用内存版 FakeDocumentDBBackend（core/storage/tests/_fake_documentdb.py），
绝不触碰真实 Mongo / 生产 "default" 会话。覆盖：
- create/get 在 db=None（离线）时 raise StorageUnavailableError（fail-closed，
  区别于 GenerationRepository 的静默 no-op）
- create → get（上传落记录，ref_count=0 起步 active）
- add_ref/remove_ref 维护 refs 列表与 ref_count==len(refs) 不变量
- remove_ref 归零自动软删（spec §3.1：消息移除归零 → status=deleted）
- mark_deleted 置 status=deleted + deleted_at
- delete_record 物理删记录（GC 规则 1 用）
- find_unreferenced_older_than（ref_count==0 且 created_at < iso）
- find_by_status("deleted")
- close() 置 _db=None（后续操作 fail-closed）
- Agent 归属（plan review Minor 2）：覆写 agent._attachment_repo 后
  attachment_repo property 返回注入的 fake
"""

import asyncio

import pytest

from core.storage.attachment_repo import (
    AttachmentRepository,
    STATUS_ACTIVE,
    STATUS_DELETED,
    StorageUnavailableError,
)
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service

_NOW = "2026-08-04T00:00:00+00:00"


def _doc(*, attachment_id: str, kind: str = "image", mime: str = "image/png",
         size: int = 100, filename: str = "a.png", stored_name: str = "abc123.png",
         sha256: str = "0" * 64, ref_count: int = 0, refs: list[str] | None = None,
         status: str = STATUS_ACTIVE, created_at: str = _NOW,
         deleted_at: str | None = None, **extra) -> dict:
    """构造最小附件文档（对齐 attachments 集合 schema，批次 7 spec §3.1）。"""
    doc = {
        "attachment_id": attachment_id,
        "kind": kind,
        "mime": mime,
        "size": size,
        "filename": filename,
        "stored_name": stored_name,
        "sha256": sha256,
        "ref_count": ref_count,
        "refs": refs or [],
        "status": status,
        "created_at": created_at,
        "deleted_at": deleted_at,
    }
    doc.update(extra)
    return doc


def _run(coro):
    return asyncio.run(coro)


class TestFailClosed:
    """db=None（Mongo 离线）时附件操作 fail-closed——全部 raise StorageUnavailableError。"""

    def test_create_offline_raises(self):
        repo = AttachmentRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.create(_doc(attachment_id="att_1")))

    def test_get_offline_raises(self):
        repo = AttachmentRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.get("att_1"))

    def test_add_ref_offline_raises(self):
        repo = AttachmentRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.add_ref("att_1", "msg_1"))

    def test_remove_ref_offline_raises(self):
        repo = AttachmentRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.remove_ref("att_1", "msg_1"))

    def test_mark_deleted_offline_raises(self):
        repo = AttachmentRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.mark_deleted("att_1"))

    def test_delete_record_offline_raises(self):
        repo = AttachmentRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.delete_record("att_1"))

    def test_find_by_status_offline_raises(self):
        repo = AttachmentRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.find_by_status(STATUS_DELETED))

    def test_find_unreferenced_offline_raises(self):
        repo = AttachmentRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.find_unreferenced_older_than("2026-08-01T00:00:00+00:00"))


class TestCrud:
    def test_create_then_get(self):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1")))
        doc = _run(repo.get("att_1"))
        assert doc is not None
        assert doc["attachment_id"] == "att_1"
        assert doc["kind"] == "image"
        assert doc["status"] == STATUS_ACTIVE
        assert doc["ref_count"] == 0
        assert doc["refs"] == []
        assert doc["created_at"]  # 建档时间已写入

    def test_create_is_upsert(self):
        """create 用 update_one(upsert=True)——同 attachment_id 二次写不重复插入。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1", size=100)))
        _run(repo.create(_doc(attachment_id="att_1", size=200)))
        docs = _run(repo._db.find("attachments", {}))  # noqa: SLF001
        assert len(docs) == 1
        assert docs[0]["size"] == 200

    def test_get_missing_returns_none(self):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        assert _run(repo.get("att_nope")) is None


class TestReferences:
    def test_add_ref_maintains_invariant(self):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1")))
        _run(repo.add_ref("att_1", "msg_1"))
        _run(repo.add_ref("att_1", "msg_2"))
        doc = _run(repo.get("att_1"))
        assert doc["refs"] == ["msg_1", "msg_2"]
        assert doc["ref_count"] == len(doc["refs"]) == 2
        assert doc["status"] == STATUS_ACTIVE

    def test_add_ref_idempotent_same_message(self):
        """同 message_id 至多入 refs 一次（幂等去重）。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1")))
        _run(repo.add_ref("att_1", "msg_1"))
        _run(repo.add_ref("att_1", "msg_1"))
        doc = _run(repo.get("att_1"))
        assert doc["refs"] == ["msg_1"]
        assert doc["ref_count"] == 1

    def test_remove_ref_maintains_invariant(self):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1")))
        _run(repo.add_ref("att_1", "msg_1"))
        _run(repo.add_ref("att_1", "msg_2"))
        _run(repo.remove_ref("att_1", "msg_1"))
        doc = _run(repo.get("att_1"))
        assert doc["refs"] == ["msg_2"]
        assert doc["ref_count"] == len(doc["refs"]) == 1
        assert doc["status"] == STATUS_ACTIVE  # 未归零不软删

    def test_remove_ref_to_zero_soft_deletes(self):
        """spec §3.1：ref_count 由「消息移除」归零 → status=deleted + deleted_at。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1")))
        _run(repo.add_ref("att_1", "msg_1"))
        _run(repo.remove_ref("att_1", "msg_1"))
        doc = _run(repo.get("att_1"))
        assert doc["refs"] == []
        assert doc["ref_count"] == 0
        assert doc["status"] == STATUS_DELETED
        assert doc["deleted_at"]


class TestStatus:
    def test_mark_deleted_sets_status_and_deleted_at(self):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1")))
        _run(repo.mark_deleted("att_1"))
        doc = _run(repo.get("att_1"))
        assert doc["status"] == STATUS_DELETED
        assert doc["deleted_at"]

    def test_mark_deleted_missing_is_noop(self):
        """记录不存在时软删为 no-op（不报错，不插新记录）。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.mark_deleted("att_nope"))
        assert _run(repo.get("att_nope")) is None

    def test_find_by_status_deleted(self):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1", status=STATUS_DELETED,
                              deleted_at=_NOW)))
        _run(repo.create(_doc(attachment_id="att_2")))  # active 不入 deleted
        _run(repo.create(_doc(attachment_id="att_3", status=STATUS_DELETED,
                              deleted_at=_NOW)))
        deleted = _run(repo.find_by_status(STATUS_DELETED))
        assert sorted(d["attachment_id"] for d in deleted) == ["att_1", "att_3"]


class TestGc:
    def test_find_unreferenced_older_than(self):
        """GC 规则 3（未关联 7 天回收）：ref_count==0 且 created_at < iso。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_old", ref_count=0,
                              created_at="2026-08-01T00:00:00+00:00")))
        _run(repo.create(_doc(attachment_id="att_recent", ref_count=0,
                              created_at="2026-08-04T00:00:00+00:00")))
        _run(repo.create(_doc(attachment_id="att_refed", ref_count=1, refs=["m1"],
                              created_at="2026-08-01T00:00:00+00:00")))
        found = _run(repo.find_unreferenced_older_than("2026-08-03T00:00:00+00:00"))
        ids = {d["attachment_id"] for d in found}
        assert "att_old" in ids            # 未关联且早于 iso
        assert "att_recent" not in ids     # created_at 不比 iso 小
        assert "att_refed" not in ids      # ref_count != 0

    def test_delete_record_physically_removes(self):
        """GC 规则 1（软删记录物理删）：delete_record 用物理删除。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_1")))
        _run(repo.create(_doc(attachment_id="att_2")))
        _run(repo.delete_record("att_1"))
        assert _run(repo.get("att_1")) is None
        assert _run(repo.get("att_2")) is not None  # 不影响其他记录


class TestLifecycle:
    def test_close_sets_db_none(self):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.close())
        assert repo._db is None  # noqa: SLF001
        # close 后 fail-closed 恢复
        with pytest.raises(StorageUnavailableError):
            _run(repo.get("att_1"))

    def test_connect_with_injected_db_is_noop_true(self):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        assert _run(repo.connect()) is True  # 已注入 db 视为已连接


class TestAgentOwnership:
    """批次 7 plan review C2/Minor 2：agent.attachment_repo 归属。"""

    def _make_agent(self):
        from core.agent import Agent
        from core.config import AppConfig
        return Agent(config=AppConfig())

    def test_attachment_repo_property_returns_injected(self):
        """覆写 agent._attachment_repo = fake 后，attachment_repo 返回 fake。"""
        from unittest.mock import MagicMock

        agent = self._make_agent()
        fake = MagicMock()
        agent._attachment_repo = fake  # noqa: SLF001
        assert agent.attachment_repo is fake

    def test_attachment_repo_property_lazy_init(self):
        """未注入时首访 property 构造真实 AttachmentRepository 并缓存。"""
        agent = self._make_agent()
        repo = agent.attachment_repo
        assert isinstance(repo, AttachmentRepository)
        assert repo is agent._attachment_repo  # noqa: SLF001
