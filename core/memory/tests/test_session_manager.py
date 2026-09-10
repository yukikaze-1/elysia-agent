"""SessionManager 测试 — 缓存池 + 活动指针 + resolve/switch/CRUD/list/evict。

批次 3 (multi-session) 核心组件。fake repo 实现 Task 4 brief 规定的方法集，
验证 manager 逻辑（不连真实 MongoDB）。
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.memory.session import SessionMemory
from core.memory.session_manager import (
    SessionManager, SessionBusyError, SessionNotFoundError,
)
from core.storage.session_repo import SessionRepository
from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.message import UserMessage
from core.schema.part import TextPart
from core.schema.service_state import ServiceState
from core.schema.types import MessageId, SessionId
from core.storage.session_repo import SessionSummary

# 全文件为 FakeSessionRepo/FakeMsgRepo 内存替身驱动的 SessionManager 单模块测试，
# 无真实 I/O、无外部服务 mock；依文件内已有 @pytest.mark.unit 锚点统一标 unit
# （缺 marker 会被 make test-quick 的 -m "unit or service" 静默跳过）。
pytestmark = pytest.mark.unit


class FakeSessionRepo:
    """Task 4 专用 fake repo — 覆盖 brief 方法集。

    save/load/connect/health_check/create/rename/delete/list_all。
    sessions: sid -> {"messages", "cs", "rs", "ss", "title", "updated_at"}
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.created: list[tuple[str, str]] = []
        self.renamed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.save_calls: list[str] = []
        self.load_calls: list[str] = []
        self._connected = False

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def health_check(self) -> bool:
        return self._connected

    async def save(self, session_id, messages, *,
                   character_state=None, reflector_state=None, service_state=None,
                   character_binding=None):
        self.save_calls.append(session_id)
        doc = self.sessions.setdefault(session_id, {})
        doc["messages"] = list(messages)
        doc["cs"] = character_state
        doc["rs"] = reflector_state
        doc["ss"] = service_state
        doc["cb"] = character_binding
        doc["updated_at"] = f"2026-08-04T00:00:{len(self.save_calls):02d}Z"
        doc.setdefault("title", "")

    async def load(self, sid):
        self.load_calls.append(sid)
        doc = self.sessions.get(sid)
        if doc is None:
            return None
        return (list(doc.get("messages", [])),
                doc.get("cs") or CharacterState(session_id=sid),
                doc.get("rs") or MemoryReflectorState(),
                doc.get("ss") or ServiceState(),
                doc.get("cb") or {})

    async def create(self, sid, title="", character_binding=None):
        self.created.append((sid, title))
        self.sessions[sid] = {"messages": [], "title": title,
                              "cb": character_binding,
                              "updated_at": f"2026-08-04T01:{len(self.created):02d}Z"}

    async def rename(self, sid, title):
        if sid not in self.sessions:
            return False
        self.renamed.append((sid, title))
        self.sessions[sid]["title"] = title
        return True

    async def delete(self, sid):
        if sid not in self.sessions:
            return 0
        del self.sessions[sid]
        self.deleted.append(sid)
        return 1

    async def list_all(self) -> list[SessionSummary]:
        summaries = []
        for sid, doc in self.sessions.items():
            summaries.append(SessionSummary(
                session_id=SessionId(sid),
                message_count=len(doc.get("messages", [])),
                updated_at=doc.get("updated_at", ""),
                title=doc.get("title", ""),
                character_binding=doc.get("cb"),
            ))
        # 模拟 repo 排序：updated_at DESC
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries


class FakeMsgRepo:
    """Task 4 专用 fake msg repo — append/delete_by_session。"""

    def __init__(self):
        self.appended: list[tuple[str, object]] = []
        self.deleted: list[str] = []

    async def append(self, session_id, msg):
        self.appended.append((session_id, msg))

    async def delete_by_session(self, sid) -> int:
        n = sum(1 for (s, _) in self.appended if s == sid)
        self.deleted.append(sid)
        return n


def _make_manager(repo=None, msg_repo=None, **kw) -> SessionManager:
    return SessionManager(repo or FakeSessionRepo(), msg_repo or FakeMsgRepo(), **kw)


# ───────────────────────── eager default ─────────────────────────


@pytest.mark.unit
def test_eager_default_instance_identity():
    """构造后 get('default') 命中同一实例（is），active_id=='default'。"""
    m = _make_manager()
    assert m.active_id == "default"
    assert m.get("default") is m.get("default")
    assert isinstance(m.active, SessionMemory)
    assert m.active.session_id == "default"


# ───────────────────────── ensure_loaded ─────────────────────────


class TestEnsureLoadedConcurrency:
    """B2/P1-01：并发 ensure_loaded 合并为一次 load；失败允许重试。"""

    @pytest.mark.asyncio
    async def test_concurrent_load_single_repo_call(self):
        """两个并发 ensure_loaded → repo.load 只调一次，两个 caller 拿到同一已加载会话。"""
        repo = FakeSessionRepo()
        msg = UserMessage(message_id=MessageId("m1"), content=[TextPart(content="hello")])
        repo.sessions["conv_x"] = {
            "messages": [msg], "cs": CharacterState(session_id="conv_x"),
            "rs": MemoryReflectorState(), "ss": ServiceState(),
            "title": "x", "updated_at": "2026-08-04T00:00:00Z",
        }
        m = _make_manager(repo)

        async def call():
            return await m.ensure_loaded(SessionId("conv_x"))

        s1, s2 = await asyncio.gather(call(), call())

        assert s1 is s2
        assert SessionId("conv_x") in m._loaded  # noqa: SLF001
        assert repo.load_calls.count("conv_x") == 1  # 只 load 一次

    @pytest.mark.asyncio
    async def test_storage_unavailable_does_not_mark_loaded_and_allows_retry(self):
        """存储不可用（load 抛 StorageUnavailableError）→ 不标记 loaded；重试成功标记。"""
        from core.storage.base import StorageUnavailableError

        class FlakyRepo(FakeSessionRepo):
            def __init__(self):
                super().__init__()
                self._fail_once = True

            async def load(self, sid):
                if self._fail_once:
                    self._fail_once = False
                    raise StorageUnavailableError("simulated offline")
                return await super().load(sid)

        repo = FlakyRepo()
        repo.sessions["conv_y"] = {
            "messages": [], "cs": CharacterState(session_id="conv_y"),
            "rs": MemoryReflectorState(), "ss": ServiceState(),
            "title": "y", "updated_at": "2026-08-04T00:00:00Z",
        }
        m = _make_manager(repo)

        s1 = await m.ensure_loaded(SessionId("conv_y"))
        assert SessionId("conv_y") not in m._loaded  # noqa: SLF001 — 存储不可用不标记

        s2 = await m.ensure_loaded(SessionId("conv_y"))
        assert SessionId("conv_y") in m._loaded  # noqa: SLF001 — 重试成功
        assert s1 is s2


class TestDeleteDurable:
    """B3/P1-02+P1-03：删除前 drain 归档、离线删除不假成功、失败重开归档。"""

    @pytest.mark.asyncio
    async def test_delete_drains_archive_before_records(self):
        """慢 append 在途 → delete 先 drain 归档再清记录（无孤儿消息）。"""
        class SlowMsgRepo:
            def __init__(self):
                self.append_entered = asyncio.Event()
                self.release_append = asyncio.Event()
                self.deleted = asyncio.Event()

            async def append(self, sid, msg):
                self.append_entered.set()
                await self.release_append.wait()

            async def delete_by_session(self, sid):
                self.deleted.set()
                return 1

        msg_repo = SlowMsgRepo()
        repo = FakeSessionRepo()
        m = SessionManager(repo, msg_repo)
        await repo.create("conv_z", "t")
        session = await m.ensure_loaded("conv_z")
        session.add_message(UserMessage(message_id=MessageId("m1"),
                                        content=[TextPart(content="hi")]))
        await asyncio.wait_for(msg_repo.append_entered.wait(), 1.0)

        delete_task = asyncio.create_task(m.delete("conv_z"))
        await asyncio.sleep(0.05)
        assert not msg_repo.deleted.is_set()  # append 未完成前不得清记录

        msg_repo.release_append.set()
        await asyncio.wait_for(delete_task, 1.0)
        assert msg_repo.deleted.is_set()
        assert "conv_z" in repo.deleted

    @pytest.mark.asyncio
    async def test_delete_offline_raises_not_false_success(self):
        """存储不可用（repo._db=None）→ delete 抛 StorageUnavailableError，不假成功。"""
        from core.storage.base import StorageUnavailableError
        from core.storage.session_repo import SessionRepository

        repo = SessionRepository(db=None)
        m = SessionManager(repo, FakeMsgRepo())
        summary = await m.create(title="x")
        sid = summary.session_id

        with pytest.raises(StorageUnavailableError):
            await m.delete(sid)

        # tombstone 回滚：会话仍在缓存，可再删
        assert SessionId(sid) not in m._deleting  # noqa: SLF001
        assert SessionId(sid) in m._sessions  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_delete_failure_reopens_archive(self):
        """delete 中途失败 → 归档重新打开，后续消息可继续归档。"""
        class FailDeleteRepo(FakeSessionRepo):
            async def delete(self, sid):
                raise RuntimeError("simulated delete failure")

        msg_repo = MagicMock()
        msg_repo.delete_by_session = AsyncMock(return_value=0)
        repo = FailDeleteRepo()
        m = SessionManager(repo, msg_repo)
        await repo.create("conv_w", "t")
        session = await m.ensure_loaded("conv_w")

        with pytest.raises(RuntimeError):
            await m.delete("conv_w")

        # 归档已重开：再次 add_message 会创建归档任务
        session.add_message(UserMessage(message_id=MessageId("m1"),
                                        content=[TextPart(content="hi")]))
        assert len(session._archive_tasks) == 1  # noqa: SLF001


class TestRecoverPendingSessions:
    """B2/P2-01：启动扫描所有 pending JSONL（非活动也在内）。"""

    @pytest.mark.asyncio
    async def test_scans_all_pending_files(self, tmp_path):
        """多个 pending 文件全部恢复进内存缓存，并标记 loaded。"""
        m = _make_manager()
        p1 = tmp_path / "session_conv_p1.json"
        p2 = tmp_path / "session_conv_p2.json"
        msg = UserMessage(message_id=MessageId("m1"), content=[TextPart(content="hi")])
        doc1 = {"session_id": "conv_p1",
                "messages": [msg.model_dump(mode="json")],
                "character_state": {}, "reflector_state": {}, "service_state": {},
                "updated_at": "2026-08-04T00:00:00Z"}
        doc2 = {"session_id": "conv_p2",
                "messages": [], "character_state": {},
                "reflector_state": {}, "service_state": {},
                "updated_at": "2026-08-04T00:00:00Z"}
        p1.write_text(json.dumps(doc1), encoding="utf-8")
        p2.write_text(json.dumps(doc2), encoding="utf-8")

        recovered = await m.recover_pending_sessions(pending_dir=str(tmp_path))

        assert recovered == 2
        assert SessionId("conv_p1") in m._loaded  # noqa: SLF001
        assert SessionId("conv_p2") in m._loaded  # noqa: SLF001
        assert len(m.get("conv_p1").messages) == 1
        # 非活动会话对 has_session 可见（离线 list/detail）
        assert await m.has_session("conv_p1") is True

    @pytest.mark.asyncio
    async def test_corrupt_file_ignored(self, tmp_path):
        """损坏 JSON 忽略不计数，不阻断其他 pending 恢复。"""
        m = _make_manager()
        bad = tmp_path / "session_conv_bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        good = tmp_path / "session_conv_good.json"
        good.write_text(json.dumps({"session_id": "conv_good", "messages": [],
                                    "character_state": {}, "reflector_state": {},
                                    "service_state": {}, "updated_at": ""}),
                        encoding="utf-8")

        recovered = await m.recover_pending_sessions(pending_dir=str(tmp_path))

        assert recovered == 1
        assert SessionId("conv_good") in m._loaded  # noqa: SLF001
        assert SessionId("conv_bad") not in m._loaded  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_import_to_mongo_deletes_pending(self, tmp_path):
        """Mongo 可用时导入并删除 pending（save() durable 后删）。"""
        repo = FakeSessionRepo()
        m = _make_manager(repo)
        p = tmp_path / "session_conv_imp.json"
        p.write_text(json.dumps({"session_id": "conv_imp", "messages": [],
                                 "character_state": {}, "reflector_state": {},
                                 "service_state": {}, "updated_at": ""}),
                     encoding="utf-8")

        recovered = await m.recover_pending_sessions(pending_dir=str(tmp_path),
                                                     import_to_mongo=True)

        assert recovered == 1
        # Mongo durable（fake repo.save 成功）→ pending 文件已删除
        assert not p.exists()


@pytest.mark.asyncio
async def test_ensure_loaded_constructs_and_loads():
    """首次 ensure_loaded：构造 + load（fake repo 返回消息）。"""
    repo = FakeSessionRepo()
    msg = UserMessage(message_id=MessageId("m1"), content=[TextPart(content="hello")])
    repo.sessions["conv_a"] = {
        "messages": [msg],
        "cs": CharacterState(session_id="conv_a", mood="happy"),
        "rs": MemoryReflectorState(),
        "ss": ServiceState(),
        "title": "a",
        "updated_at": "2026-08-04T00:00:00Z",
    }
    m = _make_manager(repo)
    s = await m.ensure_loaded("conv_a")
    assert s.session_id == "conv_a"
    assert len(s.messages) == 1
    assert s.messages[0].message_id == "m1"
    assert s.character_state.mood == "happy"
    assert "conv_a" in repo.load_calls


@pytest.mark.asyncio
async def test_ensure_loaded_no_repeat_load():
    """重复 ensure_loaded 不重复 load。"""
    repo = FakeSessionRepo()
    repo.sessions["conv_b"] = {
        "messages": [], "cs": CharacterState(session_id="conv_b"),
        "rs": MemoryReflectorState(), "ss": ServiceState(),
        "title": "b", "updated_at": "2026-08-04T00:00:00Z",
    }
    m = _make_manager(repo)
    await m.ensure_loaded("conv_b")
    n = len(repo.load_calls)
    await m.ensure_loaded("conv_b")
    assert len(repo.load_calls) == n  # 未重复 load


# ───────────────────────── resolve 规则 ─────────────────────────


@pytest.mark.asyncio
async def test_resolve_v1_always_default():
    """protocol_version=1（<2）恒 default，忽略 conversation_id。"""
    m = _make_manager()
    s = await m.resolve("conv_x", 1)
    assert s.session_id == "default"
    s = await m.resolve(None, 0)
    assert s.session_id == "default"


@pytest.mark.asyncio
async def test_resolve_v2_none_returns_active():
    """protocol_version>=2 且 conversation_id=None → active。"""
    m = _make_manager()
    await m.ensure_loaded("default")
    await m.switch("conv_active")
    s = await m.resolve(None, 2)
    assert s.session_id == "conv_active"


@pytest.mark.asyncio
async def test_resolve_v2_explicit_conv():
    """protocol_version>=2 且显式 conversation_id → 该会话（已知会话）。"""
    m = _make_manager()
    summary = await m.create()
    s = await m.resolve(str(summary.session_id), 2)
    assert s.session_id == summary.session_id


@pytest.mark.asyncio
async def test_resolve_v2_explicit_unknown_raises_session_not_found():
    """P2-1：protocol_version>=2 且显式 conversation_id 未知/已删 → SessionNotFoundError。

    最终 resolver 严格化——禁止懒创建复活已删除会话（9A-3「已删除会话不得重建」）。
    """
    m = _make_manager()
    with pytest.raises(SessionNotFoundError):
        await m.resolve("conv_ghost", 2)
    assert "conv_ghost" not in m._sessions
    assert await m.has_session("conv_ghost") is False


@pytest.mark.asyncio
async def test_resolve_v2_explicit_deleted_raises_and_does_not_resurrect():
    """P2-1：已加载会话删除后显式 resolve → 抛错且不复活（缓存/持久层均无）。"""
    repo = FakeSessionRepo()
    msg_repo = FakeMsgRepo()
    m = _make_manager(repo, msg_repo)
    summary = await m.create(title="del-race")
    sid = str(summary.session_id)
    assert await m.has_session(sid) is True      # 入口检查通过
    await m.delete(sid)                          # 竞态窗口：删除
    assert await m.has_session(sid) is False
    with pytest.raises(SessionNotFoundError):
        await m.resolve(sid, 2)
    assert sid not in m._sessions                # 缓存无该会话（未复活）
    assert sid not in repo.sessions              # 持久层无该会话


@pytest.mark.asyncio
async def test_resolve_v2_explicit_default_still_ok():
    """P2-1：显式 "default" 不受严格化影响（default eager 缓存命中）。"""
    m = _make_manager()
    s = await m.resolve("default", 2)
    assert s.session_id == "default"


@pytest.mark.asyncio
async def test_reserved_work_blocks_delete_until_terminal_release():
    """accepted work 覆盖 EventBus/pending/in-flight 窗口，引用释放前删除必须 409 语义。"""
    m = _make_manager()
    summary = await m.create(title="leased")
    target = await m.reserve_work(summary.session_id, 2)
    assert target.session_id == summary.session_id
    assert m.work_count(summary.session_id) == 1

    with pytest.raises(SessionBusyError):
        await m.delete(summary.session_id)
    assert await m.has_session(summary.session_id) is True

    m.release_work(summary.session_id)
    assert m.work_count(summary.session_id) == 0
    await m.delete(summary.session_id)
    assert await m.has_session(summary.session_id) is False


@pytest.mark.asyncio
async def test_reserve_none_freezes_actual_active_session():
    """v2 缺省 conversationId 在 accepted 时固化，不随之后 active 切换而改投。"""
    m = _make_manager()
    summary = await m.create(title="active-at-ack")
    await m.switch(summary.session_id)
    reserved = await m.reserve_work(None, 2)
    await m.switch("default")
    assert reserved.session_id == summary.session_id
    assert m.work_count(summary.session_id) == 1
    assert m.work_count("default") == 0
    m.release_work(summary.session_id)


@pytest.mark.asyncio
async def test_delete_tombstone_blocks_new_work_during_before_delete():
    """附件扫描等删除前回调运行期间，tombstone 已生效，不能再接收新消息。"""
    m = _make_manager()
    summary = await m.create(title="deleting")
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def before_delete(_sid):
        entered.set()
        await finish.wait()

    task = asyncio.create_task(m.delete(summary.session_id, before_delete=before_delete))
    await entered.wait()
    assert await m.has_session(summary.session_id) is False
    with pytest.raises(SessionNotFoundError):
        await m.reserve_work(summary.session_id, 2)
    finish.set()
    await task
    assert await m.has_session(summary.session_id) is False


# ───────────────────────── has_session ─────────────────────────


@pytest.mark.asyncio
async def test_has_session_cache_hit():
    """已构造/已加载会话（缓存命中）→ True；default eager 命中。"""
    m = _make_manager()
    assert await m.has_session("default") is True
    m.get("conv_cached")  # get 已缓存（不 load）
    assert await m.has_session("conv_cached") is True


@pytest.mark.asyncio
async def test_has_session_checks_repo_for_unknown():
    """缓存未命中 → 查 repo：有 doc → True，无 → False（activate_session 404 判定）。"""
    repo = FakeSessionRepo()
    repo.sessions["conv_repo"] = {
        "messages": [], "cs": CharacterState(session_id="conv_repo"),
        "rs": MemoryReflectorState(), "ss": ServiceState(),
        "title": "r", "updated_at": "2026-08-04T00:00:00Z",
    }
    m = _make_manager(repo)
    assert await m.has_session("conv_repo") is True
    assert await m.has_session("conv_ghost") is False


# ───────────────────────── switch ─────────────────────────


@pytest.mark.asyncio
async def test_switch_swaps_active_and_returns_target():
    """switch：换 active_id + 返回目标实例。"""
    m = _make_manager()
    await m.ensure_loaded("default")
    target = await m.switch("conv_switch")
    assert m.active_id == "conv_switch"
    assert m.active is target


@pytest.mark.asyncio
async def test_switch_no_attach():
    """M3: bind_character_engine 已删, switch 不再 attach_session / _pre_save_hook。

    switch 只换 active 指针；instance-scoped 状态经 runtime 权威，session 切换不重绑 engine。
    """
    m = _make_manager()
    # M3: bind_character_engine 已删, switch 不再 attach_session
    target = await m.switch("conv_eng")
    assert target is not None and m.active_id == "conv_eng"


@pytest.mark.asyncio
async def test_switch_rejects_when_busy_check_set():
    """F1(a): switch 内部 busy check——忙（生成/TTS 中）→ 抛 SessionBusyError，不切换。"""
    m = _make_manager()
    m.set_busy_check(lambda: True)
    with pytest.raises(SessionBusyError):
        await m.switch("conv_busy")
    assert m.active_id == "default"


@pytest.mark.asyncio
async def test_switch_allows_when_busy_check_false():
    """F1(a): busy check 为 False（空闲）→ switch 正常执行。"""
    m = _make_manager()
    m.set_busy_check(lambda: False)
    target = await m.switch("conv_ok")
    assert m.active_id == "conv_ok"
    assert m.active is target


# ───────────────────────── create / rename / delete ─────────────────────────


@pytest.mark.asyncio
async def test_create_generates_conv_id_and_persists():
    """create：conv_ 前缀 ID + 空会话落库 + 返回 SessionSummary。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo)
    summary = await m.create(title="hello")
    assert summary.session_id.startswith("conv_")
    assert summary.title == "hello"
    assert summary.message_count == 0
    assert len(summary.session_id) == 5 + 12  # "conv_" + uuid4 hex[:12]
    assert repo.created == [(summary.session_id, "hello")]
    assert repo.sessions[summary.session_id]["messages"] == []
    # 创建后可 ensure_loaded 为空会话
    s = await m.ensure_loaded(summary.session_id)
    assert s.session_id == summary.session_id
    assert len(s.messages) == 0


@pytest.mark.asyncio
async def test_create_title_optional_and_not_active():
    """create 无 title → 空串；新会话非 active。"""
    m = _make_manager()
    summary = await m.create()
    assert summary.title == ""
    assert summary.active is False


@pytest.mark.asyncio
async def test_create_persists_and_caches_normalized_character_binding():
    repo = FakeSessionRepo()
    manager = _make_manager(repo)
    summary = await manager.create(
        title="Cyrene",
        character_binding={
            "characterId": "chr_builtin_cyrene",
            "instanceId": "chi_builtin_cyrene",
            "ignored": True,
        },
    )
    expected = {
        "character_id": "chr_builtin_cyrene",
        "instance_id": "chi_builtin_cyrene",
    }
    assert summary.character_binding == expected
    assert repo.sessions[summary.session_id]["cb"] == expected
    assert manager.get(summary.session_id).character_binding == expected

    listed, _, _ = await manager.list()
    item = next(s for s in listed if s.session_id == summary.session_id)
    assert item.character_binding == expected


@pytest.mark.asyncio
async def test_create_offline_repo_visible_to_has_session():
    """P1-1：MongoDB 离线（repo._db=None）create → 新会话立即对 has_session 可见。

    create() 追加 ensure_loaded 把新会话放入内存缓存 + _loaded——离线降级语义下
    REST 创建成功后 text/附件/voice 携带该 conversationId 不再被严格检查判为幽灵。
    """
    repo = SessionRepository(db=None)  # 离线：create 静默不落库
    m = _make_manager(repo, FakeMsgRepo())
    summary = await m.create(title="offline")
    assert await m.has_session(summary.session_id) is True   # 原缺陷：False
    assert str(summary.session_id) in m._sessions            # 已入内存缓存
    assert await m.has_session("conv_ghost") is False        # 真正未知仍 False


@pytest.mark.asyncio
async def test_offline_create_list_and_rename_are_read_after_write_consistent():
    """Mongo 离线：create 后 list/detail 摘要可见，rename 成功并立即读到新标题。"""
    repo = SessionRepository(db=None)
    m = _make_manager(repo, FakeMsgRepo())
    summary = await m.create(title="离线标题")

    items, has_more, cursor = await m.list()
    assert has_more is False and cursor is None
    by_id = {item.session_id: item for item in items}
    # clean-start（BE-P1-05）：default 恒有本地摘要，离线时同样可见
    assert summary.session_id in by_id
    assert SessionId("default") in by_id
    assert by_id[summary.session_id].title == "离线标题"

    assert await m.rename(summary.session_id, "离线改名") is True
    detail = await m.get_summary(summary.session_id)
    assert detail is not None
    assert detail.title == "离线改名"
    items, _, _ = await m.list(search="改名")
    assert [item.session_id for item in items] == [summary.session_id]


@pytest.mark.asyncio
async def test_rename_updates_title():
    """rename：落库 title 更新；不存在返回 False。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo)
    summary = await m.create(title="old")
    ok = await m.rename(summary.session_id, "new-title")
    assert ok is True
    assert repo.sessions[summary.session_id]["title"] == "new-title"
    assert await m.rename("conv_missing", "x") is False


@pytest.mark.asyncio
async def test_delete_drops_cache_archive_and_active_fallback():
    """delete：msg_repo.delete_by_session + 丢弃缓存 + active 回落 default。"""
    repo = FakeSessionRepo()
    msg_repo = FakeMsgRepo()
    m = _make_manager(repo, msg_repo)
    summary = await m.create(title="del-me")
    sid = summary.session_id
    s = await m.ensure_loaded(sid)
    s.add_message(UserMessage(message_id=MessageId("m1"), content=[TextPart(content="x")]))
    await asyncio.sleep(0)  # 让 fire-and-forget 归档 task 完成
    await m.switch(sid)
    assert m.active_id == sid
    n = await m.delete(sid)
    assert n == 1  # 归档中 1 条消息被删
    assert sid in msg_repo.deleted
    assert sid in repo.deleted
    assert sid not in repo.sessions
    # 已丢缓存 + tombstone：删除后 get/resolve 都不得再懒创建复活。
    assert sid not in m._sessions
    with pytest.raises(SessionNotFoundError):
        m.get(sid)
    assert m.active_id == "default"


@pytest.mark.asyncio
async def test_delete_default_rejected():
    """删除 default 会话被拒绝：返回 0，default 仍在缓存池且 active 不变（review 修复）。"""
    m = _make_manager()
    await m.ensure_loaded("default")
    n = await m.delete("default")
    assert n == 0  # 拒绝删除
    assert m.active_id == "default"
    assert "default" in m._sessions
    assert m.get("default").session_id == "default"
    assert m.active.session_id == "default"


@pytest.mark.asyncio
async def test_delete_active_conv_rebinds_active_to_default():
    """删除活动会话：active 回落 default。M3 不再重绑 engine（attach_session 已删）。"""
    m = _make_manager()
    await m.ensure_loaded("default")
    summary = await m.create(title="del-active")
    await m.switch(summary.session_id)
    assert m.active_id == summary.session_id
    n = await m.delete(summary.session_id)
    assert n == 0
    assert m.active_id == "default"
    assert m.active.session_id == "default"


# ───────────────────────── list ─────────────────────────


@pytest.mark.asyncio
async def test_list_returns_items_has_more_cursor():
    """list：返回 (items, has_more, next_cursor)，keyset 游标无重叠。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo)
    for i in range(60):
        await m.create(title=f"session-{i}")
    items, has_more, cursor = await m.list(limit=50)
    assert len(items) == 50
    assert has_more is True
    assert cursor is not None
    items2, has_more2, cursor2 = await m.list(limit=50, cursor=cursor)
    # 60 conv + default（clean-start 恒可见）= 61 → 第二页 11 条
    assert len(items2) == 11
    assert has_more2 is False
    assert cursor2 is None
    ids1 = {s.session_id for s in items}
    ids2 = {s.session_id for s in items2}
    assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_list_fills_active_flag():
    """list items 填 active（按 active_id 比较）。"""
    m = _make_manager()
    await m.create(title="a")
    summary = await m.create(title="b")
    await m.switch(summary.session_id)
    items, _, _ = await m.list()
    actives = [s for s in items if s.active]
    assert len(actives) == 1
    assert actives[0].session_id == summary.session_id


@pytest.mark.asyncio
async def test_list_search_title_case_insensitive():
    """search 仅匹配 title，大小写不敏感；不匹配消息文本。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo)
    await m.create(title="AlphaBeta")
    await m.create(title="gamma")
    # 预置一条消息到某个会话，验证搜索不混消息文本
    summary = await m.create(title="plain-title")
    s = await m.ensure_loaded(summary.session_id)
    s.add_message(UserMessage(message_id=MessageId("m-search"), content=[TextPart(content="needle")]))
    await asyncio.sleep(0)
    items, _, _ = await m.list(search="alphabeta")
    assert [x.title for x in items] == ["AlphaBeta"]
    items, _, _ = await m.list(search="needle")
    assert items == []  # 消息文本不参与搜索
    items, _, _ = await m.list(search="")
    assert len(items) == 4  # 3 个创建会话 + default（clean-start 恒可见）


@pytest.mark.asyncio
async def test_list_sort_updated_at_desc_then_session_id_asc():
    """排序：updated_at DESC，同 updated_at 时 session_id ASC tiebreak。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo)
    # 手动写入同 updated_at 的两个会话（模拟 tiebreak 场景）
    ts = "2026-08-04T00:00:00Z"
    repo.sessions["conv_zz"] = {"messages": [], "cs": None, "rs": None, "ss": None,
                                "title": "z", "updated_at": ts}
    repo.sessions["conv_aa"] = {"messages": [], "cs": None, "rs": None, "ss": None,
                                "title": "a", "updated_at": ts}
    repo.sessions["conv_new"] = {"messages": [], "cs": None, "rs": None, "ss": None,
                                 "title": "new", "updated_at": "2026-08-04T00:00:01Z"}
    items, _, _ = await m.list()
    # default 本地摘要 updated_at=now（新鲜）→ 排序最前；其余同 2026-08-04 时间序
    assert [s.session_id for s in items] == ["default", "conv_new", "conv_aa", "conv_zz"]


# ───────────────────────── evict / save_all ─────────────────────────


@pytest.mark.asyncio
async def test_evict_drops_lru_non_active_and_saves():
    """evict：超 max_sessions 逐出最久未用非活动会话并 save。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo, max_sessions=3)
    for sid in ("a", "b", "c", "d"):
        await m.ensure_loaded(sid)   # 访问顺序 a,b,c,d → a 最久未用
    await m.switch("c")               # c 成为活动（也被移至最近）
    m.evict_if_needed()
    await asyncio.sleep(0)            # 排空的 fire-and-forget save task
    assert len(m._sessions) == 3
    assert m.active_id == "c"
    assert "c" in m._sessions
    assert "a" not in m._sessions     # LRU 非活动被逐出
    assert "a" in repo.save_calls     # 逐出前已 save


@pytest.mark.asyncio
async def test_evict_preserves_active():
    """evict 永不逐出活动会话。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo, max_sessions=2)
    for sid in ("a", "b", "c"):
        await m.ensure_loaded(sid)
    await m.switch("a")
    m.evict_if_needed()
    await asyncio.sleep(0)
    assert m.active_id == "a"
    assert "a" in m._sessions


@pytest.mark.asyncio
async def test_get_auto_evicts_when_over_capacity():
    """F7: get() 构造新会话后自动 evict——超 max_sessions 时最旧非活动被逐出（不再无界增长）。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo, max_sessions=2)
    await m.ensure_loaded("a")      # {default, a} 未超
    assert "a" in m._sessions
    await m.ensure_loaded("b")      # {default, a, b} 超 → 自动逐出最旧非活动 a
    await asyncio.sleep(0)          # 排空 fire-and-forget save
    assert "a" not in m._sessions
    assert "b" in m._sessions
    assert "default" in m._sessions  # default 锚点不被逐出
    assert "a" in repo.save_calls    # 逐出前已 save


@pytest.mark.asyncio
async def test_evict_never_evicts_default():
    """F7: evict 永不逐出 default（默认锚点会话，与 delete 守卫同主题）。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo, max_sessions=2)
    await m.ensure_loaded("a")
    await m.ensure_loaded("b")
    await asyncio.sleep(0)
    assert "default" in m._sessions
    assert m.active_id == "default"


@pytest.mark.asyncio
async def test_save_all_saves_loaded_sessions():
    """save_all：持久化所有已加载会话。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo)
    await m.ensure_loaded("default")
    await m.ensure_loaded("conv_save")
    await m.save_all()
    assert set(repo.save_calls) == {"default", "conv_save"}


# ───────────────────────── lifecycle passthrough ─────────────────────────


@pytest.mark.asyncio
async def test_connect_db_and_health_check_delegate():
    """connect_db / health_check 委托给 repo。"""
    repo = FakeSessionRepo()
    m = _make_manager(repo)
    assert await m.connect_db() is True
    assert await m.health_check() is True
    m._repo._connected = False
    assert await m.health_check() is False


@pytest.mark.asyncio
async def test_load_active_loads_default():
    """load_active：加载当前活动（default）会话。"""
    m = _make_manager()
    await m.load_active()
    assert m.active.session_id == "default"


class TestMemoryReflectorBinding:
    """B6/P2-01：reflector 挂到所有（含未来）会话——反思状态可持久化。"""

    @pytest.mark.asyncio
    async def test_bind_applies_to_current_and_future_sessions(self):
        m = _make_manager()
        await m.ensure_loaded("conv_r1")
        reflector = MagicMock()

        m.bind_memory_reflector(reflector)

        # 已存在的会话
        assert m.get("conv_r1")._memory_reflector is reflector  # noqa: SLF001
        # 未来新建会话（get 懒构造）
        assert m.get("conv_r2")._memory_reflector is reflector  # noqa: SLF001


@pytest.mark.asyncio
async def test_reopen_all_archives_after_drain():
    """review 复核 #3：stop() 经 drain_all_archives 关闭全部归档，start() 必须
    reopen_all_archives 重开——否则重启后新消息只在内存、不写 MessageRepository。"""
    msg_repo = MagicMock()
    msg_repo.append = AsyncMock()
    sm = SessionManager(SessionRepository(db=MagicMock()), msg_repo)
    session = sm.get("default")

    def _msg(mid):
        return UserMessage(message_id=MessageId(mid), content=[TextPart(content="hi")])

    session.add_message(_msg("m1"))
    await asyncio.sleep(0.05)
    assert msg_repo.append.call_count == 1

    await sm.drain_all_archives()            # stop 路径：关闭全部归档入口
    session.add_message(_msg("m2"))
    await asyncio.sleep(0.05)
    assert msg_repo.append.call_count == 1   # 关闭后不再归档

    sm.reopen_all_archives()                 # start 路径：重开全部归档入口
    session.add_message(_msg("m3"))
    await asyncio.sleep(0.05)
    assert msg_repo.append.call_count == 2   # 重开后恢复归档


# ───────────────────────── review 复核 #5 D2：提交点前置 ─────────────────────────


@pytest.mark.asyncio
async def test_delete_commit_first_cleanup_failure_journals(tmp_path):
    """review 复核 #5 D2：会话 doc 删除（提交点）成功后，附件释放失败**不阻塞删除**
    ——登记待清理 journal，retry_pending_cleanup 重试成功即清除。"""
    from core.storage.base import StorageUnavailableError

    m = _make_manager(pending_cleanup_file=tmp_path / "cleanup.json")
    summary = await m.create(title="to-delete")
    sid = summary.session_id

    async def _fail_cleanup(_sid):
        raise StorageUnavailableError("offline")

    m.set_attachment_cleanup(_fail_cleanup)
    n = await m.delete(sid)

    assert await m.has_session(sid) is False   # doc 已删——用户要的删除生效
    assert n == 0                              # 清理未完成，未计归档删除数
    assert sid in m._pending_cleanup           # noqa: SLF001 — 已登记待清理
    assert (tmp_path / "cleanup.json").is_file()

    m.set_attachment_cleanup(None)             # 存储恢复后重试
    await m.retry_pending_cleanup()
    assert sid not in m._pending_cleanup       # noqa: SLF001 — 重试成功清除
    assert not (tmp_path / "cleanup.json").exists()  # journal 清空


@pytest.mark.asyncio
async def test_delete_doc_failure_rolls_back_before_cleanup(tmp_path):
    """review 复核 #5 D2：提交点（doc 删除）失败 → 回滚（重开归档 + 释放 tombstone），
    附件 cleanup 不执行——不再出现「doc 存活但 refs/归档已被清空」。"""
    from core.storage.base import StorageUnavailableError

    m = _make_manager(pending_cleanup_file=tmp_path / "cleanup.json")
    summary = await m.create(title="to-delete")
    sid = summary.session_id
    cleaned: list = []

    async def _cleanup(s):
        cleaned.append(s)

    m.set_attachment_cleanup(_cleanup)
    original_delete = m._repo.delete  # noqa: SLF001

    async def _fail_delete(s):
        raise StorageUnavailableError("offline")

    m._repo.delete = _fail_delete  # noqa: SLF001
    with pytest.raises(StorageUnavailableError):
        await m.delete(sid)

    assert cleaned == []                       # cleanup 未执行——doc 未删前不破坏数据
    assert sid not in m._deleting              # noqa: SLF001 — tombstone 已释放
    assert sid not in m._pending_cleanup       # noqa: SLF001 — 未登记（提交未发生）
    assert await m.has_session(sid) is True    # doc 仍在，可安全重试
    m._repo.delete = original_delete  # noqa: SLF001
