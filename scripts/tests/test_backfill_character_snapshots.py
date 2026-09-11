"""存量角色戳回填脚本测试（P3 剩余第 2 项，marker=unit）。

覆盖三类断言：
1. **补戳正确**：内置角色（文件源）与 custom（Mongo 实例+revision）都能补出合法快照；
2. **不伪造归属**：会话无绑定 / 实例悬空 / instance-character 身份不一致 / displayName
   非法 → 一律跳过并计数，文档保持原样（绝不清空、绝不猜）；
3. **幂等 + dry-run**：dry-run 零写入；实跑二次运行 0 改动。

归档（message_archive）用共享 `FakeDocumentDBBackend`（支持 $or/$exists，且归档补戳是
扁平 `$set`）；`sessions.messages[]` 需要数组下标 `$set` 与点路径读取，而共享 fake 的
`$set` 是扁平 `dict.update`（无点路径语义）——故本地另建最小 fake 镜像该语义。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.character.instance import CharacterInstance
from core.character.revision import CharacterRevision
from core.storage.message_repo import COLLECTION as MSG_COLLECTION
from core.storage.session_repo import COLLECTION as SESSION_COLLECTION
from scripts.backfill_character_snapshots import (
    Counters,
    SnapshotResolver,
    backfill_archive,
    backfill_sessions,
    parse_args,
)

pytestmark = pytest.mark.unit

BUILTIN_CHAR = "chr_builtin_elysia"
BUILTIN_INST = "chi_builtin_elysia"


def _run(coro: Any) -> Any:
    # asyncio.run 每测试独立新鲜循环（对照 test_backfill_message_ids.py 的同款说明）
    return asyncio.run(coro)


def _fake():
    from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
    return FakeDocumentDBBackend()


def _binding(cid: str = BUILTIN_CHAR, iid: str = BUILTIN_INST) -> dict:
    return {"character_id": cid, "instance_id": iid}


def _assistant_doc(session_id: str = "conv_1", character: Any = None) -> dict:
    return {"role": "assistant", "session_id": session_id, "character": character,
            "message_id": "msg_" + "a" * 26, "content": [{"type": "text", "content": "hi"}]}


# ---------------------------------------------------------------------------
# 数组下标 $set 的最小 fake（仅 sessions.messages[] 路径需要）
# ---------------------------------------------------------------------------

class _ArraySetDocDB:
    """内存 doc DB：find 支持 `_id $gt` keyset；update_one 的 $set 支持点路径 + 数组下标。"""

    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    async def find(self, collection: str, filter_expr: dict, *, sort=None,
                   limit: int = 100, skip: int = 0) -> list[dict]:
        hits = []
        for d in self._docs:
            gt = (filter_expr.get("_id") or {}).get("$gt") if isinstance(
                filter_expr.get("_id"), dict) else None
            if gt is not None and not (str(d["_id"]) > str(gt)):
                continue
            hits.append(d)
        if sort:
            for key, direction in sort:
                hits.sort(key=lambda d: str(d.get(key, "")), reverse=(direction == -1))
        return hits[skip:skip + limit]

    async def update_one(self, collection: str, filter_expr: dict, update: dict,
                         upsert: bool = False) -> int:
        for d in self._docs:
            if str(d["_id"]) != str(filter_expr.get("_id")):
                continue
            for path, value in (update.get("$set") or {}).items():
                self._set_path(d, path, value)
            return 1
        return 0

    @staticmethod
    def _set_path(doc: dict, path: str, value: Any) -> None:
        """镜像 Mongo `$set` 点号语义：`messages.0.character` → 数组元素字段。"""
        parts = path.split(".")
        node: Any = doc
        for part in parts[:-1]:
            node = node[int(part)] if part.isdigit() else node[part]
        last = parts[-1]
        if last.isdigit():
            node[int(last)] = value
        else:
            node[last] = value


# ---------------------------------------------------------------------------
# 内置角色（文件源）补戳
# ---------------------------------------------------------------------------

class TestArchiveBuiltinBackfill:
    def test_backfills_builtin_snapshot_shape(self):
        """内置角色：按会话绑定补出 snake_case 内部快照（presentation 键为 camelCase）。"""
        db = _fake()
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1", "character_binding": _binding()}))
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))

        assert counters.updated == 1 and not counters.skipped
        stored = _run(db.find(MSG_COLLECTION, {}))[0]["character"]
        assert stored["character_id"] == BUILTIN_CHAR
        assert stored["character_instance_id"] == BUILTIN_INST
        assert stored["character_revision"] >= 1
        assert stored["display_name"]
        assert set(stored["presentation"]) == {
            "avatar_asset_id", "live2d_model_id", "theme_color"}

    def test_dry_run_writes_nothing(self):
        db = _fake()
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1", "character_binding": _binding()}))
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters, dry_run=True))

        assert counters.updated == 1  # 会改 1 条
        assert _run(db.find(MSG_COLLECTION, {}))[0]["character"] is None  # 但没写

    def test_idempotent_rerun_zero_changes(self):
        db = _fake()
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1", "character_binding": _binding()}))
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))
        resolver = SnapshotResolver(db)

        first = Counters()
        _run(backfill_archive(db, resolver, first))
        before = _run(db.find(MSG_COLLECTION, {}))[0]["character"]

        second = Counters()
        _run(backfill_archive(db, resolver, second))

        assert first.updated == 1
        assert second.updated == 0, "二次运行必须零改动（补过的记录已离开过滤集）"
        assert _run(db.find(MSG_COLLECTION, {}))[0]["character"] == before

    def test_missing_character_key_also_backfilled(self):
        """`character` 键完全缺失（非显式 null）也在过滤集内。"""
        db = _fake()
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1", "character_binding": _binding()}))
        doc = _assistant_doc()
        doc.pop("character")
        _run(db.insert_one(MSG_COLLECTION, doc))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))
        assert counters.updated == 1


# ---------------------------------------------------------------------------
# 不伪造归属：各类无法解析的情形必须跳过且不改文档
# ---------------------------------------------------------------------------

class TestNeverFabricates:
    def test_session_without_binding_skipped(self):
        db = _fake()
        _run(db.insert_one(SESSION_COLLECTION, {"session_id": "conv_1"}))  # 无 binding
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))

        assert counters.updated == 0
        assert counters.skipped == {"会话缺角色绑定": 1}
        assert _run(db.find(MSG_COLLECTION, {}))[0]["character"] is None

    def test_empty_binding_object_skipped(self):
        """`character_binding={}`（真实故障形态）→ 跳过，不猜。"""
        db = _fake()
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1", "character_binding": {}}))
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))
        assert counters.updated == 0 and counters.skipped

    def test_session_missing_entirely_skipped(self):
        """消息所属会话已不存在（悬空）→ 跳过。"""
        db = _fake()
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc(session_id="conv_gone")))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))
        assert counters.updated == 0
        assert counters.skipped == {"会话缺角色绑定": 1}

    def test_dangling_custom_instance_skipped(self):
        """custom 角色：实例不存在 → 跳过（悬空引用不猜）。"""
        db = _fake()
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1",
                            "character_binding": _binding("chr_custom_x", "chi_custom_x")}))
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))

        assert counters.updated == 0
        assert "实例不存在（悬空引用）" in counters.skipped
        assert _run(db.find(MSG_COLLECTION, {}))[0]["character"] is None

    def test_instance_character_mismatch_skipped(self):
        """实例存在但所属 character 与绑定不一致 → 拒绝归属（防错挂到别的角色）。"""
        from core.storage.character_instance_repo import CharacterInstanceRepository

        db = _fake()
        _run(CharacterInstanceRepository(db=db).save(
            CharacterInstance(instanceId="chi_custom_x", characterId="chr_OTHER")))
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1",
                            "character_binding": _binding("chr_custom_x", "chi_custom_x")}))
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))

        assert counters.updated == 0
        assert "instance/character 身份不一致" in counters.skipped

    def test_revision_with_blank_display_name_skipped(self):
        """revision 的 displayName 为空 → 模型校验拦下 → 跳过（不写半个快照）。

        精确断言跳过原因：必须归因到「revision 不可解析」，而不是被回退路径的次生
        失败（角色定义不存在）掩盖——否则运维按原因分类时会误判。
        """
        from core.character.definition import CharacterDefinition
        from core.storage.character_instance_repo import CharacterInstanceRepository
        from core.storage.character_repo import CharacterRepository
        from core.storage.character_revision_repo import CharacterRevisionRepository

        db = _fake()
        _run(CharacterInstanceRepository(db=db).save(
            CharacterInstance(instanceId="chi_custom_x", characterId="chr_custom_x",
                              revision=1)))
        _run(CharacterRevisionRepository(db=db).append_revision(
            CharacterRevision(characterId="chr_custom_x", revision=1, displayName="")))
        # 定义存在且 currentRevision 同为 1 → 回退无路，唯一原因是 revision 非法
        _run(CharacterRepository(db=db).insert_definition(
            CharacterDefinition(characterId="chr_custom_x", currentRevision=1)))
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1",
                            "character_binding": _binding("chr_custom_x", "chi_custom_x")}))
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))

        assert counters.updated == 0
        assert counters.skipped == {"revision 1 不可解析": 1}, counters.skipped
        assert _run(db.find(MSG_COLLECTION, {}))[0]["character"] is None


# ---------------------------------------------------------------------------
# custom 角色正常补戳
# ---------------------------------------------------------------------------

class TestCustomBackfill:
    def test_custom_instance_uses_its_own_revision(self):
        """custom：用实例记录的 revision（消息写入时的真实身份），并带 presentation。"""
        from core.storage.character_instance_repo import CharacterInstanceRepository
        from core.storage.character_revision_repo import CharacterRevisionRepository

        db = _fake()
        _run(CharacterInstanceRepository(db=db).save(
            CharacterInstance(instanceId="chi_custom_x", characterId="chr_custom_x",
                              revision=2)))
        _run(CharacterRevisionRepository(db=db).append_revision(
            CharacterRevision(characterId="chr_custom_x", revision=2, displayName="小夜",
                              presentation={"avatarAssetId": "att_1",
                                            "themeColor": "#abcdef"})))
        _run(db.insert_one(SESSION_COLLECTION,
                           {"session_id": "conv_1",
                            "character_binding": _binding("chr_custom_x", "chi_custom_x")}))
        _run(db.insert_one(MSG_COLLECTION, _assistant_doc()))

        counters = Counters()
        _run(backfill_archive(db, SnapshotResolver(db), counters))

        assert counters.updated == 1, counters.render()
        stored = _run(db.find(MSG_COLLECTION, {}))[0]["character"]
        assert stored["character_revision"] == 2, "必须用实例自身 revision（非 Definition 当前指针）"
        assert stored["display_name"] == "小夜"
        assert stored["presentation"]["avatar_asset_id"] == "att_1"
        assert stored["presentation"]["theme_color"] == "#abcdef"
        assert stored["presentation"]["live2d_model_id"] is None


# ---------------------------------------------------------------------------
# sessions.messages[] 内嵌补戳
# ---------------------------------------------------------------------------

class TestEmbeddedSessionMessages:
    def _session_doc(self, messages: list[dict], binding: Any = None) -> dict:
        doc: dict = {"_id": "0" * 24, "session_id": "conv_1", "messages": messages}
        if binding is not None:
            doc["character_binding"] = binding
        return doc

    def test_embedded_assistant_backfilled_by_index(self):
        db = _ArraySetDocDB([self._session_doc(
            [_assistant_doc(), {"role": "user"}], _binding())])
        counters = Counters()
        _run(backfill_sessions(db, SnapshotResolver(db), counters))

        assert counters.updated == 1, counters.render()
        embedded = db._docs[0]["messages"][0]["character"]
        assert embedded["character_id"] == BUILTIN_CHAR
        assert embedded["character_instance_id"] == BUILTIN_INST

    def test_embedded_skips_without_binding(self):
        db = _ArraySetDocDB([self._session_doc([_assistant_doc()])])
        counters = Counters()
        _run(backfill_sessions(db, SnapshotResolver(db), counters))

        assert counters.updated == 0
        assert db._docs[0]["messages"][0]["character"] is None

    def test_embedded_already_stamped_untouched(self):
        """已带快照的内嵌消息不动（幂等）。"""
        stamped = _assistant_doc(character={"character_id": "chr_keep"})
        db = _ArraySetDocDB([self._session_doc([stamped], _binding())])
        counters = Counters()
        _run(backfill_sessions(db, SnapshotResolver(db), counters))

        assert counters.updated == 0
        assert db._docs[0]["messages"][0]["character"] == {"character_id": "chr_keep"}


# ---------------------------------------------------------------------------
# CLI 契约
# ---------------------------------------------------------------------------

class TestCliContract:
    def test_defaults_are_safe(self):
        """默认（无参数）不是 dry-run——但必须显式传 --dry-run 才安全；此处锁定旗标语义。"""
        args = parse_args([])
        assert args.dry_run is False and args.limit == 0

    def test_dry_run_flag_parsed(self):
        args = parse_args(["--dry-run", "--limit", "5"])
        assert args.dry_run is True and args.limit == 5
