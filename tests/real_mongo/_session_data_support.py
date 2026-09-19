"""批次 16 real-Mongo 测试层的共享 fixture/helper（`_` 前缀：不被 pytest 收集）。

拆成两个文件（KB-15 排序可达性 / KB-16 身份唯一性），使修复可**按 commit 二分**：
每个 commit 携带自己的 PRE_FIX_RED，而不是把两条 finding 混在一个测试文件里。
"""

from __future__ import annotations

from core.storage.session_repo import SessionRepository

COLL = "sessions"

#: Mongo 按键自动命名——这里刻意写字面量（而不是 import 常量），使测试文件在
#: **修前 HEAD** 上也能 import：PRE_FIX_RED 必须红在"约束不存在/可造重复"这类
#: 行为上，而不是红在"新常量不存在"（§101）。
SESSION_ID_INDEX = "session_id_1"


def ts(second: int) -> str:
    return f"2026-08-01T00:{second // 60:02d}:{second % 60:02d}+00:00"


async def walk(repo: SessionRepository, *, page_size: int = 50) -> list[str]:
    """按 keyset 合同逐页走到底（只用返回的记录推进游标）。"""
    seen: list[str] = []
    after: tuple[str, str] | None = None
    for _ in range(200):
        page = await repo.list_page(limit=page_size, after=after)
        seen.extend(str(s.session_id) for s in page)
        if len(page) < page_size:
            return seen
        last = page[-1]
        after = (last.updated_at, str(last.session_id))
    raise AssertionError("分页不收敛")


async def seed_legacy(backend) -> list[str]:
    """播种"没有可用排序键"的历史文档（真库形态：缺失 / null / 空串 / **数组**）。

    数组型是**承重用例**（Challenger A 反证）：Mongo 的比较与 `$type` 算子会**遍历
    数组元素**，`["1990-…","2040-…"]` 因此既匹配 `{"$gt": ""}`（并按最大元素排序到
    最前）、又被投影层归一成 Sentinel `""` —— 桶判据必须用 `{"$not": {"$type":
    "array"}}` 把它挡在有序桶之外，否则游标 Sentinel 会与"legacy 桶位置"撞车。
    """
    await backend.insert_one(COLL, {"session_id": "conv_e3_missing", "title": "缺字段"})
    await backend.insert_one(COLL, {"session_id": "conv_e3_null", "title": "null",
                                    "updated_at": None})
    await backend.insert_one(COLL, {"session_id": "conv_e3_empty", "title": "空串",
                                    "updated_at": ""})
    await backend.insert_one(COLL, {
        "session_id": "conv_e3_array", "title": "数组",
        "updated_at": ["1990-01-01T00:00:00+00:00", "2040-01-01T00:00:00+00:00"]})
    return ["conv_e3_missing", "conv_e3_null", "conv_e3_empty", "conv_e3_array"]
