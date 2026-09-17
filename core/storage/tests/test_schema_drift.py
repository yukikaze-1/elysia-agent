"""索引 schema 漂移自检测试。

三条主线：

1. **派生表不漂移**（`TestExpectedIndexesPinnedToCode`）：`EXPECTED_INDEXES` 是从各仓储
   `connect()` 的 `ensure_index` 调用静态提取的**派生缓存**——本测试用 AST 重新提取并
   逐项断言一致。新增/修改索引却忘记同步该表时，这里会失败并指出差异。
2. **选项声明表只引用真实实体**（`TestExpectedIndexOptionsPinnedToCode`，N-4）：
   `EXPECTED_INDEX_OPTIONS` 的**集合键与索引名**都必须能在 `ensure_index` 声明里找到，
   且代码声明了选项的索引必须已登记——守卫的判据是**生产代码的 AST**，不是第二张手写表。
3. **漂移判定语义**（`TestCheckSchemaDrift`）：孤儿索引/缺失索引/集合缺席三态区分，
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

#: 会影响"选项是否被比对"的 ensure_index kwargs（与 `_option_mismatches` 比对的选项同集）。
_OPTION_KWARGS = ("unique", "expireAfterSeconds")

#: `_static_option_value` 的"无法静态判定"哨兵（非常量表达式，如 `unique=desired_unique`）。
_DYNAMIC = object()


def _storage_source_paths() -> list[Path]:
    """`core/storage/` 下的**生产**模块（排除测试与 `schema_drift.py` 自身）。"""
    return [
        path for path in sorted(_STORAGE_DIR.rglob("*.py"))
        if "tests" not in str(path)
        and not path.name.startswith("test_")
        and path.name != "schema_drift.py"
    ]


def _module_constants(tree: ast.Module) -> dict[str, object]:
    """模块级字面量常量（`X = "..."` 与 `X: str = "..."` 都收）。"""
    consts: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            value, targets = node.value.value, node.targets
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            value, targets = node.value.value, [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = value
    return consts


def _ensure_index_collection(node: ast.Call, consts: dict[str, object]) -> str | None:
    """从 `ensure_index(collection, keys, ...)` 提取集合名（字面量或模块级常量）。"""
    if not node.args:
        return None
    first = node.args[0]
    collection = (
        consts.get(first.id) if isinstance(first, ast.Name)
        else first.value if isinstance(first, ast.Constant)
        else None
    )
    return collection if isinstance(collection, str) else None


def _index_name_from_keys(keys: list[tuple[str, int]]) -> str:
    """`[(field, direction), ...]` → Mongo 自动索引名（`field_1_other_-1`）。"""
    return "_".join(f"{field}_{direction}" for field, direction in keys)


def _static_option_value(kw: ast.keyword) -> object:
    """把一个选项 kwarg 折叠成静态值；无法静态判定 → `_DYNAMIC`。

    `expireAfterSeconds=<NAME>` 折叠成常量**名字符串**——与 `EXPECTED_INDEX_OPTIONS`
    的登记约定一致（该表只比"有没有 TTL"，不比秒数）。
    """
    try:
        value = ast.literal_eval(kw.value)
    except Exception:
        if kw.arg == "expireAfterSeconds" and isinstance(kw.value, ast.Name):
            return kw.value.id
        return _DYNAMIC
    if kw.arg == "unique" and not isinstance(value, bool):
        return _DYNAMIC
    return value


def _derive_expected_from_code() -> dict[str, set[str]]:
    """AST 解析 `core/storage/*.py`，提取 ensure_index 声明（测试用独立实现）。

    与 `schema_drift.py` 的表**独立**——若两者不一致，说明表过期了。

    Raises:
        AssertionError: 出现无法静态解析的索引键（动态表达式）——必须显式失败，
            否则解析器会静默漏掉声明，让表悄然过期。
    """
    derived: dict[str, set[str]] = {}
    unresolved: list[str] = []

    for path in _storage_source_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_constants(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "ensure_index"):
                continue
            if not node.args:
                continue
            collection = _ensure_index_collection(node, consts)
            if not isinstance(collection, str):
                continue
            try:
                keys = ast.literal_eval(node.args[1])
            except Exception:
                unresolved.append(f"{path.name}: {ast.unparse(node)[:80]}")
                continue
            derived.setdefault(collection, set()).add(_index_name_from_keys(keys))

    assert not unresolved, (
        "存在无法静态解析的 ensure_index 调用——请改为字面量键，或同步扩展解析器：\n  "
        + "\n  ".join(unresolved)
    )
    return derived


def _derive_option_table_from_code() -> tuple[
    dict[str, dict[str, object]], list[str], list[str],
]:
    """AST 提取**带选项**的 ensure_index 声明（`EXPECTED_INDEX_OPTIONS` 的判据来源）。

    与 `_derive_expected_from_code` 的区别：那个只看索引**名字**，本函数看 kwargs。
    `EXPECTED_INDEX_OPTIONS` 的职责是 `{集合: {索引名: {选项: 值}}}`（见该表 docstring），
    故本函数按同一形状派生——判据是**生产代码**，不手写第二张静态表。

    Returns:
        (declared, dynamic, unresolved)

        - `declared`：`{collection: {index_name: {option: value} | _DYNAMIC}}`。
          值为 `_DYNAMIC` 表示该索引确实带选项、但选项值是非常量表达式
          （如 `character_version_repo` 的 `unique=desired_unique`：唯一性由重复探测
          动态决定）。这类**算合法引用**（实体真实存在），但**不参与覆盖断言**
          （静态判不了值，就不该假装判得了）。
        - `dynamic`：`["collection.index: unique=<expr>"]`，供"解析器是否过度折叠"自检。
        - `unresolved`：调用形态无法静态解析（集合名非常量）。非空即**拒答**——
          静默跳过等于给守卫开后门（"判据无从成立"型假绿）。
    """
    declared: dict[str, dict[str, object]] = {}
    dynamic: list[str] = []
    unresolved: list[str] = []

    for path in _storage_source_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_constants(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "ensure_index"):
                continue
            option_kwargs = [kw for kw in node.keywords if kw.arg in _OPTION_KWARGS]
            if not option_kwargs:
                continue
            collection = _ensure_index_collection(node, consts)
            try:
                keys = ast.literal_eval(node.args[1])
            except Exception:
                keys = None
            if not isinstance(collection, str) or keys is None:
                unresolved.append(f"{path.name}: {ast.unparse(node)[:90]}")
                continue
            name = _index_name_from_keys(keys)
            opts: dict[str, object] = {}
            unresolved_values = False
            for kw in option_kwargs:
                value = _static_option_value(kw)
                if value is _DYNAMIC:
                    dynamic.append(f"{collection}.{name}: {kw.arg}={ast.unparse(kw.value)}")
                    unresolved_values = True
                    continue
                if kw.arg == "unique" and value is False:
                    continue  # unique=False 就是默认语义，不登记
                opts[str(kw.arg)] = value
            if opts:
                declared.setdefault(collection, {})[name] = opts
            elif unresolved_values:
                declared.setdefault(collection, {})[name] = _DYNAMIC

    return declared, dynamic, unresolved


def _option_table_reference_errors(
    options: dict[str, dict[str, dict[str, object]]],
    declared: dict[str, dict[str, object]],
) -> list[str]:
    """返回 `options` 里**指向不存在 schema 实体**的条目（空列表 = 全部指向真实集合/索引）。

    判据是**生产代码**（`ensure_index` 声明的 AST），不是另一张静态表：
    两张手写表会**共同漂移**——同名改名、两边一起改错，互相印证为绿。
    """
    errors: list[str] = []
    for collection, indexes in sorted(options.items()):
        if collection not in declared:
            errors.append(
                f"集合 {collection!r} 在任何 ensure_index 声明中都不存在（疑似拼写错误）")
            continue
        for name in sorted(indexes):
            if name not in declared[collection]:
                errors.append(
                    f"索引 {collection}.{name} 不存在（该集合声明的带选项索引："
                    f"{', '.join(sorted(declared[collection])) or '（无）'}）")
    return errors


def _option_table_coverage_errors(
    options: dict[str, dict[str, dict[str, object]]],
    declared: dict[str, dict[str, object]],
    known_unregistered: dict[str, dict[str, str]],
) -> list[str]:
    """返回**代码声明了选项、但表未登记**的索引（空列表 = 覆盖完整）。

    为什么这条也在守卫范围内：键写错与**整条漏登记**在运行期的后果完全一样——
    `_option_mismatches` 用 `expected.get(collection, {})` 取值，两者都让该集合
    "扫了、没比、报健康"。只钉拼写的守卫抓不住后者（把 `idempotency` 整条删掉即复现）。

    `known_unregistered` 是**显式登记**的已知缺口（每条含理由与解除条件）：登记缺口
    而非把它们悄悄排除，是因为"补进表"会扩大生产漂移自检的比对范围（改产品语义），
    超出本批范围。补齐后必须删除对应条目——陈旧登记同样报红。
    """
    errors: list[str] = []
    for collection, indexes in sorted(declared.items()):
        for name in sorted(indexes):
            if declared[collection][name] is _DYNAMIC:
                continue  # 选项值动态 ⇒ 静态判不了，不要求登记（登记了也不算错）
            if name in options.get(collection, {}):
                continue
            if name in known_unregistered.get(collection, {}):
                continue
            errors.append(
                f"代码声明了 {collection}.{name} 的索引选项，但 EXPECTED_INDEX_OPTIONS "
                "未登记——该索引的选项不会被比对")
    return errors


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
        assert len(EXPECTED_INDEXES) == 13
        # 28 = 26（#6 前）+ background_task_results.{task_id_1, ttl_expires_at_1}
        assert total == 28, f"期望 28 个索引，实际 {total}"


#: 代码声明了索引**选项**、但 `EXPECTED_INDEX_OPTIONS` 尚未登记的索引（N-15 记账）。
#:
#: 登记为**已知缺口**而不是补进 `EXPECTED_INDEX_OPTIONS`，是因为补表会扩大生产漂移
#: 自检的比对范围（多比 4 条选项 = 改变产品语义），超出 N-4「补表级守卫」的范围。
#: 每条都要写解除条件；补齐后**必须**删除对应条目——否则
#: `test_unregistered_option_ledger_has_no_stale_entries` 报陈旧登记。
_KNOWN_UNREGISTERED_OPTION_INDEXES: dict[str, dict[str, str]] = {
    "character_instances": {
        "owner_user_id_1_character_id_1_canonical_1":
            "S3-20 新增的 partial 唯一索引（unique=True + partialFilterExpression）。"
            "解除条件：确认线上该索引确为 unique 后登记进 EXPECTED_INDEX_OPTIONS",
    },
    "desktop_calls": {
        "call_id_1":
            "调用方一直去重（call_id 语义唯一），但选项表自 S2-B1 起就没登记它。"
            "解除条件：登记 call_id_1 的 unique 进 EXPECTED_INDEX_OPTIONS",
    },
    "generations": {
        "generation_id_1":
            "同上：generation_id 语义唯一，选项表从未登记。"
            "解除条件：登记 generation_id_1 的 unique 进 EXPECTED_INDEX_OPTIONS",
    },
    "idempotency": {
        "conversation_id_1_client_request_id_1":
            "同上：该唯一索引是幂等键的并发防线，选项表只登记了 expires_at_1 的 TTL。"
            "解除条件：登记该索引的 unique 进 EXPECTED_INDEX_OPTIONS",
    },
}


@pytest.mark.unit
class TestExpectedIndexOptionsPinnedToCode:
    """`EXPECTED_INDEX_OPTIONS` 只许引用真实存在的 schema 实体（N-4 表级结构守卫）。

    ## 守什么

    该表的职责是 `{集合: {索引名: 选项}}`——`_option_mismatches` 用**集合名**取该集合的
    声明、再用**索引名**取该索引的声明。所以只要这两级键有一个不存在，对应的选项就
    **一个都不比**：`check_schema_drift()` 照常"跑完、报健康"，而实际门禁已经失效
    （B3-CB-2：键误写成从未存在的 `idempotency_claims`）。本类把两个层级都钉在
    **生产代码的 AST** 上。

    ## 为什么不是"第二张静态表 vs 第一张静态表"

    判据来自 `_derive_option_table_from_code()`：解析 `core/storage/*.py` 里真实的
    `ensure_index(...)` 调用。两张手写表会**共同漂移**（改名时两边一起改错，互相
    印证为绿）；AST 判据不会——它读的是仓储 `connect()` 真正要建的东西。

    ## 与既有守卫的分工

    `TestIndexOptionConflicts::test_idempotency_ttl_option_is_gauged` 是**运行期**定点
    回归（构造冲突 → 断言它被报出来）；本类只守**静态声明表没有指向不存在的实体**，
    不重复 runtime 漂移逻辑。
    """

    def test_expected_index_options_reference_known_collections(self):
        """表里的每个集合键都必须是真实 `ensure_index` 声明的集合（N-4 主不变量）。"""
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        derived, _dynamic, unresolved = _derive_option_table_from_code()
        assert not unresolved, (
            "存在无法静态解析形态的 ensure_index 调用（集合名非常量）——拒答，"
            "不假装守卫有效：\n  " + "\n  ".join(unresolved))

        errors = _option_table_reference_errors(EXPECTED_INDEX_OPTIONS, derived)
        assert not errors, (
            "EXPECTED_INDEX_OPTIONS 指向了不存在的 schema 实体——这些条目在运行期"
            "**一个选项都不会被比对**（扫了、没比、报健康）：\n  " + "\n  ".join(errors))

    def test_expected_index_options_reference_known_index_names(self):
        """集合对还不够：索引名写错时该索引的选项同样不会被比对。"""
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        derived, _dynamic, _unresolved = _derive_option_table_from_code()
        for collection, indexes in EXPECTED_INDEX_OPTIONS.items():
            for name in indexes:
                assert name in derived.get(collection, {}), (
                    f"索引名 {collection}.{name} 不在该集合的 ensure_index 声明里——"
                    f"实际声明：{sorted(derived.get(collection, {}))}")

    def test_expected_index_options_cover_code_declared_options(self):
        """反 vacuity：代码里**带选项**的索引必须已登记（漏登记 = 同一类假绿）。

        键写错与整条漏登记在运行期后果完全一样（该集合一个选项都不比），只钉拼写的
        守卫抓不住后者。已知未登记项见 `_KNOWN_UNREGISTERED_OPTION_INDEXES`。
        """
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        derived, dynamic, _unresolved = _derive_option_table_from_code()
        assert dynamic, (
            "解析器把**所有**动态选项都折叠成了静态值——判据太宽，先检查解析器")
        errors = _option_table_coverage_errors(
            EXPECTED_INDEX_OPTIONS, derived, _KNOWN_UNREGISTERED_OPTION_INDEXES)
        assert not errors, (
            "代码声明了选项但 EXPECTED_INDEX_OPTIONS 未登记（扫了、没比、报健康）：\n  "
            + "\n  ".join(errors))

    def test_unregistered_option_ledger_has_no_stale_entries(self):
        """已知缺口登记表不得陈旧：补进表之后必须删掉对应条目。"""
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        derived, _dynamic, _unresolved = _derive_option_table_from_code()
        stale: list[str] = []
        for collection, entries in _KNOWN_UNREGISTERED_OPTION_INDEXES.items():
            for name, reason in entries.items():
                if name in EXPECTED_INDEX_OPTIONS.get(collection, {}):
                    stale.append(f"{collection}.{name}（已登记进表，请删除本条目）")
                elif name not in derived.get(collection, {}):
                    stale.append(f"{collection}.{name}（代码里已无此带选项声明，请删除本条目）")
                elif not reason.strip():
                    stale.append(f"{collection}.{name}（缺理由/解除条件）")
        assert not stale, "已知缺口登记表已陈旧：\n  " + "\n  ".join(stale)

    # ── 判据灵敏度（fake-green 防护）：光"当前表绿"不构成守卫证据 ──────────────

    def test_guard_flags_historical_typo_collection_key(self):
        """变异 M4-1：键退回历史拼写 `idempotency_claims` → 必须被抓出来。

        这条证明 B3-CB-2 的形态若重新出现，会被**自动**挡住（不需要人记得）。
        """
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        derived, _dynamic, _unresolved = _derive_option_table_from_code()
        mutated = {
            ("idempotency_claims" if coll == "idempotency" else coll): idxs
            for coll, idxs in EXPECTED_INDEX_OPTIONS.items()
        }
        errors = _option_table_reference_errors(mutated, derived)
        assert any("idempotency_claims" in e for e in errors), (
            f"历史拼写未被判据抓住：{errors}")

    def test_guard_flags_arbitrary_unknown_collection_key(self):
        """变异：任意不存在的集合键（不必是历史拼写）→ 必须红。

        防"只针对 `idempotency_claims` 这一个字符串硬编码"的假守卫。
        """
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        derived, _dynamic, _unresolved = _derive_option_table_from_code()
        for bogus in ("definitely_not_a_collection", "idempotency_claims", "sessions_typo"):
            mutated = dict(EXPECTED_INDEX_OPTIONS)
            mutated[bogus] = {"whatever_1": {"unique": True}}
            errors = _option_table_reference_errors(mutated, derived)
            assert any(bogus in e for e in errors), f"{bogus} 未被判据抓住：{errors}"

    def test_guard_flags_unknown_index_name(self):
        """索引名写错（集合对但索引不存在）→ 必须红。"""
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        derived, _dynamic, _unresolved = _derive_option_table_from_code()
        mutated = dict(EXPECTED_INDEX_OPTIONS)
        mutated["idempotency"] = {"expires_at_1_typo": {"expireAfterSeconds": 0}}
        errors = _option_table_reference_errors(mutated, derived)
        assert any("expires_at_1_typo" in e for e in errors), f"索引名错拼未被抓住：{errors}"

    def test_guard_flags_deleted_and_emptied_table(self):
        """删掉整条 / 清空全表 → 必须红（这两种形态复现**同一个**运行期缺陷）。

        `_option_mismatches` 拿不到声明即不比选项——与键写错完全等价。缺了这条，
        守卫只钉住了拼写，没钉住"门禁是否真的在比"。
        """
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        derived, _dynamic, _unresolved = _derive_option_table_from_code()

        deleted = {c: v for c, v in EXPECTED_INDEX_OPTIONS.items() if c != "idempotency"}
        errors = _option_table_coverage_errors(
            deleted, derived, _KNOWN_UNREGISTERED_OPTION_INDEXES)
        assert any("idempotency" in e for e in errors), f"整条删除未被抓住：{errors}"

        errors = _option_table_coverage_errors({}, derived, _KNOWN_UNREGISTERED_OPTION_INDEXES)
        assert errors, "空表被判为合规——守卫退化成 vacuous"

    def test_parser_survives_reformatting_and_reordering(self):
        """AST 判据不得依赖空格/换行/参数顺序（格式化后仍稳定）。

        判据若是"抄一份同名静态表"，格式化当然也不影响它——但那种守卫会共同漂移。
        这条只证明 AST 路线对格式**免疫**（区别于行号/正则匹配）。
        """
        pretty = '''
COLLECTION = "probe"

async def connect(db):
    await db.ensure_index(
        COLLECTION,
        [
            ("alpha", 1),
            ("beta", -1),
        ],
        expireAfterSeconds=PROBE_TTL_SECONDS,
    )
    await db.ensure_index(COLLECTION, [("gamma", 1)], unique=True)
'''
        tree = ast.parse(pretty)
        consts = _module_constants(tree)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ensure_index"
        ]
        assert len(calls) == 2
        assert _ensure_index_collection(calls[0], consts) == "probe"
        assert _index_name_from_keys(ast.literal_eval(calls[0].args[1])) == "alpha_1_beta_-1"
        assert _static_option_value(calls[0].keywords[0]) == "PROBE_TTL_SECONDS"
        assert _static_option_value(calls[1].keywords[0]) is True


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


@pytest.mark.service
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


@pytest.mark.service
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
        """TTL 只比"有没有"，不比秒数——调大保留期属正常改动，不该报漂移。

        模拟的**落库形态从声明派生**（`EXPECTED_INDEX_OPTIONS`）：声明为 TTL 的索引
        就给一个 TTL。此前这里靠 `n.startswith("ttl_")` 判断，而
        `idempotency.expires_at_1` **不以 `ttl_` 开头**——于是修正 B3-CB-2 的键之后
        （该集合从此真的被比对），这条用例会因为 mock 自己没给 TTL 而变红：
        测试与被测对象各持一份真相。派生式写法则对未来新增的 TTL 索引自动成立。
        """
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=lambda coll: [
            {"name": _ID_INDEX},
            *[
                {"name": n,
                 # 声明了 TTL 的索引，库里就有 TTL（秒数刻意与声明不同：
                 # 只比"有没有"，不比秒数）
                 **({"expireAfterSeconds": 999999}
                    if "expireAfterSeconds"
                    in EXPECTED_INDEX_OPTIONS.get(coll, {}).get(n, {}) else {}),
                 **({"unique": True} if n == "character_id_1" and coll == "characters" else {}),
                 **({"unique": True} if n == "attachment_id_1" and coll == "attachments" else {}),
                 **({"unique": True} if n == "instance_id_1" and coll == "character_instances" else {}),
                 **({"unique": True} if n == "user_id_1" and coll == "user_identity_profiles" else {}),
                 **({"unique": True} if n == "character_id_1_revision_-1" and coll == "character_revisions" else {}),
                 **({"unique": True} if n == "session_id_1_content_type_1_version_-1" and coll == "character_versions" else {}),
                 **({"unique": True} if n == "task_id_1" and coll == "background_task_results" else {}),
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

    @pytest.mark.asyncio
    async def test_idempotency_ttl_option_is_gauged(self):
        """`idempotency.expires_at_1` 的 TTL 选项必须真的被比对（B3-CB-2）。

        ## 这条用例钉的是什么

        `EXPECTED_INDEX_OPTIONS` 的键曾写成**从未存在**的 `idempotency_claims`
        （真集合名是 `idempotency`，见 `idempotency_repo.COLLECTION`）。后果不是
        "少报一条"那么轻：`_option_mismatches` 用 `expected.get(collection, {})`
        取值，键不存在 ⇒ 该集合**一个选项都不比**，而它的索引名照样出现在
        `EXPECTED_INDEXES` 里被扫描 —— 即"扫了、没比、报健康"。这正是 S2-B1 要
        防的那类假保障（`option_conflicts` 的 ERROR 级解释与处置指引也一并失效）。

        ## 为什么用派生式 mock 而不是照抄 `ttl_` 前缀特例

        本文件既有的 fixture 靠 `n.startswith("ttl_")` 判断"库里这份索引带 TTL"，
        而 `expires_at_1` **不以 `ttl_` 开头**。若新 fixture 照抄该特例，修好键之后
        这条用例会因为 mock 自己没给 TTL 而变红——测试与被测对象各持一份真相。
        故这里从 `EXPECTED_INDEX_OPTIONS`（**声明本身**）派生 mock 的落库形态：
        声明有 TTL 的就给 TTL，唯独把 `expires_at_1` 的 TTL 摘掉来制造冲突。

        变异验证：把 `schema_drift.py` 的键退回 `idempotency_claims` → 本用例红
        （无法构造出冲突，`option_conflicts` 为空）；改回 `idempotency` → 绿。
        """
        from core.storage.schema_drift import EXPECTED_INDEX_OPTIONS

        declared_ttl = EXPECTED_INDEX_OPTIONS["idempotency"]["expires_at_1"]
        assert "expireAfterSeconds" in declared_ttl, (
            "本用例的前提是 idempotency.expires_at_1 被声明为 TTL 索引")

        def _indexes(coll: str) -> list[dict]:
            out = [{"name": _ID_INDEX}]
            for name in EXPECTED_INDEXES[coll]:
                want = EXPECTED_INDEX_OPTIONS.get(coll, {}).get(name, {})
                idx: dict = {"name": name}
                if want.get("unique"):
                    idx["unique"] = True
                # 只在 idempotency 的 expires_at_1 上省略 TTL —— 制造"同名不同选项"
                if "expireAfterSeconds" in want and name != "expires_at_1":
                    idx["expireAfterSeconds"] = 999999
                out.append(idx)
            return out

        backend = MagicMock()
        backend.list_indexes = AsyncMock(side_effect=_indexes)
        report = await check_schema_drift(backend)

        assert "idempotency" in report.option_conflicts, (
            "idempotency 的索引选项未被比对——EXPECTED_INDEX_OPTIONS 的键与"
            f"真实集合名不符。实际 option_conflicts={dict(report.option_conflicts)}")
        assert "expires_at_1" in report.option_conflicts["idempotency"]
        assert "选项冲突" in report.summary()


# ══════════════════════════════════════════════════════════════════
# S3-20：热路径查询必须有覆盖 filter + sort 的索引
# ══════════════════════════════════════════════════════════════════

@pytest.mark.unit
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
