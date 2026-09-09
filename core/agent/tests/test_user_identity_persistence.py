"""BE-STAGEH-02：用户身份必须跨服务进程重启保持（Mongo 权威）。

真实联调复现：只重启后端进程、不清数据库，`GET /api/me` 回到
`initialized=false / revision=1 / defaultCharacterId=null`——角色「不再认识我」。
本文件用同一份假 Mongo 数据跨两个 Agent 实例验证重启后档案与 revision 不变。
"""

import os

import pytest

from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

os.environ.setdefault("DEEPSEEK_API_KEY", "test")

from core.agent.agent import Agent          # noqa: E402
from core.config import AppConfig           # noqa: E402

pytestmark = pytest.mark.service


class _DurableFakeDB(FakeDocumentDBBackend):
    """模拟「进程退出但数据仍在 Mongo」：close() 不清库。"""

    async def close(self) -> None:  # noqa: D102
        pass


def _make_agent(db: FakeDocumentDBBackend) -> Agent:
    agent = Agent(config=AppConfig())
    agent._user_identity_repo._db = db          # noqa: SLF001
    agent._user_identity_repo._injected_db = True  # noqa: SLF001
    return agent


@pytest.mark.asyncio
async def test_user_identity_survives_agent_restart():
    db = _DurableFakeDB()

    agent_a = _make_agent(db)
    await agent_a.start()
    try:
        store_a = agent_a._user_identity_store  # noqa: SLF001
        assert store_a.get() is not None and store_a.get().revision == 1  # type: ignore[union-attr]
        updated = await store_a.update_if_revision(
            1, {"displayName": "妖梦", "preferredAddress": "主人",
                "pronouns": "她", "initialized": True})
        assert updated is not None
        assert updated.revision == 2
    finally:
        await agent_a.stop()

    agent_b = _make_agent(db)
    await agent_b.start()
    try:
        loaded = agent_b._user_identity_store.get()  # noqa: SLF001
        assert loaded is not None
        assert loaded.displayName == "妖梦"
        assert loaded.preferredAddress == "主人"
        assert loaded.pronouns == "她"
        assert loaded.initialized is True
        assert loaded.revision == 2          # 不回到 1
    finally:
        await agent_b.stop()


@pytest.mark.asyncio
async def test_second_agent_does_not_reseed_bootstrap_over_existing_profile():
    """并发/重复启动不得覆盖已有档案（setOnInsert 唯一 bootstrap）。"""
    db = _DurableFakeDB()

    agent_a = _make_agent(db)
    await agent_a.start()
    try:
        await agent_a._user_identity_store.update_if_revision(  # noqa: SLF001
            1, {"displayName": "妖梦", "initialized": True})
    finally:
        await agent_a.stop()

    agent_b = _make_agent(db)
    await agent_b.start()
    try:
        loaded = agent_b._user_identity_store.get()  # noqa: SLF001
        assert loaded is not None
        assert loaded.displayName == "妖梦"
        assert loaded.revision == 2
    finally:
        await agent_b.stop()


@pytest.mark.asyncio
async def test_restarted_agent_uses_persisted_identity_in_turn_prompt():
    """P0-2：重启后新一轮 Prompt 使用 Mongo 中持久化的身份（称呼/代词/revision）。

    证据链：Agent A 更新档案 → 停止 → Agent B 用同一份数据启动 → _prepare_turn
    冻结本轮快照 → 真实 PromptAssembler 渲染出的 system prompt 含持久化称呼。
    """
    db = _DurableFakeDB()

    agent_a = _make_agent(db)
    await agent_a.start()
    try:
        updated = await agent_a._user_identity_store.update_if_revision(  # noqa: SLF001
            1, {"displayName": "妖梦", "preferredAddress": "主人",
                "pronouns": "她", "timezone": "Asia/Shanghai", "initialized": True})
        assert updated is not None and updated.revision == 2
    finally:
        await agent_a.stop()

    agent_b = _make_agent(db)
    await agent_b.start()
    try:
        loop = agent_b.cognitive
        setup = await loop._prepare_turn(  # noqa: SLF001
            user_message="你好", user_name="启动默认名", stt_emotion="",
            content=None, message_id=None, conversation_id=None,
            protocol_version=1)
        assert setup is not None
        try:
            ctx = loop._turn_context  # noqa: SLF001
            assert ctx is not None
            assert ctx.userDisplayName == "妖梦"
            assert ctx.userPreferredAddress == "主人"
            assert ctx.userPronouns == "她"
            assert ctx.userTimezone == "Asia/Shanghai"
            assert ctx.userIdentityRevision == 2
            assert ctx.effectiveUserAddress == "主人"

            rendered = await loop._prompt.build_system_prompt(  # noqa: SLF001
                user_message="你好", user_name="启动默认名")
            assert "关于 主人：" in rendered
            assert "启动默认名" not in rendered
        finally:
            setup.runtime.end_generation()
    finally:
        await agent_b.stop()


@pytest.mark.asyncio
async def test_offline_startup_keeps_identity_unavailable_and_fails_closed():
    """P0-3：Mongo 离线启动不伪造身份——get() 为 None，写/generation 均 fail-closed。"""
    from unittest.mock import AsyncMock

    from core.storage.base import StorageUnavailableError

    agent = Agent(config=AppConfig())
    agent._user_identity_repo.connect = AsyncMock(return_value=False)  # noqa: SLF001
    await agent.start()
    try:
        store = agent.user_identity_store
        assert store.get() is None
        assert store.is_available is False
        with pytest.raises(StorageUnavailableError):
            await store.update_if_revision(1, {"displayName": "新名"})
        # generation 取本轮身份快照 → fail-closed（不拿占位身份继续聊天）
        with pytest.raises(StorageUnavailableError):
            agent.cognitive._identity_snapshot("启动默认名")  # noqa: SLF001
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_identity_reloaded_after_mongo_recovery():
    """P0-3：Mongo 恢复 → 协调器重载权威档案，下一轮 Prompt 使用持久化身份。"""
    from unittest.mock import AsyncMock

    db = _DurableFakeDB()

    seed = _make_agent(db)
    await seed.start()
    try:
        await seed._user_identity_store.update_if_revision(  # noqa: SLF001
            1, {"displayName": "妖梦", "preferredAddress": "主人",
                "pronouns": "她", "initialized": True})
    finally:
        await seed.stop()

    agent = _make_agent(db)
    agent._user_identity_repo.connect = AsyncMock(return_value=False)  # noqa: SLF001
    await agent.start()
    try:
        assert agent.user_identity_store.get() is None
        assert agent.user_identity_store.is_available is False

        # Mongo 恢复：撤销 connect 打桩，协调器重载
        del agent._user_identity_repo.connect  # noqa: SLF001
        assert await agent._recover_dependency("identity") is True  # noqa: SLF001

        loaded = agent.user_identity_store.get()
        assert loaded is not None
        assert loaded.revision == 2              # 不覆盖 Mongo 中更高 revision
        assert loaded.preferredAddress == "主人"
        assert agent._user_identity.preferredAddress == "主人"  # noqa: SLF001

        snapshot = agent.cognitive._identity_snapshot("启动默认名")  # noqa: SLF001
        assert snapshot.revision == 2
        assert snapshot.displayName == "妖梦"
        assert snapshot.preferredAddress == "主人"
    finally:
        await agent.stop()
