#!/usr/bin/env python
"""幂等回填存量 assistant 记录的**角色身份快照**（问题单 §5.2 ★☆☆ 迁移脚本）。

## 背景

`character_profiles=true` 时前端要求每条 assistant 消息都带角色身份快照，缺失即对
消息历史**整批 fail-closed**（避免「游标推进但静默丢历史」）。历史数据、测试残留与
（2026-09-11 前）工具轮归档漏传 `character=` 都会留下 `character=null` 的记录。

`core/cognitive/tool_phase.py` 的源头缺口已修（新记录不再产生缺口）；本脚本清理**存量**。

## 不伪造归属（硬约束）

补戳值**只**来自真实可解析的数据，任何一环解析不到就**跳过并计数**，绝不猜测：

1. 会话必须存在且 `character_binding` 含非空 `character_id` + `instance_id`；
2. 实例可解析时，用**实例记录的 revision**（= 该消息写入时实际使用的版本）——
   刻意**不**走 `CharacterService.reconcile_instance_revision`（那会把 follow_latest
   推进到最新版，反而偏离消息当时的真实身份，且会在只读迁移中写库）；
3. 实例缺失时才回退 Definition 的 `currentRevision`；
4. revision/displayName 解析不到 → 跳过；
5. 最终快照经 `CharacterMessageSnapshot` 校验（revision ≥ 1、displayName 非空），
   构造失败 → 跳过。

内置角色（`chi_builtin_*`）走文件源 `BuiltinCharacterSource`（不在 Mongo
`characters` 集合内——问题单 §7.2 记录过：用该集合判定内置角色会误报悬空）。

## 用法

    python scripts/backfill_character_snapshots.py --dry-run   # 只报告，不写库
    python scripts/backfill_character_snapshots.py             # 实跑
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))
load_dotenv(_SCRIPT_DIR / ".env")

from bson import ObjectId  # noqa: E402

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory  # noqa: E402
from core.character.builtin_source import BuiltinCharacterSource  # noqa: E402
from core.schema.message import (  # noqa: E402
    CharacterMessageSnapshot,
    CharacterPresentationSnapshot,
)
from core.storage.character_instance_repo import (  # noqa: E402
    CharacterInstanceRepository,
)
from core.storage.character_repo import CharacterRepository  # noqa: E402
from core.storage.character_revision_repo import (  # noqa: E402
    CharacterRevisionRepository,
)
from core.storage.message_repo import (  # noqa: E402
    ASSISTANT_WITHOUT_CHARACTER_FILTER,
)
from core.storage.message_repo import COLLECTION as MSG_COLLECTION  # noqa: E402
from core.storage.session_repo import COLLECTION as SESSION_COLLECTION  # noqa: E402

logger = logging.getLogger(__name__)

_BATCH = 200
"""keyset 分页批大小。"""

_BUILTIN_PREFIX = "chi_builtin_"


def _optional_ref(value: object) -> str | None:
    """展示引用仅接受 str，其余（None/数字/嵌套）一律降级 None（对照 finalize 同名逻辑）。"""
    return value if isinstance(value, str) else None


class SnapshotResolver:
    """由 (character_id, instance_id) 解析真实角色快照；解析不到返回 None。"""

    def __init__(self, db: DocumentDBBackend):
        self._builtins = BuiltinCharacterSource()
        self._characters = CharacterRepository(db=db)
        self._revisions = CharacterRevisionRepository(db=db)
        self._instances = CharacterInstanceRepository(db=db)
        self._builtin_cache: dict[str, CharacterMessageSnapshot | None] = {}
        self._revision_cache: dict[tuple[str, int, str], CharacterMessageSnapshot | None] = {}
        self._instance_cache: dict[str, object | None] = {}

    async def resolve(
        self, character_id: str, instance_id: str,
    ) -> tuple[CharacterMessageSnapshot | None, str]:
        """返回 (快照, 跳过原因)；快照非 None 时原因为空串。"""
        if not character_id or not instance_id:
            return None, "binding 字段为空"

        # 内置角色：文件源（Mongo 无记录，不能用集合判定悬空）
        if instance_id.startswith(_BUILTIN_PREFIX):
            return self._resolve_builtin(character_id, instance_id)

        instance = await self._get_instance(instance_id)
        if instance is None:
            return None, "实例不存在（悬空引用）"
        # 身份交叉核对：绑定的 character 与实例所属 character 必须一致，否则拒绝归属
        if getattr(instance, "characterId", None) != character_id:
            return None, "instance/character 身份不一致"

        revision_no = int(getattr(instance, "revision", 1) or 1)
        snapshot = await self._snapshot_from_revision(character_id, revision_no, instance_id)
        if snapshot is None:
            # 唯一回退：实例记录的 revision 已不可解析 → 用 Definition 当前指针。
            # 跳过原因始终以「主因」（实例 revision 不可解析）开头，避免被回退路径的
            # 次生失败掩盖（运维按原因分类处置）。
            definition = await self._characters.get_definition(character_id)
            if definition is None:
                return None, f"revision {revision_no} 不可解析（且角色定义不存在）"
            fallback_no = int(getattr(definition, "currentRevision", 1) or 1)
            if fallback_no == revision_no:
                return None, f"revision {revision_no} 不可解析"
            snapshot = await self._snapshot_from_revision(
                character_id, fallback_no, instance_id)
            if snapshot is None:
                return None, f"revision {revision_no}/{fallback_no} 均不可解析"
        return snapshot, ""

    def _resolve_builtin(
        self, character_id: str, instance_id: str,
    ) -> tuple[CharacterMessageSnapshot | None, str]:
        if instance_id in self._builtin_cache:
            cached = self._builtin_cache[instance_id]
            return cached, "" if cached else "内置角色解析失败"
        role_name = instance_id.removeprefix(_BUILTIN_PREFIX)
        try:
            definition, revision, _seed = self._builtins.load(role_name)
        except Exception:
            logger.warning("内置角色加载失败：instance=%s", instance_id, exc_info=True)
            self._builtin_cache[instance_id] = None
            return None, "内置角色加载失败"
        if definition.characterId != character_id:
            self._builtin_cache[instance_id] = None
            return None, "instance/character 身份不一致"
        snapshot = self._build(
            character_id, instance_id,
            getattr(revision, "revision", 1),
            getattr(revision, "displayName", ""),
            getattr(revision, "presentation", None),
        )
        self._builtin_cache[instance_id] = snapshot
        return snapshot, "" if snapshot else "内置角色快照构造失败"

    async def _get_instance(self, instance_id: str) -> object | None:
        if instance_id in self._instance_cache:
            return self._instance_cache[instance_id]
        try:
            instance = await self._instances.get(instance_id)
        except Exception:
            logger.warning("实例查询失败：instance=%s", instance_id, exc_info=True)
            instance = None
        self._instance_cache[instance_id] = instance
        return instance

    async def _snapshot_from_revision(
        self, character_id: str, revision_no: int, instance_id: str,
    ) -> CharacterMessageSnapshot | None:
        key = (character_id, revision_no, instance_id)
        if key in self._revision_cache:
            return self._revision_cache[key]
        try:
            revision = await self._revisions.get_revision(character_id, revision_no)
        except Exception:
            logger.warning("revision 查询失败：%s#%d", character_id, revision_no, exc_info=True)
            revision = None
        snapshot = (
            self._build(
                character_id, instance_id, revision_no,
                getattr(revision, "displayName", ""),
                getattr(revision, "presentation", None),
            )
            if revision is not None else None
        )
        self._revision_cache[key] = snapshot
        return snapshot

    @staticmethod
    def _build(
        character_id: str, instance_id: str, revision_no: object,
        display_name: object, presentation: object,
    ) -> CharacterMessageSnapshot | None:
        """构造并校验快照。

        模型自带硬约束（character_id/instance_id/display_name 非空、revision ≥ 1），
        故任何解析异常/脏值都会在此被拦下 → 返回 None → 调用方跳过，绝不写入半个快照。
        """
        raw = presentation if isinstance(presentation, dict) else {}
        try:
            return CharacterMessageSnapshot(
                character_id=character_id,
                character_instance_id=instance_id,
                character_revision=int(revision_no),  # type: ignore[arg-type]
                display_name=str(display_name),
                presentation=CharacterPresentationSnapshot(
                    avatar_asset_id=_optional_ref(raw.get("avatarAssetId")),
                    live2d_model_id=_optional_ref(raw.get("live2dModelId")),
                    theme_color=_optional_ref(raw.get("themeColor")),
                ),
            )
        except Exception:
            logger.warning("快照构造失败：%s#%s/%s", character_id, revision_no, instance_id)
            return None


class Counters:
    """按跳过原因计数（可见性：运维据此决定人工处置）。"""

    def __init__(self) -> None:
        self.updated = 0
        self.skipped: dict[str, int] = {}

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def render(self) -> str:
        if not self.skipped:
            return f"回填 {self.updated}，跳过 0"
        detail = "；".join(f"{k}={v}" for k, v in sorted(self.skipped.items()))
        return f"回填 {self.updated}，跳过 {sum(self.skipped.values())}（{detail}）"


async def _resolve_bindings(
    db: DocumentDBBackend, session_ids: set[str],
) -> dict[str, tuple[str, str] | None]:
    """批量取会话绑定：session_id → (character_id, instance_id) 或 None（无/坏绑定）。"""
    bindings: dict[str, tuple[str, str] | None] = {}
    for sid in session_ids:
        docs = await db.find(SESSION_COLLECTION, {"session_id": sid}, limit=1)
        if not docs:
            bindings[sid] = None
            continue
        raw = docs[0].get("character_binding")
        if not isinstance(raw, dict):
            bindings[sid] = None
            continue
        cid = raw.get("character_id") or raw.get("characterId")
        iid = raw.get("instance_id") or raw.get("instanceId")
        bindings[sid] = (cid, iid) if isinstance(cid, str) and isinstance(iid, str) \
            and cid.strip() and iid.strip() else None
    return bindings


async def backfill_archive(
    db: DocumentDBBackend, resolver: SnapshotResolver, counters: Counters,
    *, dry_run: bool = False, limit: int = 0,
) -> None:
    """message_archive：assistant 且 character 缺失/null → 按会话绑定补戳。

    keyset 分页（_id $gt）：更新后文档离开过滤集（character 非 null），skip-offset
    会越过收缩后的剩余文档（对照 backfill_message_ids.py 同类修复）。
    """
    last_id = None
    while True:
        filt = dict(ASSISTANT_WITHOUT_CHARACTER_FILTER)
        if last_id is not None:
            filt["_id"] = {"$gt": ObjectId(last_id)}
        docs = await db.find(MSG_COLLECTION, filt, sort=[("_id", 1)], limit=_BATCH)
        if not docs:
            break
        last_id = docs[-1]["_id"]

        session_ids = {str(d.get("session_id") or "") for d in docs}
        bindings = await _resolve_bindings(db, session_ids)

        for doc in docs:
            sid = str(doc.get("session_id") or "")
            binding = bindings.get(sid)
            if binding is None:
                counters.skip("会话缺角色绑定")
                continue
            snapshot, reason = await resolver.resolve(*binding)
            if snapshot is None:
                counters.skip(reason)
                continue
            if not dry_run:
                await db.update_one(
                    MSG_COLLECTION, {"_id": ObjectId(doc["_id"])},
                    {"$set": {"character": snapshot.model_dump(mode="json")}})
            counters.updated += 1
        if limit and counters.updated + sum(counters.skipped.values()) >= limit:
            break


async def backfill_sessions(
    db: DocumentDBBackend, resolver: SnapshotResolver, counters: Counters,
    *, dry_run: bool = False, limit: int = 0,
) -> None:
    """sessions.messages[]：内嵌 assistant 且 character 缺失/null → 按数组下标 $set。

    会话文档本身始终留在过滤集内（过滤条件不随更新收缩），但沿用 _id keyset 以免
    并发新增/删除导致 skip-offset 漂移。
    """
    last_id = None
    while True:
        filt: dict = {}
        if last_id is not None:
            filt["_id"] = {"$gt": ObjectId(last_id)}
        docs = await db.find(SESSION_COLLECTION, filt, sort=[("_id", 1)], limit=_BATCH)
        if not docs:
            break
        last_id = docs[-1]["_id"]

        for doc in docs:
            messages = doc.get("messages") or []
            targets = [
                (idx, m) for idx, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "assistant"
                and m.get("character") in (None, {})
            ]
            if not targets:
                continue
            raw = doc.get("character_binding")
            cid = raw.get("character_id") if isinstance(raw, dict) else None
            iid = raw.get("instance_id") if isinstance(raw, dict) else None
            if not (isinstance(cid, str) and isinstance(iid, str) and cid and iid):
                for _idx, _m in targets:
                    counters.skip("会话缺角色绑定")
                continue
            snapshot, reason = await resolver.resolve(cid, iid)
            if snapshot is None:
                for _idx, _m in targets:
                    counters.skip(reason)
                continue
            updates = {
                f"messages.{idx}.character": snapshot.model_dump(mode="json")
                for idx, _m in targets
            }
            if not dry_run:
                await db.update_one(
                    SESSION_COLLECTION, {"_id": ObjectId(doc["_id"])},
                    {"$set": updates})
            counters.updated += len(updates)
        if limit and counters.updated + sum(counters.skipped.values()) >= limit:
            break


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填存量 assistant 记录的角色身份快照")
    parser.add_argument("--dry-run", action="store_true",
                        help="只扫描报告将改动的文档数，不写库")
    parser.add_argument("--limit", type=int, default=0,
                        help="处理上限（0=全量）")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db = DocumentDBFactory.create("mongodb/default")
    resolver = SnapshotResolver(db)
    mode = "dry-run" if args.dry_run else "实跑"

    archive = Counters()
    await backfill_archive(db, resolver, archive, dry_run=args.dry_run, limit=args.limit)
    print(f"[{mode}] message_archive: {archive.render()}")

    sessions = Counters()
    await backfill_sessions(db, resolver, sessions, dry_run=args.dry_run, limit=args.limit)
    print(f"[{mode}] sessions.messages[]: {sessions.render()}")

    await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
