"""索引 schema 漂移自检测试。

两条主线：

1. **派生表不漂移**（`TestExpectedIndexesPinnedToCode`）：`EXPECTED_INDEXES` 是从各仓储
   `connect()` 的 `ensure_index` 调用静态提取的**派生缓存**——本测试用 AST 重新提取并
   逐项断言一致。新增/修改索引却忘记同步该表时，这里会失败并指出差异。
2. **漂移判定语义**（`TestCheckSchemaDrift`）：孤儿索引/缺失索引/集合缺席三态区分，
   以及「未扫描 ≠ 健康」。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.storage.schema_drift import (
    EXPECTED_INDEXES,
    SchemaDriftReport,
    check_schema_drift,
    log_schema_drift_report,
)

_STORAGE_DIR = Path(__file__).resolve().parent.parent
_ID_INDEX = "_id_"


def _derive_expected_from_code() -> dict[str, set[str]]:
    """AST 解析 `core/storage/*.py`，提取 ensure_index 声明（测试用独立实现）。

    与 `schema_drift.py` 的表**独立**——若两者不一致，说明表过期了。

    Raises:
        AssertionError: 出现无法静态解析的索引键（动态表达式）——必须显式失败，
            否则解析器会静默漏掉声明，让表悄然过期。
    """
    derived: dict[str, set[str]] = {}
    unresolved: list[str] = []

    for path in sorted(_STORAGE_DIR.rglob("*.py")):
        rel = str(path)
        if "tests" in rel or path.name.startswith("test_") or path.name == "schema_drift.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        consts: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "ensure_index"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            collection = (
                consts.get(first.id) if isinstance(first, ast.Name)
                else first.value if isinstance(first, ast.Constant)
                else None
            )
            if not isinstance(collection, str):
                continue
            try:
                keys = ast.literal_eval(node.args[1])
            except Exception:
                unresolved.append(f"{path.name}: {ast.unparse(node)[:80]}")
                continue
            name = "_".join(f"{field}_{direction}" for field, direction in keys)
            derived.setdefault(collection, set()).add(name)

    assert not unresolved, (
        "存在无法静态解析的 ensure_index 调用——请改为字面量键，或同步扩展解析器：\n  "
        + "\n  ".join(unresolved)
    )
    return derived


@pytest.mark.unit
class TestExpectedIndexesPinnedToCode:
    """EXPECTED_INDEXES 必须与代码实际声明逐项一致（防止派生表静默过期）。"""

    def test_matches_code_declarations(self):
        derived = _derive_expected_from_code()
        expected = {coll: set(names) for coll, names in EXPECTED_INDEXES.items()}

        missing_in_table = {c: sorted(n - expected.get(c, set())) for c, n in derived.items()
                            if n - expected.get(c, set())}
        extra_in_table = {c: sorted(n - derived.get(c, set())) for c, n in expected.items()
                          if n - derived.get(c, set())}

        assert not missing_in_table, (
            f"代码声明了新索引但 EXPECTED_INDEXES 未同步：{missing_in_table}"
        )
        assert not extra_in_table, (
            f"EXPECTED_INDEXES 有代码已不再声明的索引：{extra_in_table}"
        )

    def test_collection_set_matches(self):
        assert set(EXPECTED_INDEXES) == set(_derive_expected_from_code())

    def test_expected_table_is_non_trivial(self):
        """防止解析器故障导致表被"清空"却测试通过。"""
        total = sum(len(v) for v in EXPECTED_INDEXES.values())
        assert len(EXPECTED_INDEXES) == 12
        # 26 = 24（S3-20 前）+ generations.user_message_id_1_created_at_-1
        #    + character_instances.character_id_1_instance_id_1
        assert total == 26, f"期望 26 个索引，实际 {total}"


@pytest.mark.unit
class TestCheckSchemaDrift:
    """漂移判定语义。"""

    @pytest.mark.asyncio
    async def test_none_backend_is_not_scanned(self):
        """后端为 None → 未扫描，而非「健康」。"""
        report = await check_schema_drift(None)
        assert report.scanned is False
        assert report.has_drift is False
        assert "未执行" in report.summary()

    @pytest.mark.asyncio
    async def test_matching_indexes_no_drift(self):
        """库中索引与声明一致 → 无漂移。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=lambda coll: [
            {"name": _ID_INDEX},
            *[{"name": n} for n in EXPECTED_INDEXES[coll]],
        ])
        report = await check_schema_drift(backend)
        assert report.scanned is True
        assert report.has_drift is False
        assert report.orphans == {}
        assert report.missing == {}

    @pytest.mark.asyncio
    async def test_orphan_index_detected(self):
        """库中有代码不再声明的索引 → 孤儿（危险：会静默改变行为）。"""
        backend = MagicMock()

        def _indexes(coll: str) -> list[dict]:
            items = [{"name": _ID_INDEX}, *[{"name": n} for n in EXPECTED_INDEXES[coll]]]
            if coll == "sessions":
                # 复刻实测案例：代码删掉的 30 天 TTL 索引仍留在库里
                items.append({"name": "updated_at_1", "expireAfterSeconds": 2592000})
            return items

        backend.list_indexes = AsyncMock(side_effect=_indexes)
        report = await check_schema_drift(backend)

        assert report.scanned is True
        assert report.has_drift is True
        assert report.orphans == {"sessions": ("updated_at_1",)}
        assert report.orphan_count == 1
        assert "孤儿索引 1 个" in report.summary()

    @pytest.mark.asyncio
    async def test_missing_index_detected(self):
        """集合存在但声明索引全缺 → missing（多为该仓储 connect() 未跑成功）。

        注意：集合存在时 Mongo 至少有内建 `_id_`，故「只有 _id_」是缺失，
        **不是**集合缺席——这两者必须区分。
        """
        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=lambda coll: [{"name": _ID_INDEX}])
        report = await check_schema_drift(backend)

        assert report.scanned is True
        assert report.has_drift is False  # 缺失不算「漂移」（不阻断），但会被报告
        assert report.missing["sessions"] == ("session_id_1", "updated_at_-1")
        assert report.absent == ()

    @pytest.mark.asyncio
    async def test_absent_collection_not_reported(self):
        """集合不存在（从未写入）→ 不计入孤儿也不计入缺失，避免噪音。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(return_value=[])
        report = await check_schema_drift(backend)

        assert report.scanned is True
        assert report.orphans == {}
        assert report.missing == {}
        assert set(report.absent) == set(EXPECTED_INDEXES)

    @pytest.mark.asyncio
    async def test_id_index_is_ignored(self):
        """内建 `_id_` 不算孤儿。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=lambda coll: [
            {"name": _ID_INDEX},
            *[{"name": n} for n in EXPECTED_INDEXES[coll]],
        ])
        report = await check_schema_drift(backend)
        assert report.orphans == {}

    @pytest.mark.asyncio
    async def test_query_failure_degrades_to_not_scanned(self):
        """查询异常 → 未扫描，绝不抛出（自检不得让启动失败）。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=RuntimeError("mongo down"))
        report = await check_schema_drift(backend)
        assert report.scanned is False


@pytest.mark.unit
class TestLogSchemaDriftReport:
    """日志级别：孤儿索引 warning（需人工处置），无漂移 debug。"""

    def test_orphans_logged_as_warning_with_resolution(self, caplog):
        report = SchemaDriftReport(
            scanned=True, orphans={"sessions": ("updated_at_1",)},
        )
        with caplog.at_level(logging.WARNING, logger="core.storage.schema_drift"):
            log_schema_drift_report(report)

        text = caplog.text
        assert "孤儿" in text
        assert "sessions" in text and "updated_at_1" in text
        # 告警必须给出处置命令（否则运维不知道怎么办）
        assert "drop_index" in text
        # 且必须说明「不会自动删」
        assert "不会自动删" in text

    def test_missing_logged_as_info_not_warning(self, caplog):
        report = SchemaDriftReport(scanned=True, missing={"sessions": ("session_id_1",)})
        with caplog.at_level(logging.INFO, logger="core.storage.schema_drift"):
            log_schema_drift_report(report)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_clean_report_is_debug_only(self, caplog):
        report = SchemaDriftReport(scanned=True)
        with caplog.at_level(logging.DEBUG, logger="core.storage.schema_drift"):
            log_schema_drift_report(report)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert "无孤儿索引" in caplog.text

    def test_unscanned_is_debug_only(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="core.storage.schema_drift"):
            log_schema_drift_report(SchemaDriftReport(scanned=False))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestQueryFailureIsNotAbsence:
    """S2-B1：**"查不到" ≠ "不存在"**。

    原实现把 `list_indexes` 的任何异常都折成 `[]`，而 `check_schema_drift` 把 `[]`
    读成"集合不存在（从未写入）"→ `scanned=True`、无漂移 → 自检报"通过"。
    于是排障时最要紧的一类故障——**索引选项冲突令整个仓储 `_db` 永久为 None**
    （附件全站 503 / 幂等静默降级）——恰好会被自检报告成健康。
    """

    @pytest.mark.asyncio
    async def test_none_from_list_indexes_means_not_scanned(self):
        """`list_indexes` 返回 None（查询失败）→ `scanned=False`，不是"健康"。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(return_value=None)
        report = await check_schema_drift(backend)
        assert report.scanned is False, "查询失败必须如实报告为未完成扫描"
        assert "未执行" in report.summary()

    @pytest.mark.asyncio
    async def test_empty_list_still_means_absent_collection(self):
        """回归网：真正的"集合不存在"（`[]`）仍是 absent，不报噪音。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(return_value=[])
        report = await check_schema_drift(backend)
        assert report.scanned is True
        assert len(report.absent) == len(EXPECTED_INDEXES)
        assert not report.missing and not report.orphans


class TestIndexOptionConflicts:
    """S2-B1：同名索引的**选项**也要比。

    Mongo 自动索引名只含键名+方向，`unique` / `expireAfterSeconds` **不进名字**——
    所以选项冲突在"名字集合"上与健康状态完全一致，只看名字的自检看不见它。
    而库里同名不同选项时 `create_index` 必抛 `OperationFailure`，`ensure_index`
    在 `connect()` 的同一 try 里把它吞成 `return False` → 整个仓储不可用。
    """

    @pytest.mark.asyncio
    async def test_unique_downgrade_detected(self):
        """库里唯一索引被降级为非唯一 → 必须报选项冲突。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=lambda coll: [
            {"name": _ID_INDEX},
            *[
                {"name": n,
                 # 声明 unique 的索引在库里没有 unique → 冲突
                 **({"unique": False} if coll == "characters" and n == "character_id_1" else {})}
                for n in EXPECTED_INDEXES[coll]
            ],
        ])
        report = await check_schema_drift(backend)
        assert "characters" in report.option_conflicts
        assert "character_id_1" in report.option_conflicts["characters"]
        assert "选项冲突" in report.summary()

    @pytest.mark.asyncio
    async def test_ttl_present_matches(self):
        """TTL 只比"有没有"，不比秒数——调大保留期属正常改动，不该报漂移。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=lambda coll: [
            {"name": _ID_INDEX},
            *[
                {"name": n,
                 **({"expireAfterSeconds": 999999} if n.startswith("ttl_") else {}),
                 **({"unique": True} if n == "character_id_1" and coll == "characters" else {}),
                 **({"unique": True} if n == "attachment_id_1" and coll == "attachments" else {}),
                 **({"unique": True} if n == "instance_id_1" and coll == "character_instances" else {}),
                 **({"unique": True} if n == "user_id_1" and coll == "user_identity_profiles" else {}),
                 **({"unique": True} if n == "character_id_1_revision_-1" and coll == "character_revisions" else {}),
                 **({"unique": True} if n == "session_id_1_content_type_1_version_-1" and coll == "character_versions" else {}),
                 }
                for n in EXPECTED_INDEXES[coll]
            ],
        ])
        report = await check_schema_drift(backend)
        assert not report.option_conflicts, report.option_conflicts

    @pytest.mark.asyncio
    async def test_ttl_missing_detected(self):
        """声明了 TTL 但库里没有 → 冲突（记录永不回收）。"""
        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=lambda coll: [
            {"name": _ID_INDEX},
            *[{"name": n} for n in EXPECTED_INDEXES[coll]],
        ])
        report = await check_schema_drift(backend)
        # generations / desktop_calls 声明了 ttl_expires_at_1 的 TTL
        assert "generations" in report.option_conflicts

    @pytest.mark.asyncio
    async def test_option_conflict_logs_error(self, caplog):
        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=lambda coll: [
            {"name": _ID_INDEX},
            *[{"name": n} for n in EXPECTED_INDEXES[coll]],
        ])
        report = await check_schema_drift(backend)
        with caplog.at_level(logging.ERROR):
            log_schema_drift_report(report)
        assert any("选项冲突" in r.getMessage() for r in caplog.records)


# ══════════════════════════════════════════════════════════════════
# S3-20：热路径查询必须有覆盖 filter + sort 的索引
# ══════════════════════════════════════════════════════════════════

class TestHotQueryIndexes:
    """`find_by_user_message` 是**幂等证据探测**的热路径（每次 stale CLAIMED 恢复都走
    一次），filter `user_message_id` + sort `created_at desc`——而两个字段原先都没有
    索引 → **每次探测都是 COLLSCAN**，随档案增长线性劣化。

    这类缺陷 `EXPECTED_INDEXES` 的 AST 钉死天然看不见：它只保证"声明过的索引名与表
    对得上"，**不保证"该声明的索引都声明了"**。所以这里按**查询形态**断言。
    """

    def test_find_by_user_message_has_covering_index(self):
        """幂等证据热路径：filter + sort 两列都要被同一个复合索引覆盖。"""
        from core.storage.schema_drift import EXPECTED_INDEXES

        declared = EXPECTED_INDEXES["generations"]
        assert "user_message_id_1_created_at_-1" in declared, (
            f"find_by_user_message 的 filter(user_message_id) + sort(created_at desc) "
            f"没有覆盖索引——每次幂等恢复都会 COLLSCAN。现有：{sorted(declared)}")

    def test_compound_index_order_matches_query(self):
        """复合索引的**顺序**要匹配查询：等值条件在前、排序键在后。

        反了就用不上（Mongo 只能按索引前缀扫描，排序键在前会让等值过滤退化成扫描）。
        """
        from core.storage.schema_drift import EXPECTED_INDEXES

        name = "user_message_id_1_created_at_-1"
        assert name in EXPECTED_INDEXES["generations"]
        prefix, _, suffix = name.partition("_1_")
        assert prefix == "user_message_id" and suffix == "created_at_-1", (
            f"复合索引顺序应为主要过滤键在前：{name}")

    def test_list_by_character_has_index(self):
        """`list_by_character(character_id)` + sort instance_id 也需要索引。

        既有复合索引前缀是 `owner_user_id`，按 `character_id` 单查**用不上**。
        """
        from core.storage.schema_drift import EXPECTED_INDEXES

        assert "character_id_1_instance_id_1" in EXPECTED_INDEXES["character_instances"]

    def test_admin_listing_is_documented_as_unindexed(self):
        """**有意的例外必须写明理由**——否则下一个人会把"没加索引"当遗漏。

        `CharacterRepository.list_definitions` 是管理/目录路径（四种可选过滤的任意
        组合、由扫描上限限界），不为组合过滤逐一套索引。
        """
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[1] / "character_repo.py").read_text(
            encoding="utf-8")
        idx = src.index("async def list_definitions")
        doc = src[idx:idx + 900]
        assert "S3-20" in doc and "有意不为" in doc, (
            "list_definitions 未加索引这件事必须**在代码里**写明理由")
