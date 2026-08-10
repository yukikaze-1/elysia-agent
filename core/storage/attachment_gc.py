"""附件 GC 兜底扫描（批次 7 Task 7 + P0-01 完整分页/复核/quarantine 加固）。

启动时后台执行的一次性扫描，回收四类附件目录
（data/uploads/{image,video,audio,file}/）下的死文件与死记录。
data/uploads/images/、data/uploads/desktop/ 为遗留目录，不在扫描范围。

四规则（批次 7 spec §5.4 + plan）：
    规则 1：status=deleted 且 deleted_at 超保留期（默认 24h）→ 隔离文件 + 删记录。
    规则 2：active 记录但磁盘文件缺失 → 软删（mark_deleted）+ warning。
    规则 3：active 且 ref_count=0 且 created_at 超 7 天（未关联保留期）→ 软删
    （文件保留，物理删由规则 1 延后）。
    规则 4：磁盘四 kind 目录有文件但无任何记录（磁盘孤儿）→ 隔离文件。
    规则 5：quarantine 区（data/uploads/.quarantine/）内超保留期的文件 → 物理删除。

P0-01 加固（2026-08-10 全项目 review）：
    1. find_by_status / find_unreferenced_older_than 改为完整 keyset 分页，
       不再依赖 Mongo 默认 limit=100 —— 第 101+ 条有效附件不会被误判为孤儿。
    2. 规则 4 对每个 orphan 候选隔离前复核数据库（find_by_stored_name），
       闭合扫描期间新建引用/记录窗口；复核失败即 fail-closed 中止。
    3. 物理删除改两阶段：先移入 data/uploads/.quarantine/{kind}/（同文件系统
       os.replace，可恢复），规则 5 超保留期后才真正 unlink。
    4. 查询阶段任一异常（含分页中途 DB 失败）→ skipped=True 返回，零删除。
    5. dry_run=True 只计数候选、不落盘不改库，供运维预演。

清理时同步 AttachmentFileResolver.unregister——进程内 ID→路径映射是记录的投影，
避免后续解析指向已删除文件。Mongo 离线（repo 抛 StorageUnavailableError）→
记日志跳过，返回 {"skipped": True}（附件操作 fail-closed，不做半途而废的清理）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from paths import ProjectPath

from core.schema.adapters import attachment_resolver
from core.storage.attachment_repo import (
    KINDS,
    STATUS_ACTIVE,
    STATUS_DELETED,
    StorageUnavailableError,
)

logger = logging.getLogger(__name__)

# quarantine 区目录名（位于上传根内，不在 KINDS 之列——规则 4 不会扫它）
_QUARANTINE_DIR = ".quarantine"


def _parse_iso(ts: str) -> datetime:
    """解析 ISO 时间戳为 aware UTC datetime（naive 视为 UTC）。

    Args:
        ts: ISO 8601 时间戳字符串。

    Returns:
        aware UTC datetime。

    Raises:
        ValueError: 时间戳无法解析。
    """
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _uploads_dir(kind: str) -> Path:
    """返回 kind 对应的上传目录（data/uploads/{kind}）。"""
    return ProjectPath.DATA_UPLOADS.resolve() / kind


def _quarantine_root() -> Path:
    """返回 quarantine 区根目录（data/uploads/.quarantine/）。"""
    return ProjectPath.DATA_UPLOADS.resolve() / _QUARANTINE_DIR


def _quarantine_dest(kind: str, stored_name: str) -> Path:
    """返回隔离目标路径（data/uploads/.quarantine/{kind}/{stored_name}）。"""
    return _quarantine_root() / kind / stored_name


def _file_path(doc: dict[str, Any]) -> Path | None:
    """由附件记录计算落盘路径。缺 kind/stored_name 时返回 None。"""
    kind = doc.get("kind")
    stored_name = doc.get("stored_name")
    if not kind or not stored_name:
        return None
    return _uploads_dir(kind) / stored_name


def _unregister_matching(kind: str, stored_name: str) -> None:
    """从 resolver 注册表移除匹配 (kind, stored_name) 的条目（磁盘孤儿清理用）。

    孤儿文件无记录可反查 attachment_id，故遍历注册表按 (kind, stored_name)
    匹配移除——注册表是记录的进程内投影，孤儿条目属清理残留。
    """
    for aid, entry in list(attachment_resolver._registry.items()):  # noqa: SLF001
        if entry == (kind, stored_name):
            attachment_resolver.unregister(aid)


async def run_attachment_gc(repo: Any, *, deleted_retention_hours: int = 24,
                            unreferenced_retention_days: int = 7,
                            quarantine_retention_hours: int = 168,
                            dry_run: bool = False) -> dict[str, Any]:
    """启动附件 GC 兜底扫描（四规则 + quarantine 清扫，限四 kind 目录）。

    Args:
        repo: AttachmentRepository（fail-closed——离线抛 StorageUnavailableError）。
        deleted_retention_hours: 软删保留期（规则 1），默认 24h。
        unreferenced_retention_days: 未关联保留期（规则 3），默认 7 天。
        quarantine_retention_hours: quarantine 区文件保留期（规则 5），默认 7 天。
        dry_run: True 时只计数候选，不隔离文件、不改库、不物理删除（预演用）。

    Returns:
        统计 dict：{"deleted_files": int, "soft_deleted": int,
        "orphans_removed": int, "quarantine_purged": int, "skipped": bool}。
        Mongo 离线或查询/复核失败时 skipped=True。dry_run 下各计数表示候选数。
    """
    stats: dict[str, Any] = {
        "deleted_files": 0,
        "soft_deleted": 0,
        "orphans_removed": 0,
        "quarantine_purged": 0,
        "skipped": False,
    }
    now = datetime.now(timezone.utc)
    deleted_cutoff = now - timedelta(hours=deleted_retention_hours)
    unref_cutoff = now - timedelta(days=unreferenced_retention_days)

    try:
        active = await repo.find_by_status(STATUS_ACTIVE)
        deleted = await repo.find_by_status(STATUS_DELETED)
        unref_candidates = await repo.find_unreferenced_older_than(unref_cutoff.isoformat())
    except StorageUnavailableError:
        logger.warning("附件 GC 跳过：MongoDB 离线或分页失败（fail-closed）")
        stats["skipped"] = True
        return stats
    except Exception:
        # 查询阶段任一异常都 fail-closed：不给删除阶段不完整数据
        logger.warning("附件 GC 跳过：查询失败（fail-closed）", exc_info=True)
        stats["skipped"] = True
        return stats

    try:
        # ---- 规则 1：软删超保留期 → 隔离文件 + 删记录 ----
        for doc in deleted:
            aid = doc.get("attachment_id")
            kind = doc.get("kind")
            deleted_at = doc.get("deleted_at")
            if not aid or not deleted_at:
                continue
            try:
                if _parse_iso(deleted_at) >= deleted_cutoff:
                    continue  # 未超保留期——保留
            except ValueError:
                continue  # 无法解析的时间戳不处理
            path = _file_path(doc)
            if path is not None and path.is_file():
                if dry_run:
                    stats["deleted_files"] += 1
                    continue
                try:
                    dest = _quarantine_dest(doc.get("kind") or "", path.name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(dest)
                    logger.info("附件 GC：隔离软删超期文件 aid=%s kind=%s",
                                aid, kind)
                except OSError:
                    logger.warning("附件 GC：隔离文件失败 aid=%s，保留记录待重试", aid)
                    continue   # 不删记录，下次 GC 重试
            await repo.delete_record(aid)
            attachment_resolver.unregister(aid)
            stats["deleted_files"] += 1

        # 已软删记录集合——避免规则 2/3 重复计数同一条
        soft_deleted_ids: set[str] = set()

        # ---- 规则 2：active 记录磁盘文件缺失 → 软删 + warning ----
        for doc in active:
            aid = doc.get("attachment_id")
            if not aid or aid in soft_deleted_ids:
                continue
            path = _file_path(doc)
            if path is None or path.is_file():
                continue  # 文件在盘——正常
            if dry_run:
                stats["soft_deleted"] += 1
                continue
            # review 复核 #7：条件软删（ref_count==0 且 active 原子判定）——查询与
            # 删除之间 add_ref 竞入则 no-op，防「消息已 accepted 但附件被标记删除」
            if not await repo.soft_delete_if_unreferenced(aid):
                continue  # 竞态内已被引用/已删——不计数不注销
            attachment_resolver.unregister(aid)
            soft_deleted_ids.add(aid)
            stats["soft_deleted"] += 1
            logger.warning("附件 GC：active 记录磁盘文件缺失，软删 aid=%s kind=%s",
                           aid, doc.get("kind"))

        # ---- 规则 3：未关联（ref_count=0）超保留期 → 软删 ----
        for doc in unref_candidates:
            aid = doc.get("attachment_id")
            if not aid or aid in soft_deleted_ids:
                continue
            if doc.get("status") != STATUS_ACTIVE:
                continue  # 仅处理仍 active 的未关联记录
            if dry_run:
                stats["soft_deleted"] += 1
                continue
            # review 复核 #7：条件软删（ref_count==0 且 active 原子判定）——查询与
            # 删除之间 add_ref 竞入则 no-op，防「消息已 accepted 但附件被标记删除」
            if not await repo.soft_delete_if_unreferenced(aid):
                continue  # 竞态内已被引用/已删——不计数不注销
            attachment_resolver.unregister(aid)
            soft_deleted_ids.add(aid)
            stats["soft_deleted"] += 1
            logger.info("附件 GC：未关联超 %s 天软删 %s", unreferenced_retention_days, aid)

        # ---- 规则 4：磁盘孤儿（四 kind 目录有文件但无记录）→ 复核后隔离 ----
        known: set[tuple[str, str]] = {
            (d.get("kind"), d.get("stored_name"))
            for d in [*active, *deleted]
            if d.get("kind") and d.get("stored_name")
        }
        for kind in KINDS:
            directory = _uploads_dir(kind)
            if not directory.is_dir():
                continue
            for f in directory.iterdir():
                if not f.is_file():
                    continue
                if (kind, f.name) in known:
                    continue  # 有记录对应——非孤儿
                # 逐文件复核数据库：闭合扫描期间新建引用/记录窗口（P0-01）。
                # 复核失败 → fail-closed 中止孤儿清理，不冒险隔离。
                try:
                    existing = await repo.find_by_stored_name(kind, f.name)
                except StorageUnavailableError:
                    logger.warning("附件 GC 中断：复核 DB 时离线，中止孤儿清理")
                    stats["skipped"] = True
                    return stats
                if existing is not None:
                    continue  # 扫描期间刚被登记——不是孤儿，保留
                if dry_run:
                    stats["orphans_removed"] += 1
                    continue
                try:
                    dest = _quarantine_dest(kind, f.name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    f.replace(dest)
                    stats["orphans_removed"] += 1
                    _unregister_matching(kind, f.name)
                    logger.info("附件 GC：隔离磁盘孤儿文件 kind=%s name=%s",
                                kind, f.name)
                except OSError:
                    # 不带 exc_info：OSError 消息含绝对路径，会重新泄漏（review Minor）
                    logger.warning("附件 GC：隔离孤儿文件失败 kind=%s name=%s",
                                   kind, f.name)

        # ---- 规则 5：quarantine 区内超保留期文件 → 物理删除（两阶段第二步） ----
        qroot = _quarantine_root()
        if qroot.is_dir():
            q_cutoff = now - timedelta(hours=quarantine_retention_hours)
            for kind_dir in qroot.iterdir():
                if not kind_dir.is_dir():
                    continue
                for f in kind_dir.iterdir():
                    if not f.is_file():
                        continue
                    try:
                        mtime = datetime.fromtimestamp(
                            f.stat().st_mtime, tz=timezone.utc)
                    except OSError:
                        continue
                    if mtime >= q_cutoff:
                        continue  # 仍在保留期内
                    if dry_run:
                        stats["quarantine_purged"] += 1
                        continue
                    try:
                        f.unlink()
                        stats["quarantine_purged"] += 1
                        logger.info("附件 GC：物理删除超期 quarantine 文件 kind=%s name=%s",
                                    kind_dir.name, f.name)
                    except OSError:
                        logger.warning("附件 GC：删除 quarantine 文件失败 kind=%s name=%s",
                                       kind_dir.name, f.name)
    except StorageUnavailableError:
        logger.warning("附件 GC 中断：MongoDB 中途离线", exc_info=True)
        stats["skipped"] = True
        return stats

    logger.info("附件 GC 扫描完成: %s", stats)
    return stats
