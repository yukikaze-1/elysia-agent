"""附件 GC 兜底扫描（批次 7 Task 7）。

启动时后台执行的一次性扫描，回收四类附件目录
（data/uploads/{image,video,audio,file}/）下的死文件与死记录。
data/uploads/images/、data/uploads/desktop/ 为遗留目录，不在扫描范围。

四规则（批次 7 spec §5.4 + plan）：
    规则 1：status=deleted 且 deleted_at 超保留期（默认 24h）→ 删文件 + 删记录。
    规则 2：active 记录但磁盘文件缺失 → 软删（mark_deleted）+ warning。
    规则 3：active 且 ref_count=0 且 created_at 超 7 天（未关联保留期）→ 软删
    （文件保留，物理删由规则 1 延后）。
    规则 4：磁盘四 kind 目录有文件但无任何记录（磁盘孤儿）→ 删文件。

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
    for aid, entry in list(attachment_resolver._registry.items()):  # noqa: SLF001 — 投影内省
        if entry == (kind, stored_name):
            attachment_resolver.unregister(aid)


async def run_attachment_gc(repo: Any, *, deleted_retention_hours: int = 24,
                            unreferenced_retention_days: int = 7) -> dict[str, Any]:
    """启动附件 GC 兜底扫描（四规则，限四 kind 目录）。

    Args:
        repo: AttachmentRepository（fail-closed——离线抛 StorageUnavailableError）。
        deleted_retention_hours: 软删保留期（规则 1），默认 24h。
        unreferenced_retention_days: 未关联保留期（规则 3），默认 7 天。

    Returns:
        统计 dict：{"deleted_files": int, "soft_deleted": int,
        "orphans_removed": int, "skipped": bool}。Mongo 离线时 skipped=True。
    """
    stats: dict[str, Any] = {
        "deleted_files": 0,
        "soft_deleted": 0,
        "orphans_removed": 0,
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
        logger.warning("附件 GC 跳过：MongoDB 离线（StorageUnavailableError）")
        stats["skipped"] = True
        return stats

    try:
        # ---- 规则 1：软删超保留期 → 删文件 + 删记录 ----
        for doc in deleted:
            aid = doc.get("attachment_id")
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
                try:
                    path.unlink()
                    logger.info("附件 GC：删除软删超期文件 %s (%s)", path, aid)
                except OSError:
                    logger.warning("附件 GC：删文件失败 %s (%s)", path, aid, exc_info=True)
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
            await repo.mark_deleted(aid)
            attachment_resolver.unregister(aid)
            soft_deleted_ids.add(aid)
            stats["soft_deleted"] += 1
            logger.warning("附件 GC：active 记录磁盘文件缺失，软删 %s (%s)", path, aid)

        # ---- 规则 3：未关联（ref_count=0）超保留期 → 软删 ----
        for doc in unref_candidates:
            aid = doc.get("attachment_id")
            if not aid or aid in soft_deleted_ids:
                continue
            if doc.get("status") != STATUS_ACTIVE:
                continue  # 仅处理仍 active 的未关联记录
            await repo.mark_deleted(aid)
            attachment_resolver.unregister(aid)
            soft_deleted_ids.add(aid)
            stats["soft_deleted"] += 1
            logger.info("附件 GC：未关联超 %s 天软删 %s", unreferenced_retention_days, aid)

        # ---- 规则 4：磁盘孤儿（四 kind 目录有文件但无记录）→ 删文件 ----
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
                try:
                    f.unlink()
                    stats["orphans_removed"] += 1
                    _unregister_matching(kind, f.name)
                    logger.info("附件 GC：删除磁盘孤儿文件 %s", f)
                except OSError:
                    logger.warning("附件 GC：删孤儿文件失败 %s", f, exc_info=True)
    except StorageUnavailableError:
        logger.warning("附件 GC 中断：MongoDB 中途离线", exc_info=True)
        stats["skipped"] = True
        return stats

    logger.info("附件 GC 扫描完成: %s", stats)
    return stats
