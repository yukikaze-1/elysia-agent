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
import time
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


def available_kinds() -> set[str]:
    """存在且是目录的 kind 目录集合（S2-B3）。

    **为什么要这个**：规则 2 的判据是"记录 active 但 `path.is_file()` 为假 → 软删"。
    当**整个 kind 目录不存在**时（典型：`mongorestore` 恢复了数据库但没恢复
    `data/uploads` 卷——只备份了 DB，或挂载点没起来），所有 active 记录都命中 →
    全部软删 → 24h 后规则 1 连带把记录物理删掉，**附件记录不可逆全损**。
    更糟的是记录删掉后即使文件恢复，规则 4 会把它们当"磁盘孤儿"隔离并在 7 天后
    物理删。规则 4 本来就有目录存在性守卫——规则 1/2/3 没有，这个不对称就是缺口。
    """
    root = ProjectPath.DATA_UPLOADS.resolve()
    return {kind for kind in KINDS if (root / kind).is_dir()}


def _kind_available(doc: dict[str, Any], available: set[str]) -> bool:
    """该记录所属 kind 的目录是否存在（不存在时不得对它做任何破坏性操作）。"""
    kind = doc.get("kind")
    return bool(kind) and kind in available


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


async def _run_rule1_deleted(
    repo: Any,
    deleted: list[dict],
    deleted_cutoff: datetime,
    dry_run: bool,
    stats: dict[str, Any],
    available: set[str],
) -> None:
    """规则 1：软删超保留期 → 隔离文件 + 删记录。

    S2-B3：kind 目录不存在时跳过该记录——"文件不在"在这种环境下说明不了任何事，
    而删记录是不可逆的。
    """
    for doc in deleted:
        aid = doc.get("attachment_id")
        kind = doc.get("kind")
        deleted_at = doc.get("deleted_at")
        if not aid or not deleted_at:
            continue
        if not _kind_available(doc, available):
            stats["skipped_missing_dir"] += 1
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


async def _run_rule2_missing_files(
    repo: Any,
    active: list[dict],
    soft_deleted_ids: set[str],
    dry_run: bool,
    stats: dict[str, Any],
    available: set[str],
) -> None:
    """规则 2：active 记录磁盘文件缺失 → 条件软删 + warning。

    S2-B3：**kind 目录不存在时整类跳过**——否则"整个目录没了"会被逐条读成
    "每个文件都丢了"，把环境事故放大成不可逆的全量数据删除。
    """
    for doc in active:
        aid = doc.get("attachment_id")
        if not aid or aid in soft_deleted_ids:
            continue
        if not _kind_available(doc, available):
            stats["skipped_missing_dir"] += 1
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


async def _run_rule3_unreferenced(
    repo: Any,
    unref_candidates: list[dict],
    soft_deleted_ids: set[str],
    unreferenced_retention_days: int,
    dry_run: bool,
    stats: dict[str, Any],
    available: set[str],
) -> None:
    """规则 3：未关联（ref_count=0）超保留期 → 条件软删。

    S2-B3：同样受 kind 目录守卫——它的判据不含文件存在性，但"uploads 整体不在"
    时应按环境异常处理，而不是照常删记录。
    """
    for doc in unref_candidates:
        aid = doc.get("attachment_id")
        if not aid or aid in soft_deleted_ids:
            continue
        if doc.get("status") != STATUS_ACTIVE:
            continue  # 仅处理仍 active 的未关联记录
        if not _kind_available(doc, available):
            stats["skipped_missing_dir"] += 1
            continue
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


async def _run_rule4_orphans(
    repo: Any,
    active: list[dict],
    deleted: list[dict],
    dry_run: bool,
    stats: dict[str, Any],
    scan_started_at: float,
) -> bool:
    """规则 4：磁盘孤儿（四 kind 目录有文件但无记录）→ 复核后隔离。

    S2-B4：隔离前加**时间窗**——只处理 `mtime` 早于本轮扫描开始时刻的文件。
    上传的顺序是"先 `write_bytes` 落文件、后 `repo.create` 写记录"，两者之间存在
    真实窗口；原实现靠"逐文件复核 DB"只**缩小**了窗口而非闭合它：复核发生在窗口
    内时必然拿不到记录 → 文件被移进 `.quarantine`、7 天后物理删，而记录却是
    `active + ref_count>0`，规则 1/2 永远不会处理它——**静默坏掉的附件**。

    Args:
        scan_started_at: 本轮扫描开始时刻（`time.time()`）。晚于它落盘的文件
            视为并发上传，留到下一轮。

    Returns:
        True=完成；False=复核时 Mongo 离线，已置 skipped 并中止。
    """
    known: set[tuple[str, str]] = {
        (str(d.get("kind")), str(d.get("stored_name")))
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
            # S2-B4：扫描开始之后落盘 → 并发上传窗口内，本轮不碰（下轮再判）
            try:
                if f.stat().st_mtime >= scan_started_at:
                    stats["orphans_deferred"] += 1
                    continue
            except OSError:
                continue  # 取不到时间戳就不冒险
            # 逐文件复核数据库：闭合扫描期间新建引用/记录窗口（P0-01）。
            # 复核失败 → fail-closed 中止孤儿清理，不冒险隔离。
            try:
                existing = await repo.find_by_stored_name(kind, f.name)
            except StorageUnavailableError:
                logger.warning("附件 GC 中断：复核 DB 时离线，中止孤儿清理")
                stats["skipped"] = True
                return False
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
    return True


def _run_rule5_quarantine(
    now: datetime,
    quarantine_retention_hours: int,
    dry_run: bool,
    stats: dict[str, Any],
) -> None:
    """规则 5：quarantine 区内超保留期文件 → 物理删除（两阶段第二步）。"""
    qroot = _quarantine_root()
    if not qroot.is_dir():
        return
    q_cutoff = now - timedelta(hours=quarantine_retention_hours)
    for kind_dir in qroot.iterdir():
        if not kind_dir.is_dir():
            continue
        for f in kind_dir.iterdir():
            if not f.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
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
        "orphans_removed": int, "orphans_deferred": int, "quarantine_purged": int,
        "skipped": bool, "skipped_missing_dir": int}。
        Mongo 离线或查询/复核失败时 skipped=True。dry_run 下各计数表示候选数。
    """
    stats: dict[str, Any] = {
        "deleted_files": 0,
        "soft_deleted": 0,
        "orphans_removed": 0,
        "orphans_deferred": 0,
        "quarantine_purged": 0,
        "skipped": False,
        # S2-B3：因 kind 目录缺失而跳过的记录数（>0 说明环境异常，需人工介入）
        "skipped_missing_dir": 0,
    }
    # 时间窗基准（S2-B4）与扫描起点同源：晚于此刻落盘的文件一律留到下一轮
    scan_started_at = time.time()
    now = datetime.now(timezone.utc)
    deleted_cutoff = now - timedelta(hours=deleted_retention_hours)
    unref_cutoff = now - timedelta(days=unreferenced_retention_days)

    available = available_kinds()
    if not available:
        # S2-B3：uploads 树下没有任何 kind 目录 → 全量判"文件缺失"会把环境事故
        # 放大成不可逆的全量记录删除。**一条都不动**。
        logger.error(
            "附件 GC 跳过：data/uploads 下没有任何 kind 目录（%s）——"
            "疑似 mongorestore 恢复了数据库但未恢复 uploads 卷。"
            "本轮不做任何删除。", sorted(KINDS))
        stats["skipped"] = True
        return stats
    missing = sorted(set(KINDS) - available)
    if missing:
        logger.error(
            "附件 GC：kind 目录缺失 %s——这些 kind 的记录本轮一律跳过（不软删、"
            "不删记录、不隔离），请确认 uploads 卷是否完整", missing)

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
        await _run_rule1_deleted(repo, deleted, deleted_cutoff, dry_run, stats,
                                 available)

        # 已软删记录集合——避免规则 2/3 重复计数同一条
        soft_deleted_ids: set[str] = set()
        await _run_rule2_missing_files(repo, active, soft_deleted_ids, dry_run, stats,
                                       available)
        await _run_rule3_unreferenced(
            repo, unref_candidates, soft_deleted_ids,
            unreferenced_retention_days, dry_run, stats, available,
        )
        if not await _run_rule4_orphans(repo, active, deleted, dry_run, stats,
                                        scan_started_at):
            return stats
        _run_rule5_quarantine(now, quarantine_retention_hours, dry_run, stats)
    except StorageUnavailableError:
        logger.warning("附件 GC 中断：MongoDB 中途离线", exc_info=True)
        stats["skipped"] = True
        return stats

    logger.info("附件 GC 扫描完成: %s", stats)
    return stats
