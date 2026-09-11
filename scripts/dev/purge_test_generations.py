#!/usr/bin/env python3
"""清理测试跑留下的 generations 残留（2026-09-11）。

## 背景

测试用 `Agent(config=AppConfig())` 跑真实管线时，`GenerationRegistry` 会登记
generation；测试中途取消/结束则留下 `status=running|interrupted` 的孤儿。真实库防线
建成前这些直接写进了生产 `generations` 集合。

## 判定

`--date <YYYY-MM-DD>`（默认今天）当天的 generation 中，满足**全部**条件者：

- `request_id == "P001"`（测试/调试请求 ID，非 WS 真实请求 ID）；且
- 关联的 assistant 消息文本为**空**（未产出内容的残留）。

两者同时成立才删——真实中断的生成通常仍有部分文本，且 request_id 是 WS 侧生成值。

用法:
    python scripts/dev/purge_test_generations.py                # dry-run（今天）
    python scripts/dev/purge_test_generations.py --apply
    python scripts/dev/purge_test_generations.py --date 2026-09-11 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

GENERATIONS = "generations"
ARCHIVE = "message_archive"
TEST_REQUEST_IDS = frozenset({"P001"})


def _text_of(doc: dict | None) -> str:
    if not doc:
        return ""
    return "".join(
        p.get("content", "") for p in (doc.get("content") or []) if isinstance(p, dict)
    ).strip()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际删除（默认 dry-run）")
    parser.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD（默认今天 UTC）")
    args = parser.parse_args()

    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    load_dotenv()
    client = AsyncIOMotorClient(
        os.getenv("MONGODB_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=4000
    )
    db = client[os.getenv("ELYSIA_DB_NAME", "default")]

    candidates = []
    async for g in db[GENERATIONS].find({"created_at": {"$gte": day}}):
        if g.get("request_id") not in TEST_REQUEST_IDS:
            continue
        msg = await db[ARCHIVE].find_one({"message_id": g.get("message_id")})
        if _text_of(msg) != "":
            continue  # 有内容 → 可能是真实中断，保守保留
        candidates.append((g, msg))

    print(f"{day} 的 generations 中，测试残留候选: {len(candidates)} 条")
    for g, msg in candidates:
        print(f"  {str(g.get('created_at'))[:19]} {g.get('generation_id')} "
              f"status={g.get('status')} req={g.get('request_id')} "
              f"msg={g.get('message_id')}")

    if not args.apply:
        print("\n[dry-run] 未做任何修改。加 --apply 执行。")
        client.close()
        return

    if not candidates:
        print("无候选，未做修改。")
        client.close()
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    gens = [g for g, _ in candidates]
    msgs = [m for _, m in candidates if m is not None]
    await db[f"{GENERATIONS}_deleted_backup_{stamp}"].insert_many(gens)
    res = await db[GENERATIONS].delete_many(
        {"generation_id": {"$in": [g["generation_id"] for g in gens]}}
    )
    print(f"generations 删除 {res.deleted_count} 条"
          f"（备份 → {GENERATIONS}_deleted_backup_{stamp}）")

    if msgs:
        await db[f"{ARCHIVE}_deleted_backup_{stamp}"].insert_many(msgs)
        res = await db[ARCHIVE].delete_many(
            {"message_id": {"$in": [m["message_id"] for m in msgs]}}
        )
        print(f"关联空 assistant 消息删除 {res.deleted_count} 条（同备份前缀）")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
