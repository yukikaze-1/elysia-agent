#!/usr/bin/env python3
"""清理 default 会话里由测试套件写入的消息（2026-09-11）。

## 背景

`default` 会话被测试套件当成草稿纸：多条测试用 `Agent(config=AppConfig())` 直接跑
真实管线（真实库防线建成前），把 `process_message("消息1")` 之类的输入与
`FakeLLMBackend` 的固定响应（`hello` / `ok` / `好的我记住了`）写进真实 `sessions` +
`message_archive`。实测 238 条归档中约 123 条疑似测试来源。

## 判定（角色感知，保守）

**高置信**（脚本会删）——判据是「该 (role, text) 组合不可能来自真实对话」：

- `message_id == "msg-test"`：`TestDebugDbApi` 硬编码 ID，真实消息不会用它；
- **user** 侧测试输入：`消息1` / `消息2` / `只有agent1知道的消息` /
  `写一首很长的诗，越长越好` / `写一首长诗，越多越好` / `stop!`；
- **assistant** 侧 FakeLLM 固定响应：`hello` / `ok` / `test` / `test response` /
  `好的我记住了`——真实角色回复是长篇中文台词，不会是这些短英文词。

**模糊**（只列出，**不删**）——单独看都可能是真实消息：

- `我叫妖梦`（用户本名就是妖梦）/ `你还记得我叫什么吗`（像真实追问）；
- 通用寒暄 `你好呀` / `你好` / `hi` / `hello`（user 侧）。

## 安全

- 默认 dry-run，只打印候选；`--apply` 才写。
- 写前把受影响文档**整体备份**到 `<coll>_deleted_backup_<ts>`（含原 `_id`）。
- 同时从 `sessions.default.messages` 内嵌列表移除，保持两处一致。

用法:
    python scripts/dev/purge_test_messages.py            # dry-run
    python scripts/dev/purge_test_messages.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

SESSION_ID = "default"
ARCHIVE = "message_archive"
SESSIONS = "sessions"

# (role, text) → 高置信测试来源。角色参与判定：同一文本在 user/assistant 侧
# 可信度不同（如 `hello` 作为 assistant 回复=假，作为 user 输入=可能真实）。
_HIGH_CONFIDENCE: frozenset[tuple[str, str]] = frozenset({
    ("user", "消息1"),
    ("user", "消息2"),
    ("user", "只有agent1知道的消息"),
    ("user", "写一首很长的诗，越长越好"),
    ("user", "写一首长诗，越多越好"),
    ("user", "stop!"),
    ("assistant", "hello"),
    ("assistant", "ok"),
    ("assistant", "test"),
    ("assistant", "test response"),
    ("assistant", "好的我记住了"),
})

_AMBIGUOUS_TEXTS: frozenset[str] = frozenset({
    "我叫妖梦", "你还记得我叫什么吗", "你好呀", "你好", "hi", "hello",
})


def _text_of(doc: dict) -> str:
    parts = doc.get("content") or []
    return "".join(
        p.get("content", "") for p in parts if isinstance(p, dict)
    ).strip()


def _classify(doc: dict) -> str | None:
    """返回 'test' / 'ambiguous' / None。"""
    if str(doc.get("message_id")) == "msg-test":
        return "test"
    text = _text_of(doc)
    if (str(doc.get("role")), text) in _HIGH_CONFIDENCE:
        return "test"
    if text in _AMBIGUOUS_TEXTS:
        return "ambiguous"
    return None


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际删除（默认 dry-run）")
    args = parser.parse_args()

    load_dotenv()
    client = AsyncIOMotorClient(
        os.getenv("MONGODB_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=4000
    )
    db = client[os.getenv("ELYSIA_DB_NAME", "default")]

    docs = [d async for d in db[ARCHIVE].find({"session_id": SESSION_ID})]
    test_docs, ambiguous = [], []
    for d in docs:
        kind = _classify(d)
        if kind == "test":
            test_docs.append(d)
        elif kind == "ambiguous":
            ambiguous.append(d)

    print(f"archive({SESSION_ID}) 共 {len(docs)} 条")
    print(f"  高置信测试来源: {len(test_docs)} 条")
    if test_docs:
        for (role, text), n in Counter(
                (d.get("role"), _text_of(d)) for d in test_docs).most_common():
            print(f"      {n:3d}  [{role}] {text[:34]!r}")
    print(f"  模糊（需人工判断，本脚本不删）: {len(ambiguous)} 条")
    for text, n in Counter(_text_of(d) for d in ambiguous).most_common():
        print(f"      {n:3d}  {text[:34]!r}")

    session = await db[SESSIONS].find_one({"session_id": SESSION_ID})
    embedded = (session or {}).get("messages") or []
    embedded_hits = [m for m in embedded if _classify(m) == "test"]

    if not args.apply:
        print("\n[dry-run] 未做任何修改。加 --apply 执行。")
        client.close()
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if test_docs:
        await db[f"{ARCHIVE}_deleted_backup_{stamp}"].insert_many(test_docs)
        res = await db[ARCHIVE].delete_many({
            "session_id": SESSION_ID,
            "message_id": {"$in": [str(d["message_id"]) for d in test_docs]},
        })
        print(f"archive 删除 {res.deleted_count} 条（备份 → {ARCHIVE}_deleted_backup_{stamp}）")

    if embedded_hits and session is not None:
        await db[f"{SESSIONS}_backup_{stamp}"].insert_one(session)
        kept = [m for m in embedded if _classify(m) != "test"]
        await db[SESSIONS].update_one(
            {"session_id": SESSION_ID}, {"$set": {"messages": kept}}
        )
        print(f"sessions.default 内嵌 {len(embedded)} → {len(kept)}"
              f"（备份 → {SESSIONS}_backup_{stamp}）")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
