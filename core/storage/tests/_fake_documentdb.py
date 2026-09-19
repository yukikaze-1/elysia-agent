"""内存版 DocumentDBBackend —— storage 仓库测试用假后端。

模拟 MongoDB 语义的 subset（$lt/$gt/$elemMatch/$regex/$or/$and、sort、skip、limit、
$set/$setOnInsert/$inc/$addToSet/$pull + upsert），_id 用 bson.ObjectId 生成、
find 返回时转 str（对齐真实后端契约）。仅用于单测，不涉及真实数据库。

## modified_count 语义（对齐真后端）

`update_one` 返回**真实 modified_count**，而非「命中即 1」：

- 命中且某字段值**实际变化** → 1
- 命中但 `$set` 写入值与现值相同（no-op）→ 0
- 未命中（且非 upsert）→ 0
- upsert **插入**路径 → 0（真 Mongo 的 upsert-insert 同样 modified_count=0）

真实现 `Module/database/documentdb/mongodb.py` 返回 `result.modified_count`。
Mongo 在「filter 命中但写入值未变」时返回 0——`cas_revision_pointer` /
`cas_state_version` 等 CAS 判定依赖此语义，且 `cas_*` 靠 `$set` 恒定含 `updated_at`
（必变字段）规避 no-op。若有人删掉该必变字段，本 fake 现在会暴露该回归（旧 fake 恒返 1，
会让测试在真 Mongo 下 CAS 被击穿时仍然绿）。

## 唯一索引（对齐真后端）

`ensure_index(..., unique=True)` 会被**记录并在写入路径强制**：`insert_one` /
`insert_many` / `update_one` 的 upsert-insert 路径（以及 `$set` 修改索引键的更新）
若与既有文档唯一键冲突，抛 `pymongo.errors.DuplicateKeyError`（类名与真 driver 一致，
repo 层按类名匹配）。`partialFilterExpression` 也支持（仅对满足过滤子句的文档生效）。

注意：只有在**测试真的声明了索引**时才会强制——这与真 Mongo 一致（无索引即无约束）。
**批次 16 补强**：`ensure_index(unique=True)` 现在会校验**既有文档**是否已违反该键
（冲突抛 `DuplicateKeyError`，对齐真库 build 期行为），因为 `aggregate()` 是 stub
（恒返回 `[]`）⇒"用 fake 验证『库里有重复 ⇒ 唯一索引建不起来/需降级』"若不补这一条，
会得到**假绿**（fake 报 `unique=True`，真库会 code 11000 降级）。
若干生产 repo 的 `connect()` 在**注入 db**（测试 fake）时 early-return，不声明索引
（character/instance/generation/desktop/attachment 等），因此这些 repo 的既有测试
默认不受唯一约束影响；需要验证唯一性的测试应显式 `await db.ensure_index(...)`
（或走会声明索引的 connect，如 idempotency repo）后再操作。

## 已知局限（docstring 明示，不假装已实现）

1. **TTL 不默认强制**。真 Mongo 的 TTL 监控线程每 ~60s 跑一次、best-effort 删除
   `ttl_expires_at`/`expires_at` < now 的文档。若 fake 按当前时刻**急切**删过期文档，
   会与真后端产生**反向**偏差：现有测试用 `expires_at = datetime.now()`（过期即
   「已到期」）立即读回，真 Mongo 在监控窗口内仍能读到，急切删除则读不到 →
   造成假红。故默认 `enforce_ttl=False`（不删），并提供 `sweep_expired()` 供
   需要显式验证 TTL 逻辑的测试主动调用。TTL 索引声明仍会被记录（`ttl_indexes`）。
2. `aggregate` 为 stub（返回 []），`$unset`/`$rename`/`$push` 等更新操作符未实现。
3. 查询操作符仅覆盖 subset（见模块顶说明）；未知操作符被忽略而非报错。
   **批次 16 补强**：`$not`（缺失/null 也命中）、`$nor`（顶层）、`$type`（**含数组
   元素遍历**）已实现，比较算子按 **BSON 类型括号**判据（跨类型不匹配，不再抛
   `TypeError`）——这些是 KB-15 的桶查询（有序 `{"$type":"string","$gt":"",
   "$not":{"$type":"array"}}` ⊕ legacy `{"$nor":[有序]}`）能在 fake 上被**真正执行**
   的前提；否则未知算子会被静默忽略成 match-all（假绿）。
   仍未实现：`$nin` / `$all` / `$size` / `$expr` / `$where`（同样是静默忽略，
   新增查询算子前必须先在这里实现）。
   `$type: "null"` 对**缺失字段**与真库不一致（fake 的 `_get_field` 把缺失与显式
   null 都返回 None ⇒ 缺失也会命中 `null`）；本批的桶判据只用到 `"string"`/`"array"`，
   不受影响。）
4. `$exists` 对**显式 null** 与真库相反（fake 把缺失与 null 都当"不存在"）——
   既有缺口，本批未动（无生产查询依赖它区分这两者）。
"""

import copy
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from Module.database.documentdb import DocumentDBBackend

try:  # 真 driver 异常（类名供 repo 层 `_is_duplicate_key` 匹配）
    from pymongo.errors import DuplicateKeyError
except Exception:  # pragma: no cover - 环境无 pymongo 时的等义兜底（按类名匹配）
    class DuplicateKeyError(RuntimeError):  # type: ignore[no-redef]
        """等义于 pymongo.errors.DuplicateKeyError 的兜底异常。"""


def _get_field(doc: dict, key: str) -> Any:
    """取字段值，支持点路径；缺失返回 None。"""
    if key in doc:
        return doc[key]
    cur: Any = doc
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _bson_type_rank(value: Any) -> int:
    """BSON 类型序（Mongo 比较算子与排序的**类型括号**判据）。

    真 Mongo 的比较算子只与**同类型**操作数比较（numeric 类型之间互通），跨类型
    一律不匹配——修复前 fake 直接 `field_val > arg`，混类型数据集会抛 `TypeError`
    或给出真库不会给的结果（批次 16 / KB-15 的 `updated_at` 正是混类型数据集）。

    这里只实现判据需要的粗分类（rank 值本身无意义，只用于"是否同类型"）。
    """
    if value is None:
        return 0
    if isinstance(value, bool):     # 注意：bool 必须先于 int 判断（bool 是 int 子类）
        return 1
    if isinstance(value, (int, float)):
        return 2
    if isinstance(value, str):
        return 3
    if isinstance(value, datetime):
        return 4
    if isinstance(value, ObjectId):
        return 5
    if isinstance(value, dict):
        return 6
    if isinstance(value, list):
        return 7
    return 8


def _comparable(a: Any, b: Any) -> bool:
    """两个值是否可比较（同 BSON 类型；None 只与 None 同类）。"""
    return _bson_type_rank(a) == _bson_type_rank(b)


#: `$type` 的 BSON 类型名 → 判据（真库语义：
#: **数组字段按元素遍历**——`{f: {$type: "string"}}` 对 `f: ["a"]` 同样命中）。
_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "double": (float,),
    "int": (int,),
    "long": (int,),
    "number": (int, float),
    "decimal": (float,),
    "string": (str,),
    "object": (dict,),
    "array": (list,),
    "bool": (bool,),
    "date": (datetime,),
    "objectId": (ObjectId,),
    "null": (type(None),),
}


def _type_matches(field_val: Any, type_name: str) -> bool:
    """单个值是否是该 BSON 类型（`bool` 必须先于 `int` 判断）。"""
    if type_name == "bool":
        return isinstance(field_val, bool)
    if type_name in ("int", "long", "number", "double", "decimal"):
        return isinstance(field_val, (int, float)) and not isinstance(field_val, bool)
    expected = _TYPE_CHECKS.get(type_name)
    if expected is None:
        return False
    return isinstance(field_val, expected)


def _field_type_matches(field_val: Any, arg: Any) -> bool:
    """`$type`（含数组元素遍历；arg 可以是类型名或类型名列表）。"""
    names = arg if isinstance(arg, list) else [arg]
    if any(_type_matches(field_val, str(n)) for n in names):
        return True
    return isinstance(field_val, list) and any(
        _type_matches(el, str(n)) for el in field_val for n in names)


def _field_matches(field_val: Any, expected: Any) -> bool:
    """字段值 vs 查询条件（标量或操作符 dict）。"""
    if isinstance(expected, dict) and any(str(k).startswith("$") for k in expected):
        for op, arg in expected.items():
            if op == "$options":
                continue
            if op in ("$lt", "$lte", "$gt", "$gte"):
                if not _comparable(field_val, arg):
                    return False
                import operator
                cmp_op = {"$lt": operator.lt, "$lte": operator.le,
                          "$gt": operator.gt, "$gte": operator.ge}[op]
                if field_val is None:      # null vs null：相等（`$lte`/`$gte` 命中）
                    if op in ("$lt", "$gt"):
                        return False
                elif not cmp_op(field_val, arg):
                    return False
            elif op == "$ne":
                # 数组字段按 Mongo 语义：无任何元素等于 arg 才算 $ne 命中
                if isinstance(field_val, list):
                    if arg in field_val:
                        return False
                elif field_val == arg:
                    return False
            elif op == "$type":
                # 真库语义：数组字段**按元素遍历**（`{f: {$type: "string"}}` 命中
                # `f: ["a"]`）。批次 16 的桶判据依赖这一条来排除数组型 `updated_at`。
                if not _field_type_matches(field_val, arg):
                    return False
            elif op == "$not":
                # Mongo `$not`：字段**不满足**子表达式即命中——**缺失/null 同样命中**
                # （这正是 KB-15 legacy 桶判据依赖的语义；未知操作符被忽略的实现
                # 会把 `$not` 变成 match-all，那是假绿）。
                if _field_matches(field_val, arg):
                    return False
            elif op == "$exists":
                if bool(arg) != (field_val is not None):
                    return False
            elif op == "$regex":
                import re
                flags = re.IGNORECASE if "i" in expected.get("$options", "") else 0
                if not isinstance(field_val, str) or re.search(arg, field_val, flags) is None:
                    return False
            elif op == "$in":
                if field_val not in arg:
                    return False
            elif op == "$elemMatch":
                if not any(_matches(el, arg) for el in (field_val or [])):
                    return False
            # 未知操作符：忽略（demo subset）——**这是已知保真度缺口**：
            # `$type`/`$nin`/`$all`/`$size` 会被静默忽略成 match-all。新增查询算子前
            # 先在这里实现，别让假绿进测试（批次 16 §115/Fidelity Gap 登记）。
        return True
    # 标量等值：数组字段按 Mongo 语义做元素包含匹配（{field: value} 命中含 value 的数组）
    if isinstance(field_val, list):
        if isinstance(expected, list):
            return field_val == expected
        return expected in field_val
    return field_val == expected


def _index_key(value: Any) -> Any:
    """索引键形态（数组字段在真库里是**多键**——展开成排序后的元组做等价比较）。"""
    return tuple(value) if isinstance(value, list) else value


def _matches(doc: dict, cond: dict | None) -> bool:
    """整篇文档是否匹配过滤条件。"""
    if not cond:
        return True
    for key, value in cond.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in value):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, sub) for sub in value):
                return False
            continue
        if key == "$nor":
            # 真库语义：**没有一条**子条件命中才算命中（含字段缺失）。
            if any(_matches(doc, sub) for sub in value):
                return False
            continue
        if not _field_matches(_get_field(doc, key), value):
            return False
    return True


def _to_str_id(doc: dict) -> dict:
    """返回 _id 转 str 的文档副本（对齐真实后端契约）。"""
    out = dict(doc)
    if isinstance(out.get("_id"), ObjectId):
        out["_id"] = str(out["_id"])
    return out


def _apply_update_ops(doc: dict, update: dict, *, is_insert: bool) -> None:
    """把 Mongo 更新操作符应用到 doc（原地修改）。

    Args:
        doc: 目标文档（原地修改）。
        update: 更新操作符文档（$set/$setOnInsert/$inc/$addToSet/$pull）。
        is_insert: True 表示这是 upsert 插入路径（$setOnInsert 生效）；False 表示
            命中已有文档（$setOnInsert 被忽略）。
    """
    if "$set" in update:
        doc.update(update["$set"])
    # $setOnInsert 仅在插入时生效——命中已有文档时不修改任何字段
    # （ensure_exists 靠此保证不覆盖既有 messages/title）
    if is_insert and "$setOnInsert" in update:
        doc.update(update["$setOnInsert"])
    if "$inc" in update:
        for key, value in update["$inc"].items():
            doc[key] = (doc.get(key) or 0) + value
    if "$addToSet" in update:
        for key, value in update["$addToSet"].items():
            cur = doc.get(key) or []
            if value not in cur:
                cur.append(value)
            doc[key] = cur
    if "$pull" in update:
        for key, value in update["$pull"].items():
            cur = doc.get(key) or []
            doc[key] = [item for item in cur if item != value]


class FakeDocumentDBBackend(DocumentDBBackend):
    """内存版文档数据库后端。"""

    def __init__(self, *, enforce_ttl: bool = False) -> None:
        """初始化。

        Args:
            enforce_ttl: 是否在每次读写前急切清除已过期文档。默认 False——
                真 Mongo 的 TTL 监控是 ~60s 的异步 best-effort，急切清除会产生
                反向偏差（详见模块 docstring「已知局限」）。需要显式验证 TTL 的
                测试请主动调用 `sweep_expired()`。
        """
        self.collections: dict[str, list[dict]] = {}
        # P1-05：可配置的连通性状态——False 模拟断网（health_check 必须感知）
        self.ping_ok: bool = True
        self._enforce_ttl = enforce_ttl
        # 已声明的索引：collection → [spec]；spec 含 keys/unique/partial/ttl 字段
        self.indexes: dict[str, list[dict]] = {}

    async def ping(self) -> bool:
        """真实健康探测模拟：返回 ping_ok（断网 = False）。"""
        return self.ping_ok

    def _coll(self, collection: str) -> list[dict]:
        return self.collections.setdefault(collection, [])

    # ---- index ----

    async def ensure_index(self, collection: str, keys: list[tuple[str, int]],
                           **kwargs: Any) -> str:
        """记录索引声明（唯一索引会在写入路径强制；TTL 索引仅记录）。

        Args:
            collection: 目标集合。
            keys: 索引键列表（如 [("character_id", 1)]）。
            **kwargs: `unique` / `partialFilterExpression` / `name` /
                `expireAfterSeconds` 等真 create_index 参数。

        Returns:
            索引名（沿用真 driver 的 `<field>_<dir>` 拼接约定）。
        """
        name = kwargs.get("name") or "_".join(f"{k}_{d}" for k, d in keys)
        unique = bool(kwargs.get("unique", False))
        partial = kwargs.get("partialFilterExpression")
        spec = {
            "name": name,
            "keys": list(keys),
            "unique": unique,
            "partial": partial,
            # TTL：expireAfterSeconds 显式给出（含 0）才算 TTL 索引
            "ttl": kwargs.get("expireAfterSeconds"),
        }
        existing = [s for s in self.indexes.get(collection, []) if s["name"] == name]
        if existing:
            # 同名同选项 → 幂等 no-op；同名**不同选项** → 真库抛
            # `OperationFailure`(86 IndexKeySpecsConflict)。fake 不模拟完整选项
            # 比对（`sparse` 等未建模），但至少让"unique 变了"这条可见。
            if bool(existing[0].get("unique", False)) != unique:
                raise RuntimeError(
                    "IndexKeySpecsConflict: 同名索引已存在且选项不同 "
                    f"({collection}.{name}: unique {existing[0].get('unique')} → {unique})")
            return name
        if unique:
            # **已有的重复文档**会让真库的 unique build 失败（`DuplicateKeyError`
            # 11000），且失败后索引不存在。fake 必须同样报错，否则"用 fake 验证
            # 重复分支"会得到假绿（真库会降级为非唯一、fake 会报 unique=True）。
            self._assert_buildable_unique(collection, keys, partial)
        specs = self.indexes.setdefault(collection, [])
        specs.append(spec)
        return name

    def _assert_buildable_unique(self, collection: str, keys: list[tuple[str, int]],
                                 partial: dict | None) -> None:
        """校验既有文档满足该唯一键（对齐真库的 build 期 `DuplicateKeyError`）。"""
        seen: set[tuple] = set()
        for doc in self._coll(collection):
            if partial is not None and not _matches(doc, partial):
                continue
            key = tuple(_index_key(_get_field(doc, field)) for field, _ in keys)
            if key in seen:
                raise DuplicateKeyError(
                    f"E11000 duplicate key error index build ({collection}, {keys})", 11000,
                    {"keyPattern": {field: direction for field, direction in keys}})
            seen.add(key)

    async def list_indexes(self, collection: str) -> list[dict]:
        """列出已声明索引（含内建 _id），与真 driver 的返回形状对齐。

        schema 漂移自检在 fake 上也走同一条代码路径，因此这里必须返回
        与 `list_indexes()` 真实现同构的 dict（至少含 name/key）。
        """
        out = [{"name": "_id_", "key": [("_id", 1)], "unique": True}]
        for spec in self.indexes.get(collection, []):
            out.append({
                "name": spec["name"],
                "key": list(spec["keys"]),
                "unique": spec["unique"],
                "expireAfterSeconds": spec["ttl"],
            })
        return out

    async def drop_index(self, collection: str, name: str) -> bool:
        """删除已声明索引；不存在返回 False（幂等）。`_id_` 不可删。"""
        if name == "_id_":
            return False
        specs = self.indexes.get(collection, [])
        before = len(specs)
        self.indexes[collection] = [s for s in specs if s["name"] != name]
        return len(self.indexes[collection]) < before

    @property
    def unique_indexes(self) -> dict[str, list[dict]]:
        """已声明的唯一索引（collection → specs），供测试断言。"""
        return {
            coll: [s for s in specs if s["unique"]]
            for coll, specs in self.indexes.items()
        }

    @property
    def ttl_indexes(self) -> dict[str, list[dict]]:
        """已声明的 TTL 索引（collection → specs），供测试断言。"""
        return {
            coll: [s for s in specs if s["ttl"] is not None]
            for coll, specs in self.indexes.items()
        }

    def _assert_unique(self, collection: str, candidate: dict,
                       *, exclude: dict | None = None) -> None:
        """校验 candidate 是否违反任一唯一索引（_id 恒唯一 + 已声明唯一索引）。

        Args:
            collection: 目标集合。
            candidate: 待写入/更新后的完整文档。
            exclude: 更新场景下的原文档（自身不算冲突）。

        Raises:
            DuplicateKeyError: 与既有文档唯一键冲突（真 Mongo E11000）。
        """
        coll = self._coll(collection)
        others = [d for d in coll if d is not exclude]

        # _id 恒为唯一索引（无需显式声明）
        cand_id = candidate.get("_id")
        if cand_id is not None:
            for d in others:
                if d.get("_id") is not None and d.get("_id") == cand_id:
                    raise DuplicateKeyError(
                        f"E11000 duplicate key error collection: {collection} "
                        f"index: _id_ dup key: {{ _id: {cand_id!r} }}")

        for spec in self.indexes.get(collection, []):
            if not spec["unique"]:
                continue
            partial = spec["partial"]
            if partial is not None and not _matches(candidate, partial):
                continue  # 不满足 partialFilterExpression → 不入索引
            cand_key = tuple(_get_field(candidate, k) for k, _ in spec["keys"])
            for d in others:
                if partial is not None and not _matches(d, partial):
                    continue
                other_key = tuple(_get_field(d, k) for k, _ in spec["keys"])
                if other_key == cand_key:
                    raise DuplicateKeyError(
                        f"E11000 duplicate key error collection: {collection} "
                        f"index: {spec['name']} dup key: "
                        + str(dict(zip([k for k, _ in spec["keys"]], cand_key, strict=True))))

    # ---- write ----

    async def insert_one(self, collection: str, document: dict) -> str:
        doc = dict(document)
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        self._assert_unique(collection, doc)
        self._coll(collection).append(doc)
        return str(doc["_id"])

    async def insert_many(self, collection: str, documents: list[dict]) -> list[str]:
        # ordered=True 语义（真默认）：顺序插入，首个冲突即抛，之前的文档保留
        return [await self.insert_one(collection, d) for d in documents]

    async def find(self, collection: str, filter_expr: dict, *,
                   sort: list[tuple[str, int]] | None = None,
                   limit: int = 100, skip: int = 0) -> list[dict]:
        if self._enforce_ttl:
            self.sweep_expired()
        docs = [d for d in self._coll(collection) if _matches(d, filter_expr)]
        if sort:
            for field, direction in reversed(sort):
                docs.sort(key=lambda d, f=field: _get_field(d, f) if _get_field(d, f) is not None else "",
                          reverse=(direction == -1))
        return [_to_str_id(d) for d in docs[skip:skip + limit]]

    async def update_one(self, collection: str, filter_expr: dict,
                         update: dict, upsert: bool = False) -> int:
        coll = self._coll(collection)
        # 注意：这里不插入 await 交错点——共享 fake 被所有用 DocumentDBBackend 的
        # 测试使用，全局 sleep(0) 会扰动无关异步测试的时序（批次 8a Task 13 曾加过，
        # 导致 test_v2_interrupt 读到旧状态；已移除）。并发竞态由调用方测试断言
        # update 调用形态（原子条件 update）验证，不依赖 fake 时序复现。
        matched = [d for d in coll if _matches(d, filter_expr)]
        if matched:
            doc = matched[0]
            updated = copy.deepcopy(doc)
            _apply_update_ops(updated, update, is_insert=False)
            self._assert_unique(collection, updated, exclude=doc)
            # 真实 modified_count：仅在文档确有变化时计 1（no-op $set → 0）
            if updated == doc:
                return 0
            doc.clear()
            doc.update(updated)
            return 1
        if upsert:
            doc: dict = {}
            _apply_update_ops(doc, update, is_insert=True)
            # upsert 时把过滤条件里的等值字段并入新文档（对齐 Mongo 语义）
            for k, v in filter_expr.items():
                if k not in doc and not isinstance(v, dict):
                    doc[k] = v
            doc["_id"] = ObjectId()
            self._assert_unique(collection, doc)
            coll.append(doc)
            # 真 Mongo 的 upsert-insert：matched_count=0、modified_count=0
            return 0
        return 0

    async def delete_many(self, collection: str, filter_expr: dict) -> int:
        coll = self._coll(collection)
        remaining = [d for d in coll if not _matches(d, filter_expr)]
        deleted = len(coll) - len(remaining)
        self.collections[collection] = remaining
        return deleted

    async def count(self, collection: str, filter_expr: dict) -> int:
        return sum(1 for d in self._coll(collection) if _matches(d, filter_expr))

    async def aggregate(self, collection: str, pipeline: list[dict]) -> list[dict]:
        return []

    # ---- TTL（已知局限的显式出口）----

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        """按已声明的 TTL 索引删除已过期文档（显式调用，非默认行为）。

        真 Mongo 的 TTL 监控异步（~60s），默认不在此 fake 模拟（见模块 docstring）。
        需要验证 TTL 逻辑的测试可主动调用本方法。

        Args:
            now: 基准时刻（测试注入），默认当前 UTC。

        Returns:
            删除的文档数。
        """
        base = now if now is not None else datetime.now(timezone.utc)
        removed = 0
        for collection, specs in self.indexes.items():
            ttl_fields = [s["keys"][0][0] for s in specs if s["ttl"] is not None]
            if not ttl_fields:
                continue
            remaining = []
            for doc in self._coll(collection):
                expired = False
                for field in ttl_fields:
                    val = _get_field(doc, field)
                    if isinstance(val, datetime) and val < base:
                        expired = True
                        break
                if expired:
                    removed += 1
                else:
                    remaining.append(doc)
            self.collections[collection] = remaining
        return removed

    async def close(self) -> None:
        self.collections.clear()
