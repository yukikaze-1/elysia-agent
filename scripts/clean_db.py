#!/usr/bin/env python
"""清理 Elysia Agent 数据库内容。

用法:
    python scripts/clean_db.py                      # 全部清理（需确认）
    python scripts/clean_db.py -f                   # 全部清理（跳过确认）
    python scripts/clean_db.py --conversation-only  # 只清对话数据（保留角色/身份）
    python scripts/clean_db.py --mongo-only         # 只清理 MongoDB（全量范围）
    python scripts/clean_db.py --conversation-only --milvus-only  # 可组合
    python scripts/clean_db.py --dry-run            # 仅检查连接

清理范围（两个 mongo 范围，集合清单见 CONVERSATION_COLLECTIONS /
FULL_ONLY_COLLECTIONS 单源表）:
    对话数据: sessions, message_archive, token_usage, generations,
              idempotency, desktop_calls
    仅全量:   characters, character_revisions, character_instances,
              character_versions, character_l2_versions(旧), attachments
    永不删除: user_identity_profiles（用户身份 Mongo 权威，BE-STAGEH-02）

**必须在后端服务停止后运行**：idempotency / desktop_calls / attachments 都是
fail-closed，且 desktop_calls 有进程内缓存作为读权威——服务在跑时删库，内存态仍在，
已删记录继续可读；下次关机回写还会把行重新创建。
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── 路径：以脚本自身目录解析 .env 和 data/ ──────────────
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))  # 使 core/ Module/ 可 import
load_dotenv(_SCRIPT_DIR / ".env")

from Module.database.documentdb import DocumentDBBackend
from Module.database.documentdb import DocumentDBFactory
from Module.database.vectordb import VectorDBFactory
from core.memory.long_term import LongTermMemory
from core.storage.attachment_repo import COLLECTION as ATTACHMENT_COLLECTION
from core.storage.character_instance_repo import COLLECTION as CHARACTER_INSTANCE_COLLECTION
from core.storage.character_repo import COLLECTION as CHARACTER_COLLECTION
from core.storage.character_revision_repo import COLLECTION as CHARACTER_REVISION_COLLECTION
from core.storage.character_version_repo import COLLECTION as CHARS_VERSION_COLLECTION
from core.storage.desktop_call_repo import COLLECTION as DESKTOP_CALL_COLLECTION
from core.storage.generation_repo import COLLECTION as GENERATION_COLLECTION
from core.storage.idempotency_repo import COLLECTION as IDEMPOTENCY_COLLECTION
from core.storage.memory_repo import COLLECTION as MEM_COLLECTION
from core.storage.message_repo import COLLECTION as MSG_ARCHIVE_COLLECTION
from core.storage.session_repo import COLLECTION as SESSION_COLLECTION
from core.storage.token_repo import COLLECTION as TOKEN_USAGE_COLLECTION
from core.storage.user_identity_repo import COLLECTION as USER_IDENTITY_COLLECTION

# ── 清理范围单源 ─────────────────────────────────────────
#
# 集合名一律取自各 repository 的 COLLECTION 常量，不复制字符串——集合改名时这里跟着走，
# 不会静默漏删。范围成员被 core/tests/test_clean_db.py 用字面量钉死：增删必须让测试红。

# 对话数据：--conversation-only 与全量模式都清。
CONVERSATION_COLLECTIONS: tuple[str, ...] = (
    SESSION_COLLECTION,
    MSG_ARCHIVE_COLLECTION,
    TOKEN_USAGE_COLLECTION,
    GENERATION_COLLECTION,
    IDEMPOTENCY_COLLECTION,
    DESKTOP_CALL_COLLECTION,
)

# 仅全量模式清（角色 / 版本 / 附件）。--conversation-only 明确保留这些。
#
# attachments 只在全量模式清：对话模式保留附件记录，删掉 message_archive 后它们成为
# 孤儿，由 AttachmentGC 的「未关联超 7 天软删」规则回收（不在此重复实现删除路径，
# 也不动 data/uploads/ 下的磁盘文件）。
FULL_ONLY_COLLECTIONS: tuple[str, ...] = (
    CHARACTER_COLLECTION,
    CHARACTER_REVISION_COLLECTION,
    CHARACTER_INSTANCE_COLLECTION,
    CHARS_VERSION_COLLECTION,
    # 旧集合：迁移到 character_versions 之前的 L2/C6 版本历史，无 repo 常量。
    "character_l2_versions",
    ATTACHMENT_COLLECTION,
)

# 任何范围都不删：用户身份是 Stage H 的 Mongo 权威（BE-STAGEH-02），删掉会让
# Agent.start() 退回 bootstrap 默认身份。列在这里是给测试当回归守卫用。
PROTECTED_COLLECTIONS: tuple[str, ...] = (USER_IDENTITY_COLLECTION,)


def collections_for(scope: str) -> tuple[str, ...]:
    """按清理范围返回集合清单（顺序即删除顺序，报告据此生成）。

    Args:
        scope: "conversation"（只清对话数据）或 "full"（全量）。

    Raises:
        ValueError: 未知范围。
    """
    if scope == "conversation":
        return CONVERSATION_COLLECTIONS
    if scope == "full":
        return CONVERSATION_COLLECTIONS + FULL_ONLY_COLLECTIONS
    raise ValueError(f"未知清理范围: {scope!r}（应为 'conversation' 或 'full'）")


# ── CLI ──────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="清理 Elysia Agent 数据库内容",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="跳过确认提示，直接执行",
    )
    # --mongo-only 与 --conversation-only 对 mongo 的范围诉求冲突，互斥而非静默取其一。
    mongo_group = parser.add_mutually_exclusive_group()
    mongo_group.add_argument(
        "--mongo-only",
        action="store_true",
        help="只清理 MongoDB（全量范围，含角色/附件）",
    )
    mongo_group.add_argument(
        "--conversation-only",
        action="store_true",
        help="只清理对话数据（保留角色、版本、附件、用户身份）",
    )
    parser.add_argument(
        "--milvus-only",
        action="store_true",
        help=f"只清理 Milvus（{MEM_COLLECTION}）",
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
    """根据 CLI 选项确定清理目标（mongo / milvus / files）。"""
    if args.mongo_only or args.conversation_only or args.milvus_only or args.files_only:
        targets = set()
        if args.mongo_only or args.conversation_only:
            targets.add("mongo")
        if args.milvus_only:
            targets.add("milvus")
        if args.files_only:
            targets.add("files")
        return targets
    return {"mongo", "milvus", "files"}


def mongo_scope(args: argparse.Namespace) -> str:
    """mongo 目标使用的集合范围："conversation"（--conversation-only）或 "full"。"""
    return "conversation" if args.conversation_only else "full"


# ── Cleaners ─────────────────────────────────────────────

class MongoCleaner:
    """MongoDB 清理器。

    按 scope 逐集合 delete_many({}) 清空文档，保留 indexes。任一集合失败不中断其余
    集合，失败如实进报告（退出码随之非零）。

    backend 在 clean() 中创建（而非 __init__），确保构造失败也被
    asyncio.gather(return_exceptions=True) 捕获。
    """

    def __init__(self, scope: str = "full"):
        self._scope = scope

    async def clean(self) -> list[tuple[str, bool, int | str]]:
        """执行清理，返回 (collection, ok, detail) 列表。"""
        db = DocumentDBFactory.create("mongodb/default")
        try:
            return await self._clean(db)
        finally:
            await db.close()

    async def _clean(self, db: DocumentDBBackend) -> list[tuple[str, bool, int | str]]:
        results = []
        for collection in collections_for(self._scope):
            try:
                deleted = await db.delete_many(collection, {})
                results.append((collection, True, deleted))
            except Exception as e:
                results.append((collection, False, str(e)))
        return results


class MilvusCleaner:
    """Milvus 清理器。

    通过 LongTermMemory.clear_all() 清空向量记忆
    （drop + recreate collection）。

    backend 在 clean() 中创建（而非 __init__），确保构造失败也被
    asyncio.gather(return_exceptions=True) 捕获。
    """

    async def clean(self) -> tuple[str, bool, int | str]:
        """执行清理，返回 (collection, ok, detail)。"""
        ltm = LongTermMemory()
        try:
            # 先验证 Milvus 连通性，防止 clear_all() 静默降级
            try:
                vector_db = VectorDBFactory.create("milvus/memory")
                await vector_db.close()
            except Exception as e:
                return (MEM_COLLECTION, False, f"Milvus unreachable: {e}")

            deleted = await ltm.clear_all()
            return (MEM_COLLECTION, True, deleted)
        except Exception as e:
            return (MEM_COLLECTION, False, str(e))
        finally:
            await ltm.close()


class FileCleaner:
    """本地文件清理器。

    删除 data/ 下的持久化文件和 vision 缓存。
    """

    # data/ 下固定的对话侧持久化文件（对应 paths.ProjectPath 同名成员）。
    # scheduler_checkpoint.json 由 TaskScheduler 写入（agent.py）。
    _FIXED_FILES: tuple[str, ...] = (
        "token_usage_pending.jsonl",
        "scheduler_checkpoint.json",
        "memory_pending_deletes.json",
        "memory_pending_syncs.json",
    )

    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            data_dir = str(_SCRIPT_DIR / "data")
        self._data = Path(data_dir)

    def clean(self) -> list[tuple[str, bool, str]]:
        """执行清理，返回 (filename, ok, detail) 列表。"""
        results = []

        # 目标文件列表
        files: list[Path] = [self._data / name for name in self._FIXED_FILES]

        # 多会话：每会话一个 data/session_{session_id}.json（默认会话即
        # session_default.json）。只删写死的那一个会漏掉其余会话文件——SessionMemory
        # 在 Mongo 读失败时会 fallback 读这些文件，清库后可能被 JSONL 复活。
        # glob 同时覆盖 session_pending_cleanup.json（会话删除提交后的清理 journal）。
        files.extend(sorted(self._data.glob("session_*.json")))

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
                    results.append((name, True, "already clean"))
            except Exception as e:
                results.append((name, False, str(e)))

        return results


# ── 确认 / dry-run ───────────────────────────────────────

def describe_targets(targets: set[str], scope: str) -> list[str]:
    """生成清理目标说明行（确认提示用）。

    集合清单从范围单源表生成，绝不写死——提示与实际不符是删库脚本最危险的一类 bug。
    """
    lines = []
    if "mongo" in targets:
        lines.append(f"  MongoDB: {', '.join(collections_for(scope))} (delete_many)")
    if "milvus" in targets:
        lines.append(f"  Milvus: {MEM_COLLECTION} (drop + recreate)")
    if "files" in targets:
        names = ", ".join(FileCleaner._FIXED_FILES)
        lines.append(
            f"  data/: {names}, session_*.json（每会话一个）, cache/vision/*.json")
    return lines


async def confirm(targets: set[str], scope: str) -> bool:
    """打印清理目标并请求用户确认。返回 True 表示继续。"""
    print("将要清理以下内容:")
    for line in describe_targets(targets, scope):
        print(line)
    print()
    if "mongo" in targets:
        print("⚠️  请先停止后端服务：desktop_calls/idempotency 有进程内状态，"
              "运行中清理不会生效（内存态仍在，关机回写还会复活数据）。")
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
                await db.count(SESSION_COLLECTION, {})
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
                await vector_db.collection_exists(MEM_COLLECTION)
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
    scope = mongo_scope(args)

    # dry-run: 仅检查连接
    if args.dry_run:
        ok = await dry_run_check(targets)
        sys.exit(0 if ok else 1)

    # 确认
    if not args.force:
        if not await confirm(targets, scope):
            print("已取消")
            return

    # 并发执行清理
    tasks = []
    if "mongo" in targets:
        tasks.append(MongoCleaner(scope=scope).clean())
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
