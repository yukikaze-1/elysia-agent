#!/usr/bin/env python
"""幂等回填历史工具消息的空/缺失 message_id（spec D6）。

用法:
    python scripts/backfill_message_ids.py --dry-run   # 只报告将改动数，不写库
    python scripts/backfill_message_ids.py             # 实跑回填
"""
import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))
load_dotenv(_SCRIPT_DIR / ".env")

from bson import ObjectId  # noqa: E402

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory  # noqa: E402
from core.storage.message_repo import COLLECTION as MSG_COLLECTION  # noqa: E402
from core.storage.session_repo import COLLECTION as SESSION_COLLECTION  # noqa: E402
from core.utils.message_id import stable_tool_message_id  # noqa: E402

_EMPTY_ID_FILTER = {"message_id": {"$in": ["", None]}}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填历史工具消息空 message_id")
    parser.add_argument("--dry-run", action="store_true",
                        help="只扫描报告将改动的文档数，不写库")
    parser.add_argument("--limit", type=int, default=0,
                        help="每批处理上限（0=全量）")
    return parser.parse_args(argv)


async def backfill_archive(db: DocumentDBBackend, *, dry_run: bool = False,
                           limit: int = 0) -> tuple[int, int]:
    """message_archive：role=tool 且空/缺失 message_id → 派生稳定 ID。返回 (changed, skipped)。

    keyset 分页（_id $gt）：更新后文档离开过滤集（message_id 非空），skip-offset
    会越过收缩后的剩余文档（review 修复——>batch 空 ID 单轮收敛不跳档）。
    """
    changed = skipped = 0
    batch = 100
    last_id = None
    while True:
        filt = {"role": "tool", "message_id": {"$in": ["", None]}}
        if last_id is not None:
            filt["_id"] = {"$gt": ObjectId(last_id)}
        docs = await db.find(MSG_COLLECTION, filt,
                             sort=[("_id", 1)], limit=batch)
        if not docs:
            break
        last_id = docs[-1]["_id"]
        for doc in docs:
            tc, ts = doc.get("tool_call_id"), doc.get("timestamp")
            if not tc or not ts:
                skipped += 1
                continue
            mid = stable_tool_message_id(tc, ts)
            if not dry_run:
                await db.update_one(
                    MSG_COLLECTION, {"_id": ObjectId(doc["_id"])},
                    {"$set": {"message_id": str(mid)}})
            changed += 1
        if limit and changed + skipped >= limit:
            break
    return changed, skipped


async def backfill_sessions(db: DocumentDBBackend, *, dry_run: bool = False,
                            limit: int = 0) -> tuple[int, int]:
    """sessions.messages[]：role=tool 且空/缺失 message_id → 按数组下标 $set。返回 (changed, skipped)。"""
    changed = skipped = 0
    batch = 100
    skip = 0
    while True:
        docs = await db.find(SESSION_COLLECTION, {}, limit=batch, skip=skip)
        if not docs:
            break
        skip += len(docs)
        for doc in docs:
            messages = doc.get("messages") or []
            updates: dict[str, object] = {}
            for idx, m in enumerate(messages):
                if m.get("role") != "tool" or m.get("message_id"):
                    continue
                tc, ts = m.get("tool_call_id"), m.get("timestamp")
                if not tc or not ts:
                    skipped += 1
                    continue
                updates[f"messages.{idx}.message_id"] = str(
                    stable_tool_message_id(tc, ts))
            if updates and not dry_run:
                await db.update_one(
                    SESSION_COLLECTION, {"_id": ObjectId(doc["_id"])},
                    {"$set": updates})
            changed += len(updates)
        if limit and skip >= limit:
            break
    return changed, skipped


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db = DocumentDBFactory.create("mongodb/default")
    mode = "dry-run" if args.dry_run else "实跑"
    a_changed, a_skipped = await backfill_archive(db, dry_run=args.dry_run,
                                                  limit=args.limit)
    s_changed, s_skipped = await backfill_sessions(db, dry_run=args.dry_run,
                                                   limit=args.limit)
    print(f"[{mode}] archive: 回填 {a_changed}，跳过(无法派生) {a_skipped}")
    print(f"[{mode}] sessions: 回填 {s_changed}，跳过(无法派生) {s_skipped}")
    await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
