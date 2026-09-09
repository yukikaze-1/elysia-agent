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
