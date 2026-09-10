#!/usr/bin/env python3
"""清理开发者 `default` 库中的**测试残留**（2026-09-11）。

背景：`unit`/`service` 测试此前会连开发者本机生产库并写入（见
`core/tests/_real_db_guard.py`）。本脚本清掉已核实的一次性残留——**只删已确认的
测试产物**，并要求先备份。

用法：
    conda run -n Elysia python scripts/dev/purge_test_residue.py --dry-run
    conda run -n Elysia python scripts/dev/purge_test_residue.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# 已核实的测试残留（2026-09-11 逐条核对：0 消息 + 无 character_binding）
TEST_SESSION_TITLES = ("active", "lease", "burst")
SESSION_COLLECTION = "sessions"
CHARACTER_VERSION_COLLECTION = "character_versions"
TEST_CHARACTER_VERSION_SESSION = "test_default_type"


async def _run(apply: bool) -> int:
    load_dotenv()
    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=4000)
    db = client["default"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    # ── 1. 测试会话：仅当标题命中 且 0 消息 且 无角色绑定 ─────────────────────
    victims = []
    async for d in db[SESSION_COLLECTION].find(
        {"title": {"$in": list(TEST_SESSION_TITLES)}}
    ):
        msgs = d.get("messages") or []
        if not msgs and not d.get("character_binding"):
            victims.append(d)

    print(f"[sessions] 候选 {len(victims)} 条（标题 ∈ {TEST_SESSION_TITLES} 且 0 消息且无绑定）:")
    for d in victims:
        print(f"   {d.get('session_id')}  title={d.get('title')}")

    # ── 2. character_versions 测试残留 ─────────────────────────────────────
    cv_query = {"session_id": TEST_CHARACTER_VERSION_SESSION}
    cv_count = await db[CHARACTER_VERSION_COLLECTION].count_documents(cv_query)
    print(f"[character_versions] 候选 {cv_count} 条（session_id={TEST_CHARACTER_VERSION_SESSION!r}）")

    if not apply:
        print("\n（dry-run：未做任何修改）")
        client.close()
        return 0

    if victims:
        backup = f"{SESSION_COLLECTION}_deleted_backup_{stamp}"
        await db[backup].insert_many(victims)
        ids = [d["session_id"] for d in victims]
        res = await db[SESSION_COLLECTION].delete_many({"session_id": {"$in": ids}})
        print(f"[sessions] 已备份 {len(victims)} 条 → {backup}；删除 {res.deleted_count} 条")

    if cv_count:
        cv_backup = f"{CHARACTER_VERSION_COLLECTION}_deleted_backup_{stamp}"
        await db[cv_backup].insert_many(
            [d async for d in db[CHARACTER_VERSION_COLLECTION].find(cv_query)]
        )
        res = await db[CHARACTER_VERSION_COLLECTION].delete_many(cv_query)
        print(f"[character_versions] 已备份 → {cv_backup}；删除 {res.deleted_count} 条")

    client.close()
    print("\n完成。备份集合保留在 default 库中，确认无误后可手动 drop。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 default 库中的测试残留")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只列出将删除的文档")
    g.add_argument("--apply", action="store_true", help="执行删除（先备份）")
    args = ap.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == "__main__":
    sys.exit(main())
