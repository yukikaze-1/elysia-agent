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
        assert total == 24, f"期望 24 个索引，实际 {total}"


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
