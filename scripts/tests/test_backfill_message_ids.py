"""回填脚本幂等性测试（spec D6，marker=unit）。"""
import asyncio

import pytest
from bson import ObjectId

from core.storage.message_repo import COLLECTION as MSG_COLLECTION
from core.storage.session_repo import COLLECTION as SESSION_COLLECTION
from scripts.backfill_message_ids import backfill_archive, backfill_sessions


pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeDocDB:
    """内存文档 DB：支持 find($in/$gt keyset/sort) + update_one($set)。

    镜像真实 backend（find 返回 str _id、update 按 _id ObjectId 过滤）的关键语义：
    更新后文档离开过滤集（message_id 非空）→ keyset 分页不跳档。
    """

    def __init__(self, docs: dict[str, list[dict]]):
        self._docs = {k: list(v) for k, v in docs.items()}

    def _match(self, d: dict, filter_expr: dict) -> bool:
        for k, v in filter_expr.items():
            if isinstance(v, dict):
                # 支持 $in（含缺失→None）与 $gt（keyset _id）
                if "$in" in v and d.get(k) not in v["$in"]:
                    return False
                if "$gt" in v and not (str(d.get(k)) > str(v["$gt"])):
                    return False
            elif isinstance(v, ObjectId):
                # 镜像真实 backend：update 按 _id ObjectId 过滤（doc 中是 str）
                if str(d.get(k)) != str(v):
                    return False
            elif d.get(k) != v:
                return False
        return True

    async def find(self, collection, filter_expr, *, sort=None, limit=100, skip=0):
        hits = [d for d in self._docs.get(collection, [])
                if self._match(d, filter_expr)]
        if sort:
            for key, direction in sort:
                hits.sort(key=lambda d: str(d.get(key, "")), reverse=(direction == -1))
        return hits[skip:skip + limit]

    async def update_one(self, collection, filter_expr, update, upsert=False):
        for d in self._docs.get(collection, []):
            if self._match(d, filter_expr):
                for path, value in (update.get("$set") or {}).items():
                    self._set_path(d, path, value)
                return 1
        return 0

    @staticmethod
    def _set_path(d: dict, path: str, value) -> None:
        """按点号路径写值：`messages.0.message_id` → d["messages"][0]["message_id"]。

        镜像真实 Mongo `$set` 的点号嵌套语义（数组下标路径）。
        """
        parts = path.split(".")
        node = d
        for part in parts[:-1]:
            node = node[int(part)] if part.isdigit() else node[part]
        last = parts[-1]
        if last.isdigit():
            node[int(last)] = value
        else:
            node[last] = value

    async def count(self, collection, filter_expr):
        return len(self._docs.get(collection, []))


def _tool_doc(mid="", tool_call_id="call_1", timestamp="2026-08-13T00:00:00Z",
              _id="0123456789abcdef01234567"):
    return {"_id": _id, "role": "tool", "message_id": mid,
            "tool_call_id": tool_call_id, "timestamp": timestamp,
            "tool_name": "get_time", "content": [{"type": "text", "content": "ok"}]}


def test_archive_empty_ids_filled_deterministically():
    docs = [_tool_doc(mid=""), _tool_doc(mid="", tool_call_id="call_2",
                                          timestamp="2026-08-13T00:00:01Z",
                                          _id="0123456789abcdef01234568")]
    db = _FakeDocDB({MSG_COLLECTION: docs})
    changed, skipped = _run(backfill_archive(db))
    assert changed == 2 and skipped == 0
    for d in db._docs[MSG_COLLECTION]:
        assert str(d["message_id"]).startswith("msg_")


def test_archive_idempotent_rerun_zero_changes():
    docs = [_tool_doc(mid="")]
    db = _FakeDocDB({MSG_COLLECTION: docs})
    _run(backfill_archive(db))
    before = [str(d["message_id"]) for d in db._docs[MSG_COLLECTION]]
    changed, _ = _run(backfill_archive(db))
    assert changed == 0
    after = [str(d["message_id"]) for d in db._docs[MSG_COLLECTION]]
    assert before == after


def test_archive_keyset_pagination_no_skips():
    """>batch（batch=100）空 ID 文档 keyset 分页单轮收敛，无跳档（review 修复）。"""
    docs = [_tool_doc(mid="", tool_call_id=f"call_{i}",
                      timestamp=f"2026-08-13T00:00:0{i % 10}Z",
                      _id=f"{i:024d}") for i in range(150)]
    db = _FakeDocDB({MSG_COLLECTION: docs})
    changed, skipped = _run(backfill_archive(db))
    assert changed == 150 and skipped == 0
    assert all(str(d["message_id"]).startswith("msg_")
               for d in db._docs[MSG_COLLECTION])


def test_archive_unable_to_derive_skipped_not_crashed():
    docs = [_tool_doc(mid="", tool_call_id="")]  # 缺 tool_call_id → 无法派生
    db = _FakeDocDB({MSG_COLLECTION: docs})
    changed, skipped = _run(backfill_archive(db))
    assert changed == 0 and skipped == 1
    assert db._docs[MSG_COLLECTION][0]["message_id"] == ""


def test_sessions_array_elements_filled_by_index():
    sess = {"_id": "0123456789abcdef01234567", "session_id": "s1",
            "messages": [_tool_doc(mid=""), _tool_doc(mid="", tool_call_id="call_2")]}
    db = _FakeDocDB({SESSION_COLLECTION: [sess]})
    changed, skipped = _run(backfill_sessions(db))
    assert changed == 2 and skipped == 0
    for m in db._docs[SESSION_COLLECTION][0]["messages"]:
        assert str(m["message_id"]).startswith("msg_")
