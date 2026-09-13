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

#: 索引**选项**声明（S2-B1）：`{collection: {index_name: {option: value}}}`。
#: 只登记"选项有意义"的索引——不在表里的按"不比选项"处理（默认非唯一、无 TTL）。
#: 表由 `core/storage/tests/test_schema_drift.py` 的 AST 反向校验守护（与
#: `EXPECTED_INDEXES` 同一机制），故不会静默过期。
EXPECTED_INDEX_OPTIONS: dict[str, dict[str, dict[str, object]]] = {
    "attachments": {"attachment_id_1": {"unique": True}},
    "character_instances": {"instance_id_1": {"unique": True}},
    "character_revisions": {"character_id_1_revision_-1": {"unique": True}},
    "character_versions": {"session_id_1_content_type_1_version_-1": {"unique": True}},
    "characters": {"character_id_1": {"unique": True}},
    "desktop_calls": {"ttl_expires_at_1": {"expireAfterSeconds": "DESKTOP_CALLS_TTL_SECONDS"}},
    "generations": {"ttl_expires_at_1": {"expireAfterSeconds": "GENERATIONS_TTL_SECONDS"}},
    "idempotency_claims": {"expires_at_1": {"expireAfterSeconds": 0}},
    "user_identity_profiles": {"user_id_1": {"unique": True}},
}


def _option_mismatches(
    collection: str, live: list[dict], expected: dict[str, dict[str, object]],
) -> list[str]:
    """返回**同名但选项不符**的索引名（S2-B1）。

    比对的选项：`unique`（缺省 False）与 `expireAfterSeconds`（缺省 None）。
    TTL 期望值可以写成**常量名**（如 `"GENERATIONS_TTL_SECONDS"`）——只比较
    "库里有 TTL 而代码也声明 TTL" 这种**等价性**，不比较具体秒数：秒数属实现细节，
    比较它会让"调大保留期"这种正常改动被误报成漂移。

    Args:
        collection: 集合名（仅用于日志）。
        live: `list_indexes` 的原始结果。
        expected: 该集合的选项声明。

    Returns:
        选项不符的索引名列表（排序后由调用方入库）。
    """
    mismatched: list[str] = []
    for idx in live:
        name = str(idx.get("name") or "")
        want = expected.get(name)
        if not want:
            continue
        actual_unique = bool(idx.get("unique", False))
        if "unique" in want and actual_unique != bool(want["unique"]):
            mismatched.append(name)
            continue
        if "expireAfterSeconds" in want:
            has_ttl = idx.get("expireAfterSeconds") is not None
            if not has_ttl:
                mismatched.append(name)
    return mismatched


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

    option_conflicts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """同名索引的**选项**与声明不符（`unique` / `expireAfterSeconds`）。

    这一类最危险：Mongo 自动索引名只含键名+方向、选项不进名字，故它与"名字集合"
    完全一致——只看名字的自检**看不见它**。而库里同名不同选项时 `create_index`
    必抛 `OperationFailure`，`ensure_index` 在 `connect()` 的同一 `try` 里把它吞成
    `return False` → **整个仓储 `_db` 永久为 None**（附件全站 503 / 幂等静默降级）。
    """

    absent: tuple[str, ...] = ()
    """库中尚不存在的集合（从未写入）——既不算漂移也不报缺失。"""

    @property
    def has_drift(self) -> bool:
        """是否存在需要人工处置的漂移（**孤儿索引**）。

        注意**不含**选项冲突——两者的处置动作不同（孤儿要删、冲突要在"改代码"
        与"改库"之间决策），调用方需要分别处理，故不合并语义。
        """
        return bool(self.orphans)

    @property
    def orphan_count(self) -> int:
        """孤儿索引总数。"""
        return sum(len(v) for v in self.orphans.values())

    def summary(self) -> str:
        """人类可读摘要。"""
        if not self.scanned:
            return "索引 schema 漂移自检未执行（存储不可用）"
        if not self.has_drift and not self.missing and not self.option_conflicts:
            return f"索引 schema 无漂移（{len(EXPECTED_INDEXES)} 个集合）"
        parts = []
        if self.orphans:
            parts.append(f"孤儿索引 {self.orphan_count} 个")
        if self.missing:
            parts.append(f"缺失索引 {sum(len(v) for v in self.missing.values())} 个")
        if self.option_conflicts:
            parts.append(
                f"**选项冲突** {sum(len(v) for v in self.option_conflicts.values())} 个"
                "（会导致对应仓储 connect() 失败）")
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
    option_conflicts: dict[str, tuple[str, ...]] = {}

    try:
        for collection, expected in EXPECTED_INDEXES.items():
            live = await backend.list_indexes(collection)
            if live is None:
                # S2-B1：**查询失败 ≠ 集合不存在**。原实现把两者都折成 `[]`，
                # 于是"查不到"被读成"从未写入"→ 自检报"通过：无孤儿索引"。
                # 排障时最要紧的一类故障（索引选项冲突令仓储不可用）恰好被
                # 报告成健康。`scanned=False` 是那个"是否真正完成扫描"的区分位。
                logger.warning(
                    "索引 schema 漂移自检未能完成：list_indexes 查询失败 collection=%s",
                    collection)
                return SchemaDriftReport(scanned=False)
            if not live:
                # 空列表 = 集合不存在（从未写入）→ 既非漂移也非缺失，不报噪音。
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
            # S2-B1：同名索引的**选项**也要比。Mongo 自动索引名只含键名+方向，
            # `unique` / `expireAfterSeconds` 不进名字——库里存在同名但选项不同的
            # 索引时 `create_index` 必抛 OperationFailure，而 `ensure_index` 在
            # `connect()` 的同一 try 里，异常被吞成 `return False` → **整个仓储
            # `_db` 永久为 None**（附件全站 503 / 幂等静默降级）。只看名字集合
            # 的自检**看不见这一类**，现按声明补齐比对。
            mismatched = _option_mismatches(collection, live,
                                           EXPECTED_INDEX_OPTIONS.get(collection, {}))
            if mismatched:
                option_conflicts[collection] = tuple(sorted(mismatched))
    except Exception:
        logger.debug("索引 schema 漂移自检未执行（查询失败）", exc_info=True)
        return SchemaDriftReport(scanned=False)

    return SchemaDriftReport(
        scanned=True,
        orphans=orphans,
        option_conflicts=option_conflicts,
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

    if report.option_conflicts:
        conflict_lines = [
            "",
            "=" * 70,
            "  索引**选项冲突**告警：库中存在同名但选项（unique / TTL）不同的索引",
            "  后果：`create_index` 必抛 OperationFailure，而 `ensure_index` 在",
            "  `connect()` 的同一 try 里把它吞成 return False →",
            "  **该仓储 `_db` 永久为 None**（附件全站 503 / 幂等静默降级）。",
            "  这类冲突**名字完全一致**，只看名字的自检看不见它。",
            "",
        ]
        for collection, names in sorted(report.option_conflicts.items()):
            conflict_lines.append(f"    · {collection}: {', '.join(names)}")
        conflict_lines += [
            "",
            "  处置（二选一，**不会自动执行**）：",
            "    ① 让库与代码一致：先清理重复数据，再 drop_index() 后重启（重建为期望选项）",
            "    ② 让代码与库一致：修改代码里的 ensure_index 声明，承认现状",
            "=" * 70,
        ]
        logger.error("\n".join(conflict_lines))

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
