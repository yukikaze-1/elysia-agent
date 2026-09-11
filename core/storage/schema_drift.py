"""Mongo 索引 schema 漂移自检（启动期，只读 + 建议性）。

## 背景：为什么需要它

`ensure_index` 是幂等**只增**的：改了索引键会新建一个，旧索引**永远留着**（Mongo
按键自动命名，键不同即名字不同），而代码里**没有任何移除索引的路径**。残留索引会
**静默改变行为**——实测案例：

`sessions` 曾在 2026-07-16 声明 `[("updated_at", 1)], expireAfterSeconds=30*86400`，
2026-08-10 从代码删除（`docs/database-storage.md` §110「整改后不再建 Mongo TTL 索引」）。
但代码删不掉库里已建的索引——**只能靠手工 drop**。若当时漏掉，30 天未更新的会话会
**静默过期消失**。

同类风险还有：残留的唯一索引会让写入突然被拒；残留的错误索引会让查询走错索引。

## 非目标（刻意不做）

- **不做迁移框架**。实测 4 个月里索引定义变更 24 次，其中约 20 次是**纯增量**
  （新集合/新索引/新字段）——这些由 `ensure_index` 在每个 `connect()` 自动完成，
  **零成本**。只有「删/改已有索引」这类非增量变更需要人工介入，约 2 次/4 个月。
  为 8% 的场景引入编号迁移目录 + ledger 集合 + runner + 流程约定，成本远超收益，
  且 ledger 本身又是一份会漂移的常驻状态。
- **不自动删除任何索引**。自检只报告；删除由人用 `drop_index` 显式执行
  （残留索引可能是有人故意建的，自动删会越权）。

## 单一事实来源

`EXPECTED_INDEXES` 是**派生缓存**：它由各仓储 `connect()` 里的 `ensure_index` 调用
静态提取而来，并由 `core/storage/tests/test_schema_drift.py` 用 AST **反向校验**
（解析 `core/storage/*.py` 重新提取，断言与本表逐项一致）。因此新增/修改索引时
若忘记同步本表，**单测会失败并指出差异**——本表不会静默过期。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.database.documentdb import DocumentDBBackend

logger = logging.getLogger(__name__)

_ID_INDEX = "_id_"

EXPECTED_INDEXES: dict[str, frozenset[str]] = {
    "attachments": frozenset({
        "attachment_id_1", "created_at_1", "ref_count_1", "status_1",
    }),
    "character_instances": frozenset({
        "instance_id_1", "owner_user_id_1_character_id_1_canonical_1",
    }),
    "character_revisions": frozenset({"character_id_1_revision_-1"}),
    "character_versions": frozenset({"session_id_1_content_type_1_version_-1"}),
    "characters": frozenset({"character_id_1"}),
    "desktop_calls": frozenset({
        "call_id_1", "status_1_updated_at_1", "ttl_expires_at_1",
    }),
    "generations": frozenset({
        "generation_id_1", "message_id_1", "session_id_1_updated_at_-1",
        "ttl_expires_at_1",
    }),
    "idempotency": frozenset({
        "conversation_id_1_client_request_id_1", "expires_at_1",
    }),
    "message_archive": frozenset({"session_id_1", "session_id_1__id_-1"}),
    "sessions": frozenset({"session_id_1", "updated_at_-1"}),
    "token_usage": frozenset({"session_id_1"}),
    "user_identity_profiles": frozenset({"user_id_1"}),
}
"""collection → 期望存在的索引名集合（24 项 / 12 集合，由单测钉住与代码一致）。"""


@dataclass(frozen=True)
class SchemaDriftReport:
    """索引 schema 漂移自检结果。"""

    scanned: bool = False
    """是否真正完成了扫描（存储不可用 / 查询异常时为 False）。

    与「扫描后无漂移」严格区分——避免把「没查」误报成「健康」。
    """

    orphans: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """**库里有、代码不声明**的索引（危险：会静默改变行为，如残留 TTL 删数据）。"""

    missing: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """代码声明、库里缺失的索引（通常意味着该仓储 `connect()` 未跑成功）。"""

    absent: tuple[str, ...] = ()
    """库中尚不存在的集合（从未写入）——既不算漂移也不报缺失。"""

    @property
    def has_drift(self) -> bool:
        """是否存在需要人工处置的漂移（孤儿索引）。"""
        return bool(self.orphans)

    @property
    def orphan_count(self) -> int:
        """孤儿索引总数。"""
        return sum(len(v) for v in self.orphans.values())

    def summary(self) -> str:
        """人类可读摘要。"""
        if not self.scanned:
            return "索引 schema 漂移自检未执行（存储不可用）"
        if not self.has_drift and not self.missing:
            return f"索引 schema 无漂移（{len(EXPECTED_INDEXES)} 个集合）"
        parts = []
        if self.orphans:
            parts.append(f"孤儿索引 {self.orphan_count} 个")
        if self.missing:
            parts.append(f"缺失索引 {sum(len(v) for v in self.missing.values())} 个")
        return "；".join(parts)


async def check_schema_drift(backend: "DocumentDBBackend | None") -> SchemaDriftReport:
    """对比库中索引与代码声明（只读，绝不抛异常）。

    Args:
        backend: 已连接的 DocumentDB 后端；None 时返回未扫描。

    Returns:
        SchemaDriftReport。任何异常都降级为 `scanned=False`——自检不得让启动失败。
    """
    if backend is None:
        return SchemaDriftReport(scanned=False)

    orphans: dict[str, tuple[str, ...]] = {}
    missing: dict[str, tuple[str, ...]] = {}
    absent: list[str] = []

    try:
        for collection, expected in EXPECTED_INDEXES.items():
            live = await backend.list_indexes(collection)
            if not live:
                # **空列表 = 集合不存在**（从未写入）→ 既非漂移也非缺失，不报噪音。
                # 注意不能用「过滤 _id_ 后为空」判断：集合存在时 Mongo 至少有内建 _id_ 索引，
                # 那种情况是「所有声明索引都缺失」，必须报出来。
                absent.append(collection)
                continue
            names = {
                str(idx.get("name"))
                for idx in live
                if idx.get("name") and idx.get("name") != _ID_INDEX
            }
            extra = names - expected
            gone = expected - names
            if extra:
                orphans[collection] = tuple(sorted(extra))
            if gone:
                missing[collection] = tuple(sorted(gone))
    except Exception:
        logger.debug("索引 schema 漂移自检未执行（查询失败）", exc_info=True)
        return SchemaDriftReport(scanned=False)

    return SchemaDriftReport(
        scanned=True,
        orphans=orphans,
        missing=missing,
        absent=tuple(absent),
    )


def log_schema_drift_report(report: SchemaDriftReport) -> None:
    """写日志：无漂移 debug，有孤儿索引显著 warning；缺失索引仅 info（多为未初始化的空库）。"""
    if not report.scanned:
        logger.debug("%s", report.summary())
        return

    if report.missing:
        logger.info(
            "索引缺失（通常为该仓储 connect() 未成功；新库首次启动属正常）：%s",
            {k: list(v) for k, v in report.missing.items()},
        )

    if not report.has_drift:
        logger.debug(
            "索引 schema 漂移自检通过：无孤儿索引（集合缺席 %d 个）", len(report.absent))
        return

    lines = [
        "",
        "=" * 70,
        "  索引 schema 漂移告警：库中存在**孤儿索引**（代码不再声明）",
        "  `ensure_index` 只增不删——改过索引键后旧索引会一直留着，并静默改变行为",
        "  （实测教训：sessions 的 30 天 TTL 索引从代码删除后仍在库里，",
        "   若不清掉，30 天未更新的会话会静默过期消失）。",
        "",
    ]
    for collection, names in sorted(report.orphans.items()):
        lines.append(f"    · {collection}: {', '.join(names)}")
    lines += [
        "",
        "  处置：确认该索引确已废弃后显式删除（自检不会自动删——可能是有人故意建的）：",
        "    backend = DocumentDBFactory.create('mongodb/default')",
        "    await backend.drop_index('<collection>', '<index_name>')",
        "=" * 70,
    ]
    logger.warning("\n".join(lines))
