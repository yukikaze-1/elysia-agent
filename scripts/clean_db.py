#!/usr/bin/env python
"""清理 Elysia Agent 所有数据库内容。

用法:
    python scripts/clean_db.py              # 清理全部（需确认）
    python scripts/clean_db.py -f           # 清理全部（跳过确认）
    python scripts/clean_db.py --mongo-only # 只清理 MongoDB
    python scripts/clean_db.py --dry-run    # 仅检查连接
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── 路径：以脚本自身目录解析 .env 和 data/ ──────────────
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))  # 使 core/ Module/ 可 import
load_dotenv(_SCRIPT_DIR / ".env")

from core.memory.session import SessionMemory
from core.memory.message_archive import MessageArchive
from core.memory.long_term import LongTermMemory
from core.token_tracker import TokenTracker
from Module.database.documentdb import DocumentDBFactory
from Module.database.vectordb import VectorDBFactory


# ── CLI ──────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="清理 Elysia Agent 所有数据库内容",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="跳过确认提示，直接执行",
    )
    parser.add_argument(
        "--mongo-only",
        action="store_true",
        help="只清理 MongoDB（sessions, message_archive, token_usage）",
    )
    parser.add_argument(
        "--milvus-only",
        action="store_true",
        help="只清理 Milvus（long_term_memory）",
    )
    parser.add_argument(
        "--files-only",
        action="store_true",
        help="只清理本地 data/ 文件",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将要清理的内容，不实际执行",
    )
    return parser.parse_args(argv)


def build_targets(args: argparse.Namespace) -> set[str]:
    """根据 CLI 选项确定清理目标。"""
    if args.mongo_only or args.milvus_only or args.files_only:
        targets = set()
        if args.mongo_only:
            targets.add("mongo")
        if args.milvus_only:
            targets.add("milvus")
        if args.files_only:
            targets.add("files")
        return targets
    return {"mongo", "milvus", "files"}


# ── Cleaners ─────────────────────────────────────────────

class MongoCleaner:
    """MongoDB 清理器。

    清空 default 数据库的三个 collection：
    sessions, message_archive, token_usage。
    使用 delete_many({}) 清空文档，保留 indexes。

    backend 在 clean() 中创建（而非 __init__），确保构造失败也被
    asyncio.gather(return_exceptions=True) 捕获。
    """

    async def clean(self) -> list[tuple[str, bool, int | str]]:
        """执行清理，返回 (collection, ok, detail) 列表。"""
        db = DocumentDBFactory.create("mongodb/default")
        try:
            return await self._clean(db)
        finally:
            await db.close()

    async def _clean(self, db) -> list[tuple[str, bool, int | str]]:
        results = []

        # sessions — 直接 delete_many
        try:
            deleted = await db.delete_many(SessionMemory.COLLECTION, {})
            results.append((SessionMemory.COLLECTION, True, deleted))
        except Exception as e:
            results.append((SessionMemory.COLLECTION, False, str(e)))

        # message_archive — 使用 MessageArchive.clear_all()
        try:
            archive = MessageArchive()
            await archive.connect(db)
            deleted = await archive.clear_all()
            results.append((MessageArchive.COLLECTION, True, deleted))
        except Exception as e:
            results.append((MessageArchive.COLLECTION, False, str(e)))

        # token_usage — 使用 TokenTracker.clear_all()
        try:
            deleted = await TokenTracker.clear_all(db)
            results.append((TokenTracker.COLLECTION, True, deleted))
        except Exception as e:
            results.append((TokenTracker.COLLECTION, False, str(e)))

        return results


class MilvusCleaner:
    """Milvus 清理器。

    通过 LongTermMemory.clear_all() 清空向量记忆
    （drop + recreate collection）。
    """

    def __init__(self):
        self._ltm = LongTermMemory()

    async def clean(self) -> tuple[str, bool, int | str]:
        """执行清理，返回 (collection, ok, detail)。"""
        try:
            deleted = await self._ltm.clear_all()
            return (LongTermMemory.COLLECTION, True, deleted)
        except Exception as e:
            return (LongTermMemory.COLLECTION, False, str(e))
        finally:
            await self._ltm.close()


class FileCleaner:
    """本地文件清理器。

    删除 data/ 下的持久化文件和 vision 缓存。
    """

    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            data_dir = str(_SCRIPT_DIR / "data")
        self._data = Path(data_dir)

    def clean(self) -> list[tuple[str, bool, str]]:
        """执行清理，返回 (filename, ok, detail) 列表。"""
        results = []

        # 目标文件列表
        files = [
            self._data / "session_default.json",
            self._data / "token_usage_pending.jsonl",
            self._data / "scheduler_checkpoint.json",
        ]

        # vision 缓存（glob 匹配）
        vision_dir = self._data / "cache" / "vision"
        if vision_dir.exists():
            for f in vision_dir.glob("*.json"):
                files.append(f)

        for f in files:
            name = str(f.relative_to(self._data)) if f.is_relative_to(self._data) else f.name
            try:
                if f.exists():
                    f.unlink()
                    results.append((name, True, "deleted"))
                else:
                    results.append((name, False, "not found (skipped)"))
            except Exception as e:
                results.append((name, False, str(e)))

        return results


# ── 确认 / dry-run ───────────────────────────────────────

async def confirm(targets: set[str]) -> bool:
    """打印清理目标并请求用户确认。返回 True 表示继续。"""
    labels = {
        "mongo": "  MongoDB: sessions, message_archive, token_usage (delete_many)",
        "milvus": "  Milvus: long_term_memory (drop + recreate)",
        "files": "  data/: session_default.json, token_usage_pending.jsonl, scheduler_checkpoint.json, cache/vision/*.json",
    }
    print("将要清理以下内容:")
    for t in sorted(targets):
        print(labels[t])
    print()
    try:
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, input, "确认清理? 输入 yes 继续，其他取消: "
        )
        return answer.strip().lower() == "yes"
    except (KeyboardInterrupt, EOFError):
        print()
        return False


async def dry_run_check(targets: set[str]) -> bool:
    """检查数据库连接可用性。返回 True 表示全部可达。"""
    all_ok = True

    if "mongo" in targets:
        try:
            db = DocumentDBFactory.create("mongodb/default")
            try:
                # 执行轻量操作验证连接可用（motor 的 create 是 lazy 的）
                await db.count(SessionMemory.COLLECTION, {})
                print("✅ MongoDB: 连接正常")
            finally:
                await db.close()
        except Exception as e:
            print(f"❌ MongoDB: 连接失败 — {e}")
            all_ok = False

    if "milvus" in targets:
        try:
            vector_db = VectorDBFactory.create("milvus/memory")
            try:
                await vector_db.collection_exists(LongTermMemory.COLLECTION)
                print("✅ Milvus: 连接正常")
            finally:
                await vector_db.close()
        except Exception as e:
            print(f"❌ Milvus: 连接失败 — {e}")
            all_ok = False

    if "files" in targets:
        cleaner = FileCleaner()
        data_dir = cleaner._data
        if data_dir.exists():
            print(f"✅ 数据目录存在: {data_dir}")
        else:
            print(f"⚠️  数据目录不存在: {data_dir}")
            all_ok = False

    return all_ok


# ── 汇总报告 ─────────────────────────────────────────────

def report(results: list, *, exit_on_failure: bool = True) -> None:
    """打印汇总报告。如果 exit_on_failure 且存在失败项，sys.exit(1)。"""
    print()
    print("=" * 50)
    print("清理结果:")
    print("=" * 50)

    all_ok = True
    for item in results:
        if isinstance(item, list):
            # MongoCleaner / FileCleaner 返回列表
            for name, ok, detail in item:
                status = "✅" if ok else "❌"
                if ok:
                    suffix = f"{detail} 条" if isinstance(detail, int) else str(detail)
                    print(f"  {status} {name}: {suffix}")
                else:
                    print(f"  {status} {name}: {detail}")
                    all_ok = False
        elif isinstance(item, tuple):
            # MilvusCleaner 返回单个 tuple
            name, ok, detail = item
            status = "✅" if ok else "❌"
            if ok:
                suffix = f"{detail} 条已清除" if isinstance(detail, int) else str(detail)
                print(f"  {status} {name}: {suffix}")
            else:
                print(f"  {status} {name}: {detail}")
                all_ok = False
        elif isinstance(item, Exception):
            print(f"  ❌ 未预期错误: {item}")
            all_ok = False

    print()
    if all_ok:
        print("全部清理完成 ✅")
    else:
        print("部分清理失败，详见上方报告 ❌")
        if exit_on_failure:
            sys.exit(1)


# ── Main ─────────────────────────────────────────────────

async def main() -> None:
    args = parse_args()
    targets = build_targets(args)

    # dry-run: 仅检查连接
    if args.dry_run:
        ok = await dry_run_check(targets)
        sys.exit(0 if ok else 1)

    # 确认
    if not args.force:
        if not await confirm(targets):
            print("已取消")
            return

    # 并发执行清理
    tasks = []
    if "mongo" in targets:
        tasks.append(MongoCleaner().clean())
    if "milvus" in targets:
        tasks.append(MilvusCleaner().clean())
    if "files" in targets:
        # FileCleaner.clean() 是同步方法，用 run_in_executor
        loop = asyncio.get_running_loop()
        tasks.append(loop.run_in_executor(None, FileCleaner().clean))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    report(results)


if __name__ == "__main__":
    asyncio.run(main())
