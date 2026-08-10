"""附件 GC 兜底扫描测试（批次 7 Task 7，marker=service）。

用临时目录 + 内存 FakeDocumentDBBackend（仿 test_attachment_repo.py 模式），
绝不触碰真实 Mongo / 生产 data/uploads。GC 仅扫四个 kind 目录
（data/uploads/{image,video,audio,file}/），明确排除 images/、desktop/ 遗留目录。

覆盖：
- 规则 1：status=deleted 且 deleted_at 超 24h → 删文件 + delete_record 删记录
  （未超 24h 的软删记录保留）
- 规则 2：active 记录磁盘文件缺失 → mark_deleted 软删 + warning 日志
- 规则 3：active 且 ref_count=0 且 created_at 超 7 天 → 软删（文件保留）
- 规则 4：磁盘四 kind 目录有文件但无 records（孤儿）→ 删文件
- data/uploads/images/、data/uploads/desktop/ 遗留目录不被扫（文件保留）
- Mongo 离线（repo 抛 StorageUnavailableError）→ GC 跳过，返回 skipped=True
- 清理时同步 AttachmentFileResolver.unregister（进程内映射不残留）
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.schema.adapters import attachment_resolver
from core.storage.attachment_gc import run_attachment_gc
from core.storage.attachment_repo import (
    STATUS_ACTIVE,
    STATUS_DELETED,
    AttachmentRepository,
)
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service


def _iso(dt: datetime) -> str:
    """datetime → ISO 8601 UTC 字符串（与 now_iso() 同格式）。"""
    return dt.astimezone(timezone.utc).isoformat()


def _doc(*, attachment_id: str, kind: str = "image", mime: str = "image/png",
         size: int = 100, filename: str = "a.png", stored_name: str = "abc123.png",
         sha256: str = "0" * 64, ref_count: int = 0, refs: list[str] | None = None,
         status: str = STATUS_ACTIVE, created_at: str | None = None,
         deleted_at: str | None = None, **extra) -> dict:
    """构造最小附件文档（对齐 attachments 集合 schema，批次 7 spec §3.1）。"""
    now = _iso(datetime.now(timezone.utc))
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
        "created_at": created_at or now,
        "deleted_at": deleted_at,
    }
    doc.update(extra)
    return doc


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    """把 ProjectPath.DATA_UPLOADS 指到临时目录（ELYSIA_PATH_DATA_UPLOADS 覆盖）。"""
    monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_resolver():
    """清空模块级 resolver 注册表，避免测试间相互污染。"""
    attachment_resolver._registry.clear()  # noqa: SLF001
    yield


def _write(uploads, rel: str, data: bytes = b"x"):
    """在临时上传根下写一个文件（自动建父目录），返回 Path。"""
    from paths import ProjectPath

    root = ProjectPath.DATA_UPLOADS.resolve()
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


class TestRule1DeletedExpired:
    """规则 1：软删超 24h → 删文件 + 删记录；未超期保留。"""

    def test_expired_soft_deleted_file_and_record_removed(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        _run(repo.create(_doc(
            attachment_id="att_expired", status=STATUS_DELETED, deleted_at=old,
            kind="image", stored_name="expired.png")))
        f = _write(uploads, "image/expired.png")
        attachment_resolver.register("att_expired", "image", "expired.png")

        result = _run(run_attachment_gc(repo))

        assert result["deleted_files"] == 1
        assert _run(repo.get("att_expired")) is None   # 记录已物理删除
        assert not f.exists()                          # 文件已删除
        assert attachment_resolver.resolve("att_expired") is None  # 映射已移除

    def test_recent_soft_deleted_kept(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        _run(repo.create(_doc(
            attachment_id="att_recent", status=STATUS_DELETED, deleted_at=recent,
            kind="image", stored_name="recent.png")))
        f = _write(uploads, "image/recent.png")

        result = _run(run_attachment_gc(repo))

        assert result["deleted_files"] == 0
        assert _run(repo.get("att_recent")) is not None  # 记录保留
        assert f.exists()                                # 文件保留

    def test_gc_log_no_absolute_path(self, uploads, caplog):
        """规则1 GC 日志不泄露绝对路径（I5——只记 aid/kind，不记 path）。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        _run(repo.create(_doc(
            attachment_id="att_log", status=STATUS_DELETED, deleted_at=old,
            kind="image", stored_name="log.png")))
        _write(uploads, "image/log.png")

        # GC 规则1 日志是 INFO 级——须显式 at_level(INFO) 才会被捕获（I5）
        with caplog.at_level(logging.INFO):
            _run(run_attachment_gc(repo))

        assert not any("/home/" in r.message or "data/uploads" in r.message
                       for r in caplog.records)

    def test_gc_rule1_keeps_record_on_unlink_error(self, uploads, monkeypatch, caplog):
        """文件存在但移入 quarantine 抛 OSError → 记录保留（不 delete_record），下次 GC 重试。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        _run(repo.create(_doc(
            attachment_id="att_keep", status=STATUS_DELETED, deleted_at=old,
            kind="image", stored_name="keep.png")))
        f = _write(uploads, "image/keep.png")

        def raise_oserror(self, *args, **kwargs):
            raise OSError("simulated quarantine failure")

        monkeypatch.setattr(Path, "replace", raise_oserror)

        with caplog.at_level(logging.WARNING):
            result = _run(run_attachment_gc(repo))

        assert _run(repo.get("att_keep")) is not None  # 记录仍在
        assert result["deleted_files"] == 0            # 未计入删文件
        assert f.exists()                              # 文件仍在
        assert any("保留记录待重试" in r.getMessage() for r in caplog.records)


class TestRule2ActiveFileMissing:
    """规则 2：active 记录磁盘文件缺失 → 软删 + warning。"""

    def test_active_record_missing_file_soft_deleted(self, uploads, caplog):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        # ref_count=0（未关联）；created_at 新排除规则 3 干扰
        _run(repo.create(_doc(
            attachment_id="att_missing", ref_count=0, refs=[],
            kind="file", stored_name="gone.bin")))
        # 文件从不落盘

        with caplog.at_level(logging.WARNING):
            result = _run(run_attachment_gc(repo))

        assert result["soft_deleted"] == 1
        doc = _run(repo.get("att_missing"))
        assert doc["status"] == STATUS_DELETED
        assert doc["deleted_at"]
        assert any("文件缺失" in r.getMessage() for r in caplog.records)

    def test_active_record_missing_file_referenced_kept(self, uploads):
        """review 复核 #7：文件缺失但被消息引用（ref_count>0）→ 条件软删 no-op，
        附件保持 active——不得在「消息已 accepted」后把附件标记删除。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(
            attachment_id="att_ref_missing", ref_count=1, refs=["m1"],
            kind="file", stored_name="gone.bin")))
        # 文件从不落盘

        result = _run(run_attachment_gc(repo))

        assert result["soft_deleted"] == 0
        doc = _run(repo.get("att_ref_missing"))
        assert doc["status"] == STATUS_ACTIVE

    def test_active_record_with_file_untouched(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _run(repo.create(_doc(
            attachment_id="att_ok", ref_count=1, refs=["m1"],
            kind="file", stored_name="ok.bin")))
        f = _write(uploads, "file/ok.bin")

        result = _run(run_attachment_gc(repo))

        assert result["soft_deleted"] == 0
        assert _run(repo.get("att_ok"))["status"] == STATUS_ACTIVE
        assert f.exists()


class TestRule3Unreferenced:
    """规则 3：active 且 ref_count=0 且 created_at 超 7 天 → 软删（文件保留）。"""

    def test_unreferenced_over_7d_soft_deleted(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        old = _iso(datetime.now(timezone.utc) - timedelta(days=8))
        _run(repo.create(_doc(
            attachment_id="att_unref", ref_count=0, kind="file",
            stored_name="unref.bin", created_at=old)))
        f = _write(uploads, "file/unref.bin")

        result = _run(run_attachment_gc(repo))

        assert result["soft_deleted"] == 1
        doc = _run(repo.get("att_unref"))
        assert doc["status"] == STATUS_DELETED
        assert doc["deleted_at"]
        assert f.exists()   # 软删保留文件（物理删由规则 1 延后）

    def test_recent_unreferenced_kept(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        recent = _iso(datetime.now(timezone.utc) - timedelta(days=1))
        _run(repo.create(_doc(
            attachment_id="att_recent_unref", ref_count=0, kind="file",
            stored_name="recent.bin", created_at=recent)))
        f = _write(uploads, "file/recent.bin")

        result = _run(run_attachment_gc(repo))

        assert result["soft_deleted"] == 0
        assert _run(repo.get("att_recent_unref"))["status"] == STATUS_ACTIVE
        assert f.exists()


class TestRule4Orphans:
    """规则 4：磁盘四 kind 目录有文件但无 records → 删文件。"""

    def test_orphan_files_removed_referenced_kept(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        # 合法记录 + 落盘文件（保留）
        _run(repo.create(_doc(
            attachment_id="att_ok", ref_count=1, refs=["m1"],
            kind="audio", stored_name="ok.mp3")))
        ok = _write(uploads, "audio/ok.mp3")
        # 孤儿文件（无记录）——audio + video 两个 kind 目录
        orphan_audio = _write(uploads, "audio/orphan.mp3", data=b"o")
        orphan_video = _write(uploads, "video/orphan.mp4", data=b"v")

        result = _run(run_attachment_gc(repo))

        assert result["orphans_removed"] == 2
        assert ok.exists()            # 有记录的文件保留
        assert not orphan_audio.exists()
        assert not orphan_video.exists()


class TestLegacyDirsExcluded:
    """data/uploads/images/、data/uploads/desktop/ 遗留目录不被扫。"""

    def test_images_and_desktop_files_untouched(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        legacy_image = _write(uploads, "images/legacy.png", data=b"i")
        legacy_desktop = _write(uploads, "desktop/shot.png", data=b"d")

        result = _run(run_attachment_gc(repo))

        assert legacy_image.exists()    # 不被扫
        assert legacy_desktop.exists()  # 不被扫
        assert result["orphans_removed"] == 0


class TestP0OrphanPagination:
    """P0-01 回归：超过 100 条 active 记录时，规则 4 不得误删有效文件。

    旧实现 find_by_status 依赖 Mongo 默认 limit=100，known 集合被截断，
    第 101+ 个仍有记录引用的磁盘文件会被误判为孤儿并 unlink。
    """

    def test_101_active_referenced_files_not_deleted(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        n = 101
        for i in range(n):
            aid = f"att_{i:04d}"
            name = f"f{i:04d}.png"
            _run(repo.create(_doc(
                attachment_id=aid, kind="image", ref_count=1, refs=[f"m{i}"],
                stored_name=name)))
            _write(uploads, f"image/{name}")

        result = _run(run_attachment_gc(repo))

        assert result["orphans_removed"] == 0
        for i in range(n):
            assert (uploads / "image" / f"f{i:04d}.png").exists()

    def test_500_active_referenced_files_not_deleted(self, uploads):
        """500+ 跨页（页大小 500）：两页记录都进入 known，零误删。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        n = 520
        for i in range(n):
            aid = f"att_{i:04d}"
            name = f"f{i:04d}.png"
            _run(repo.create(_doc(
                attachment_id=aid, kind="image", ref_count=1, refs=[f"m{i}"],
                stored_name=name)))
            _write(uploads, f"image/{name}")

        result = _run(run_attachment_gc(repo))

        assert result["orphans_removed"] == 0
        for i in range(n):
            assert (uploads / "image" / f"f{i:04d}.png").exists()

    def test_active_deleted_mixed_cross_page_not_misjudged(self, uploads):
        """active+deleted 混合跨页：全部进入 known，孤儿判定基于完整集合。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        n = 600
        for i in range(n):
            aid = f"att_{i:04d}"
            name = f"f{i:04d}.png"
            status = STATUS_ACTIVE if i % 2 == 0 else STATUS_DELETED
            extra = {"deleted_at": _iso(datetime.now(timezone.utc) - timedelta(hours=25))} \
                if status == STATUS_DELETED else {}
            _run(repo.create(_doc(
                attachment_id=aid, kind="image", ref_count=1, refs=[f"m{i}"],
                stored_name=name, status=status, **extra)))
            _write(uploads, f"image/{name}")

        result = _run(run_attachment_gc(repo))

        # 孤儿判定基于完整集合：零孤儿误判。偶数条 active → 保留原位；
        # 奇数条 deleted 超期 → 规则 1 合法隔离（进 .quarantine，非永久误删）
        assert result["orphans_removed"] == 0
        for i in range(n):
            if i % 2 == 0:
                assert (uploads / "image" / f"f{i:04d}.png").exists()
            else:
                assert (uploads / ".quarantine" / "image" / f"f{i:04d}.png").exists()

    def test_db_page_failure_zero_deletions(self, uploads, caplog):
        """分页中途 DB 失败 → GC fail-closed：任何文件都不被删除。"""
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
        n = 600
        for i in range(n):
            aid = f"att_{i:04d}"
            name = f"f{i:04d}.png"
            _run(repo.create(_doc(
                attachment_id=aid, kind="image", ref_count=1, refs=[f"m{i}"],
                stored_name=name)))
            _write(uploads, f"image/{name}")

        with caplog.at_level(logging.WARNING):
            result = _run(run_attachment_gc(repo))

        assert result["skipped"] is True
        assert result["deleted_files"] == 0
        assert result["soft_deleted"] == 0
        assert result["orphans_removed"] == 0
        for i in range(n):
            assert (uploads / "image" / f"f{i:04d}.png").exists()


class TestRule4Recheck:
    """规则 4 逐文件复核：闭合扫描期间新建引用/记录窗口。"""

    def test_newly_registered_file_not_removed(self, uploads, monkeypatch):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _write(uploads, "file/race.bin", data=b"r")

        async def fake_find_by_stored_name(kind, stored_name):
            if stored_name == "race.bin":
                return {
                    "attachment_id": "att_race", "kind": "file",
                    "stored_name": "race.bin", "status": STATUS_ACTIVE,
                }
            return None

        monkeypatch.setattr(repo, "find_by_stored_name", fake_find_by_stored_name)

        result = _run(run_attachment_gc(repo))

        assert result["orphans_removed"] == 0
        assert (uploads / "file" / "race.bin").exists()


class TestQuarantine:
    """两阶段物理删除：规则 1/4 先移入 .quarantine，规则 5 超期后再真正删除。"""

    def test_rule1_expired_file_moved_to_quarantine(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        _run(repo.create(_doc(
            attachment_id="att_q", status=STATUS_DELETED, deleted_at=old,
            kind="image", stored_name="q.png")))
        _write(uploads, "image/q.png")

        result = _run(run_attachment_gc(repo))

        assert result["deleted_files"] == 1
        assert not (uploads / "image" / "q.png").exists()          # 原路径移除
        assert (uploads / ".quarantine" / "image" / "q.png").exists()  # 进入隔离区
        assert _run(repo.get("att_q")) is None                      # 记录已物理删

    def test_rule4_orphan_moved_to_quarantine(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _write(uploads, "audio/orphan.mp3", data=b"o")

        result = _run(run_attachment_gc(repo))

        assert result["orphans_removed"] == 1
        assert not (uploads / "audio" / "orphan.mp3").exists()
        assert (uploads / ".quarantine" / "audio" / "orphan.mp3").exists()

    def test_quarantine_purged_after_retention(self, uploads):
        """规则 5：quarantine 内超保留期文件被物理删除；新文件保留。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        qdir = uploads / ".quarantine" / "image"
        qdir.mkdir(parents=True, exist_ok=True)
        old = qdir / "old.png"
        old.write_bytes(b"x")
        fresh = qdir / "fresh.png"
        fresh.write_bytes(b"y")
        old_mtime = time.time() - 7200  # 2 小时前 → 超 1h 保留期
        os.utime(old, (old_mtime, old_mtime))

        result = _run(run_attachment_gc(repo, quarantine_retention_hours=1))

        assert result["quarantine_purged"] == 1
        assert not old.exists()
        assert fresh.exists()

    def test_recent_quarantine_kept(self, uploads):
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        qdir = uploads / ".quarantine" / "image"
        qdir.mkdir(parents=True, exist_ok=True)
        fresh = qdir / "fresh.png"
        fresh.write_bytes(b"y")

        result = _run(run_attachment_gc(repo, quarantine_retention_hours=168))

        assert result["quarantine_purged"] == 0
        assert fresh.exists()

    def test_dry_run_does_not_mutate(self, uploads):
        """dry_run：只计数候选，不隔离文件、不改库、不删 quarantine。"""
        repo = AttachmentRepository(db=FakeDocumentDBBackend())
        _write(uploads, "image/dry.png", data=b"d")
        qdir = uploads / ".quarantine" / "image"
        qdir.mkdir(parents=True, exist_ok=True)
        old = qdir / "old.png"
        old.write_bytes(b"x")
        old_mtime = time.time() - 7200
        os.utime(old, (old_mtime, old_mtime))

        result = _run(run_attachment_gc(repo, dry_run=True,
                                        quarantine_retention_hours=1))

        assert result["orphans_removed"] == 1      # 候选计数
        assert result["quarantine_purged"] == 1    # 候选计数
        assert (uploads / "image" / "dry.png").exists()  # 未被隔离
        assert old.exists()                               # 未被物理删


class TestMongoOffline:
    """Mongo 离线（repo 抛 StorageUnavailableError）→ GC 跳过，返回 skipped=True。"""

    def test_offline_skipped(self, caplog):
        repo = AttachmentRepository(db=None)  # fail-closed：操作即抛错

        with caplog.at_level(logging.WARNING):
            result = _run(run_attachment_gc(repo))

        assert result["skipped"] is True
        assert result["deleted_files"] == 0
        assert result["soft_deleted"] == 0
        assert result["orphans_removed"] == 0
        assert any("跳过" in r.getMessage() for r in caplog.records)
