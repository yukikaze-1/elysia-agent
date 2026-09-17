# 测试文件：core/memory/tests/test_recovery_reload.py
"""恢复路径的补偿动作（CR-05 / CR-06）。

CR-05：Milvus 启动时离线 → 运行中恢复时走的是 `repo.health_check()`（repo 内部
重连），**绕过 `connect()`**，而 `_load_from_milvus()` 只在 `connect()` 成功分支
调用 → `_entries` 永远为空。而候选召回只能由内存条目产生（`_search_milvus_candidates`
按 `_milvus_pk` 回连内存条目）→ ANN 命中的行被整体丢弃 → `search()` **恒返回 []**，
角色静默失忆到进程重启。

CR-06：`load()` 命中文档时整体替换内存、从不与离线期 pending JSONL 合并；随后一次
成功 `save()` 又删除该文件 → 消息在 sessions / message_archive / JSONL
**三处同时消失**。
"""


import pytest

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# CR-05：Milvus 恢复后重载条目
# ═══════════════════════════════════════════════════════════════════

class _FakeMilvusRepo:
    """最小 Milvus repo 替身：可切换连接状态，query 返回预置行。"""

    DIMENSION = 4

    def __init__(self, *, connected: bool, rows: list[dict]):
        self.connected = connected
        self._rows = rows
        self.query_calls = 0

    async def connect(self) -> bool:
        return self.connected

    async def health_check(self) -> bool:
        return self.connected

    async def query(self, **kwargs) -> list[dict]:
        self.query_calls += 1
        return list(self._rows) if self.connected else []


def _make_ltm(repo):
    from core.memory.long_term import LongTermMemory
    return LongTermMemory(repo=repo, max_entries=100)


@pytest.mark.asyncio
async def test_search_is_empty_when_started_offline_even_after_recovery():
    """复现：启动离线 → Milvus 恢复 → 未重载时 search() 恒为空（缺陷原状）。

    这条是**特征化测试**：它断言缺陷状态本身，用来证明下面那条修复真的改变了
    行为（否则"修好了"无法与"本来就好"区分）。
    """
    rows = [{"id": 1, "entry_id": "m1", "text": "用户养了一只猫叫橘子",
             "memory_type": "Fact", "poignancy": 5, "timestamp": 1}]
    repo = _FakeMilvusRepo(connected=False, rows=rows)
    ltm = _make_ltm(repo)
    assert await ltm.connect() is False
    assert ltm._entries == []                      # noqa: SLF001

    repo.connected = True
    assert await ltm.health_check() is True        # repo 自愈，但…
    assert await ltm.search("猫", query_embedding=[0.1] * 4) == []
    assert ltm._entries == [], "health_check 不会重载条目——这正是 CR-05"  # noqa: SLF001


@pytest.mark.asyncio
async def test_reload_from_milvus_restores_recall():
    """修复：reload_from_milvus() 之后召回恢复。"""
    rows = [{"id": 1, "entry_id": "m1", "text": "用户养了一只猫叫橘子",
             "memory_type": "Fact", "poignancy": 5, "timestamp": 1}]
    repo = _FakeMilvusRepo(connected=False, rows=rows)
    ltm = _make_ltm(repo)
    await ltm.connect()

    repo.connected = True
    assert await ltm.reload_from_milvus() == 1
    assert len(ltm._entries) == 1                  # noqa: SLF001

    hits = await ltm.search("猫", query_embedding=[0.1] * 4)
    assert [e.id for e in hits] == ["m1"], "重载后 ANN 命中才能回连到内存条目"


@pytest.mark.asyncio
async def test_reload_is_idempotent():
    """幂等：重复重载不重复 append（entry_id 去重）。"""
    rows = [{"id": 1, "entry_id": "m1", "text": "事实", "memory_type": "Fact",
             "poignancy": 5, "timestamp": 1}]
    repo = _FakeMilvusRepo(connected=True, rows=rows)
    ltm = _make_ltm(repo)
    await ltm.connect()
    assert len(ltm._entries) == 1                  # noqa: SLF001
    assert await ltm.reload_from_milvus() == 0
    assert len(ltm._entries) == 1                  # noqa: SLF001


@pytest.mark.asyncio
async def test_empty_recall_warns_once(caplog):
    """可观测性：内存空 + repo 存在 → 告警一次（不刷屏），重载成功后重新武装。"""
    import logging
    rows = [{"id": 1, "entry_id": "m1", "text": "事实", "memory_type": "Fact",
             "poignancy": 5, "timestamp": 1}]
    repo = _FakeMilvusRepo(connected=False, rows=rows)
    ltm = _make_ltm(repo)
    await ltm.connect()
    repo.connected = True

    with caplog.at_level(logging.WARNING, logger="core.memory.long_term"):
        for _ in range(3):
            await ltm.search("x", query_embedding=[0.1] * 4)

    hits = [r for r in caplog.records if "召回将恒为空" in r.getMessage()]
    assert len(hits) == 1, f"应只告警一次，实得 {len(hits)}"


@pytest.mark.asyncio
async def test_recovery_hook_reloads_ltm(caplog):
    """接线：`_recover_dependency("ltm")` 必须调用 reload（不只是 sync_dirty）。"""
    import logging
    from unittest.mock import AsyncMock, MagicMock

    from core.agent._lifecycle_persistence import _LifecyclePersistence

    agent = MagicMock()
    agent._long_term.reload_from_milvus = AsyncMock(return_value=3)
    agent._long_term.sync_dirty_entries = AsyncMock(return_value=0)

    with caplog.at_level(logging.INFO, logger="core.agent._lifecycle_persistence"):
        ok = await _LifecyclePersistence._recover_dependency(agent, "ltm")

    assert ok is True
    assert agent._long_term.reload_from_milvus.await_count == 1
    assert any("重载 3 条记忆" in r.getMessage() for r in caplog.records), caplog.text


# ═══════════════════════════════════════════════════════════════════
# CR-06：Mongo 恢复不得吞掉离线期对话
# ═══════════════════════════════════════════════════════════════════

class _FakeSessionRepo:
    """session repo 替身：可切换可用性；save 捕获真实载荷供 load 回放。

    **不手搓 CharacterState**：直接从一次真实 `save()` 的 kwargs 里取
    character_state/reflector_state/service_state/binding，避免测试固化的
    document 形状与生产写出的形状不一致（那正是本项目反复出现的缺陷家族）。
    """

    def __init__(self, *, available: bool):
        self.available = available
        self.doc = None
        self.saves: list[list] = []

    async def load(self, sid):
        from core.storage.base import StorageUnavailableError
        if not self.available:
            raise StorageUnavailableError("mongo down")
        return self.doc

    async def save(self, sid, messages, **kwargs):
        from core.storage.base import StorageUnavailableError
        if not self.available:
            raise StorageUnavailableError("mongo down")
        self.saves.append(list(messages))
        self.doc = (
            list(messages),
            kwargs.get("character_state"),
            kwargs.get("reflector_state"),
            kwargs.get("service_state"),
            kwargs.get("character_binding"),
        )
        return True


def _msg(mid: str, text: str):
    """构造一条消息（content 是 Part 列表，不是裸 str）。"""
    from core.schema import AssistantMessage, MessageId, TextPart, UserMessage
    cls = UserMessage if text.startswith("u:") else AssistantMessage
    return cls(message_id=MessageId(mid),
               content=[TextPart(content=text)], timestamp="2026-01-01T00:00:00Z")


def _session(tmp_path, repo):
    from core.memory.session import SessionMemory
    s = SessionMemory(session_id="default", repo=repo, max_messages=50)
    s._pending_file = str(tmp_path / "session_default.jsonl")   # noqa: SLF001
    return s


def _pending(tmp_path) -> "pathlib.Path":
    from pathlib import Path
    return Path(tmp_path) / "session_default.jsonl"


@pytest.mark.asyncio
async def test_offline_messages_survive_mongo_recovery(tmp_path):
    """复现 + 修复：离线期消息在 Mongo 恢复后必须仍在内存里。

    原状：`load()` 命中文档 → 整体替换 → 离线期消息消失；下一次 save() durable
    又删掉 JSONL（唯一副本）。
    """
    repo = _FakeSessionRepo(available=True)
    s = _session(tmp_path, repo)

    # 前提：远端已有一段历史
    s.add_message(_msg("m0", "u: 早上好"))
    assert await s.save() is True
    assert repo.doc is not None

    # Mongo 离线：这段对话只落在 JSONL
    repo.available = False
    s.add_message(_msg("m1", "u: 离线期的问题"))
    s.add_message(_msg("m2", "离线期的回答"))
    assert await s.save() is True
    assert _pending(tmp_path).exists()

    # Mongo 恢复
    repo.available = True
    assert await s.load() is True
    ids = [m.message_id for m in s.messages]       # noqa: SLF001
    assert "m0" in ids, "远端历史必须保留"
    assert "m1" in ids and "m2" in ids, f"离线期消息被覆盖丢失：{ids}"


@pytest.mark.asyncio
async def test_save_keeps_pending_when_file_has_unsaved_messages(tmp_path):
    """save() 只在"文件里的消息都已在内存中"时才删除本地副本（CR-06 第二道）。"""
    repo = _FakeSessionRepo(available=True)
    s = _session(tmp_path, repo)

    repo.available = False
    s.add_message(_msg("m0", "u: 早上好"))
    s.add_message(_msg("m1", "u: 只在文件里的消息"))
    assert await s.save() is True
    assert _pending(tmp_path).exists()

    # 内存被换成"不含 m1"的状态（模拟一条绕过合并的路径），Mongo 却已可用
    repo.available = True
    s._messages = type(s._messages)([_msg("m0", "u: 早上好")], maxlen=50)  # noqa: SLF001
    assert await s.save() is True
    assert _pending(tmp_path).exists(), "文件里仍有未落库消息时**不得**删除本地副本"

    # 文件里的消息都进内存后，才允许清理
    s._messages.append(_msg("m1", "u: 只在文件里的消息"))       # noqa: SLF001
    assert await s.save() is True
    assert not _pending(tmp_path).exists(), "已在内存中 → 允许清理冗余副本"


@pytest.mark.asyncio
async def test_load_without_pending_file_behaves_as_before(tmp_path):
    """回归网：没有 pending 文件时 load 行为不变（不无谓合并）。"""
    repo = _FakeSessionRepo(available=True)
    s = _session(tmp_path, repo)
    s.add_message(_msg("m0", "u: 早上好"))
    assert await s.save() is True

    s2 = _session(tmp_path, repo)
    assert await s2.load() is True
    assert [m.message_id for m in s2.messages] == ["m0"]        # noqa: SLF001
