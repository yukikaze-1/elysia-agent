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

    def test_add_ref_skips_soft_deleted(self):
        """review 复核 #7：add_ref 仅对 active 附件生效——已软删（如 GC 规则 2 文件缺失
        软删）的附件不能再积累新引用，否则 ref_count>0 使 GC 永不回收（文件永久泄漏）。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_x", status=STATUS_DELETED,
                              deleted_at=_NOW)))
        _run(repo.add_ref("att_x", "msg_1"))
        doc = _run(repo.get("att_x"))
        assert doc["refs"] == []
        assert doc["ref_count"] == 0
        assert doc["status"] == STATUS_DELETED

    def test_soft_delete_if_unreferenced_noop_when_ref_count_gt_0(self):
        """review 复核 #7：GC 条件软删必须原子判定 ref_count==0 且 active——查询与删除
        之间 add_ref 竞入（ref_count=1）时 no-op，附件保持 active；否则「消息已 accepted
        但附件被标记删除」。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_race")))
        # 模拟竞态窗口内消息引用已落库：ref_count=1（查询快照与条件更新之间）
        _run(repo.add_ref("att_race", "msg_1"))
        _run(repo.soft_delete_if_unreferenced("att_race"))
        doc = _run(repo.get("att_race"))
        assert doc["status"] == STATUS_ACTIVE   # 条件失败 → 不软删
        assert doc["ref_count"] == 1

    def test_soft_delete_if_unreferenced_applies_when_zero(self):
        """ref_count==0 且 active → 条件软删生效（GC 规则 2/3 正常路径）。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_clean")))
        _run(repo.soft_delete_if_unreferenced("att_clean"))
        doc = _run(repo.get("att_clean"))
        assert doc["status"] == STATUS_DELETED
        assert doc["deleted_at"]
        assert doc["ref_count"] == 0
        assert doc["status"] == STATUS_DELETED
        assert doc["deleted_at"]

    def test_add_ref_concurrent_no_lost_update(self):
        """并发多次 add_ref 不同 message_id → 原子 addToSet+inc 不丢更新（B 簇 minor）。

        共享 fake 不做人为 sleep 交错（会扰动无关异步测试，见 _fake_documentdb 注释），
        故并发不变量在此不测真实竞态；原子性由调用形态断言（单次条件 update、
        无 get 前置读改写）验证——这正是旧实现（get → $set 全量）做不到的。
        """
        fake = FakeDocumentDBBackend()
        repo = AttachmentRepository(db=fake)
        _run(repo.create(_doc(attachment_id="att_x")))

        # spy update_one：记录 filter + update 调用形态
        calls: list[tuple[dict, dict]] = []
        original = fake.update_one

        async def spy(collection, filter_expr, update, upsert=False):
            calls.append((filter_expr, update))
            return await original(collection, filter_expr, update, upsert)

        fake.update_one = spy  # type: ignore[method-assign]

        async def run():
            await repo.add_ref("att_x", "m1")
            await asyncio.gather(
                *[repo.add_ref("att_x", f"m{i}") for i in range(2, 8)])
            return await repo.get("att_x")

        doc = _run(run())
        assert doc["ref_count"] == len(doc["refs"]) == 7  # 不变量

        # 原子性：add_ref 只发条件 update_one（refs 不含该 message_id），
        # 且每次 update 同时 $addToSet + $inc（无 get 前置读改写）
        add_calls = [c for c in calls if "$addToSet" in c[1]]
        assert len(add_calls) == 7
        for filter_expr, update in add_calls:
            assert isinstance(filter_expr["refs"], dict) and "$ne" in filter_expr["refs"]
            assert "$addToSet" in update and "$inc" in update
            assert update["$inc"]["ref_count"] == 1

    def test_add_ref_idempotent_still_holds(self):
        """幂等保持：重复 add 同 message_id → 不双计（既有语义回归）。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_x")))
        _run(repo.add_ref("att_x", "m1"))
        _run(repo.add_ref("att_x", "m1"))
        doc = _run(repo.get("att_x"))
        assert doc["ref_count"] == 1 and doc["refs"] == ["m1"]

    def test_remove_ref_soft_delete_on_zero(self):
        """原子 remove 归零 → 条件软删（ref_count=0 且 active）。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(attachment_id="att_x")))
        _run(repo.add_ref("att_x", "m1"))
        _run(repo.remove_ref("att_x", "m1"))
        doc = _run(repo.get("att_x"))
        assert doc["status"] == "deleted" and doc["ref_count"] == 0


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


class TestPagination:
    """P0-01 回归：find_by_status / find_unreferenced_older_than 必须完整分页。

    Mongo 默认 find limit=100 —— 只依赖默认值会截断第 101+ 条，导致 GC
    规则 4 把仍有效、仍有引用的附件误判为磁盘孤儿并删除。
    """

    def test_find_by_status_returns_more_than_100(self):
        """150 条 deleted 记录：完整分页必须全部返回，不能只剩 100。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        for i in range(150):
            _run(repo.create(_doc(
                attachment_id=f"att_{i:03d}", status=STATUS_DELETED,
                deleted_at=_NOW)))
        deleted = _run(repo.find_by_status(STATUS_DELETED))
        assert len(deleted) == 150
        assert {d["attachment_id"] for d in deleted} == {
            f"att_{i:03d}" for i in range(150)}

    def test_find_unreferenced_older_than_returns_more_than_100(self):
        """150 条未关联候选：完整分页必须全部返回。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        for i in range(150):
            _run(repo.create(_doc(
                attachment_id=f"att_{i:03d}", ref_count=0,
                created_at="2026-08-01T00:00:00+00:00")))
        found = _run(repo.find_unreferenced_older_than("2026-08-03T00:00:00+00:00"))
        assert len(found) == 150

    def test_page_failure_fail_closed(self):
        """分页中途 DB 失败 → 抛 StorageUnavailableError，绝不返回部分结果。"""
        class _FailOnSecondFind(FakeDocumentDBBackend):
            def __init__(self):
                super().__init__()
                self._find_count = 0

            async def find(self, collection, filter_expr, *, sort=None,
                           limit=100, skip=0):
                self._find_count += 1
                if self._find_count == 2:
                    raise RuntimeError("simulated DB outage")
                return await super().find(
                    collection, filter_expr, sort=sort, limit=limit, skip=skip)

        repo = AttachmentRepository(db=_FailOnSecondFind())
        # 600 条 > 页大小（500）→ 强制跨页，第二页失败
        for i in range(600):
            _run(repo.create(_doc(attachment_id=f"att_{i:04d}")))
        with pytest.raises(StorageUnavailableError):
            _run(repo.find_by_status(STATUS_ACTIVE))

    def test_find_by_stored_name(self):
        """find_by_stored_name（GC 规则 4 逐文件复核）按 kind+stored_name 查单条。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(
            attachment_id="att_x", kind="image", stored_name="x.png")))
        assert _run(repo.find_by_stored_name("image", "x.png")) is not None
        assert _run(repo.find_by_stored_name("video", "x.png")) is None
        assert _run(repo.find_by_stored_name("image", "y.png")) is None


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
