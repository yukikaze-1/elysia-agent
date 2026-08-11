"""内存版 DocumentDBBackend —— storage 仓库测试用假后端。

模拟 MongoDB 语义的 subset（$lt/$gt/$elemMatch/$regex/$or、sort、skip、limit、
$set/$setOnInsert + upsert），_id 用 bson.ObjectId 生成、find 返回时转 str（对齐真实后端契约）。
仅用于单测，不涉及真实数据库。
"""

from typing import Any

from bson import ObjectId

from Module.database.documentdb import DocumentDBBackend


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


def _field_matches(field_val: Any, expected: Any) -> bool:
    """字段值 vs 查询条件（标量或操作符 dict）。"""
    if isinstance(expected, dict) and any(str(k).startswith("$") for k in expected):
        for op, arg in expected.items():
            if op == "$options":
                continue
            if op in ("$lt", "$lte", "$gt", "$gte"):
                if field_val is None:
                    return False
                import operator
                cmp_op = {"$lt": operator.lt, "$lte": operator.le,
                          "$gt": operator.gt, "$gte": operator.ge}[op]
                if not cmp_op(field_val, arg):
                    return False
            elif op == "$ne":
                # 数组字段按 Mongo 语义：无任何元素等于 arg 才算 $ne 命中
                if isinstance(field_val, list):
                    if arg in field_val:
                        return False
                elif field_val == arg:
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
            # 未知操作符：忽略（demo subset）
        return True
    # 标量等值：数组字段按 Mongo 语义做元素包含匹配（{field: value} 命中含 value 的数组）
    if isinstance(field_val, list):
        if isinstance(expected, list):
            return field_val == expected
        return expected in field_val
    return field_val == expected


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
        if not _field_matches(_get_field(doc, key), value):
            return False
    return True


def _to_str_id(doc: dict) -> dict:
    """返回 _id 转 str 的文档副本（对齐真实后端契约）。"""
    out = dict(doc)
    if isinstance(out.get("_id"), ObjectId):
        out["_id"] = str(out["_id"])
    return out


class FakeDocumentDBBackend(DocumentDBBackend):
    """内存版文档数据库后端。"""

    def __init__(self) -> None:
        self.collections: dict[str, list[dict]] = {}
        # P1-05：可配置的连通性状态——False 模拟断网（health_check 必须感知）
        self.ping_ok: bool = True

    async def ping(self) -> bool:
        """真实健康探测模拟：返回 ping_ok（断网 = False）。"""
        return self.ping_ok

    def _coll(self, collection: str) -> list[dict]:
        return self.collections.setdefault(collection, [])

    async def insert_one(self, collection: str, document: dict) -> str:
        doc = dict(document)
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        self._coll(collection).append(doc)
        return str(doc["_id"])

    async def insert_many(self, collection: str, documents: list[dict]) -> list[str]:
        return [await self.insert_one(collection, d) for d in documents]

    async def find(self, collection: str, filter_expr: dict, *,
                   sort: list[tuple[str, int]] | None = None,
                   limit: int = 100, skip: int = 0) -> list[dict]:
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
            if "$set" in update:
                doc.update(update["$set"])
            # $setOnInsert 仅在插入时生效——命中已有文档时不修改任何字段
            # （ensure_exists 靠此保证不覆盖既有 messages/title）
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
            return 1
        if upsert:
            doc = dict(update.get("$set", {}))
            doc.update(update.get("$setOnInsert", {}))
            # upsert 时把过滤条件里的等值字段并入新文档（对齐 Mongo 语义）
            for k, v in filter_expr.items():
                if k not in doc and not isinstance(v, dict):
                    doc[k] = v
            doc["_id"] = ObjectId()
            coll.append(doc)
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

    async def ensure_index(self, collection: str, keys: list[tuple[str, int]],
                           **kwargs: Any) -> str:
        return f"{collection}_idx"

    async def aggregate(self, collection: str, pipeline: list[dict]) -> list[dict]:
        return []

    async def close(self) -> None:
        self.collections.clear()
