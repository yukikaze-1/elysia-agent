"""SessionManager 测试 — 缓存池 + 活动指针 + resolve/switch/CRUD/list/evict。

批次 3 (multi-session) 核心组件。fake repo 实现 Task 4 brief 规定的方法集，
验证 manager 逻辑（不连真实 MongoDB）。
"""

import asyncio
import pytest

from core.memory.session import SessionMemory
from core.memory.session_manager import SessionManager
from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.message import UserMessage
from core.schema.part import TextPart
from core.schema.service_state import ServiceState
from core.schema.types import MessageId, SessionId
from core.storage.session_repo import SessionSummary


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
                   character_state=None, reflector_state=None, service_state=None):
        self.save_calls.append(session_id)
        doc = self.sessions.setdefault(session_id, {})
        doc["messages"] = list(messages)
        doc["cs"] = character_state
        doc["rs"] = reflector_state
        doc["ss"] = service_state
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
                doc.get("ss") or ServiceState())

    async def create(self, sid, title=""):
        self.created.append((sid, title))
        self.sessions[sid] = {"messages": [], "title": title,
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


class FakeEngine:
    """记录 attach_session 调用的 fake character engine。"""

    def __init__(self):
        self.attached: list = []

    def attach_session(self, session) -> None:
        self.attached.append(session)


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
    """protocol_version>=2 且显式 conversation_id → 该会话。"""
    m = _make_manager()
    s = await m.resolve("conv_specific", 2)
    assert s.session_id == "conv_specific"


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
async def test_switch_calls_outgoing_pre_save_hook():
    """switch 先调 outgoing._pre_save_hook()（同步 reflector buffer）。"""
    m = _make_manager()
    await m.ensure_loaded("default")
    calls = []
    m.active._pre_save_hook = lambda: calls.append("outgoing")
    await m.switch("conv_hook")
    assert calls == ["outgoing"]


@pytest.mark.asyncio
async def test_switch_attaches_character_engine():
    """switch 后 bind 的 engine 对 target 调用 attach_session。"""
    m = _make_manager()
    engine = FakeEngine()
    m.bind_character_engine(engine)
    target = await m.switch("conv_eng")
    assert engine.attached[-1] is target


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
    # 已丢缓存：删除后重新 get 得到的是新实例（非删除前实例 s）
    assert sid not in m._sessions
    assert m.get(sid) is not s
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
async def test_delete_active_conv_rebinds_engine_to_default():
    """删除活动会话：active 回落 default + 重绑 engine（attach_session(default)）。"""
    m = _make_manager()
    engine = FakeEngine()
    m.bind_character_engine(engine)
    await m.ensure_loaded("default")
    summary = await m.create(title="del-active")
    await m.switch(summary.session_id)
    assert m.active_id == summary.session_id
    n = await m.delete(summary.session_id)
    assert n == 0
    assert m.active_id == "default"
    assert engine.attached[-1] is m.get("default")  # 重绑到 default，非孤儿 state


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
    assert len(items2) == 10
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
    assert len(items) == 3


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
    assert [s.session_id for s in items] == ["conv_new", "conv_aa", "conv_zz"]


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
