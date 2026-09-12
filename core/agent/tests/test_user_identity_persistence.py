"""BE-STAGEH-02：用户身份必须跨服务进程重启保持（Mongo 权威）。

真实联调复现：只重启后端进程、不清数据库，`GET /api/me` 回到
`initialized=false / revision=1 / defaultCharacterId=null`——角色「不再认识我」。
本文件用同一份假 Mongo 数据跨两个 Agent 实例验证重启后档案与 revision 不变。

**存储隔离**：身份仓储经 `_make_agent` **直接注入** `_DurableFakeDB`（不走
`DocumentDBFactory`），故根 conftest 的真实库防线**照常生效**——`agent.start()`
连接的其余仓储（session/message/token/...）会被导流到内存 fake。

> 历史：本文件曾标 `no_storage_guard`（关掉防线）。那让 `agent.start()` 把**其余
> 仓储全部真连生产库**——实测一次全量跑产生 **108 次** `MongoDBBackend(db="default")`
> 真实构造。直接注入本就与防线解耦，无需关掉它。
"""

import asyncio
import os
from unittest.mock import AsyncMock, patch

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


def _isolate_probes(agent: Agent) -> None:
    """除 identity 外全部探针恒 False——避免其它恢复 hook 触达真实 DB（测试聚焦身份）。"""
    for owner in (agent._session_manager, agent._message_repo,   # noqa: SLF001
                  agent._token_repo, agent._version_repo,        # noqa: SLF001
                  agent._long_term, agent.idempotency_repo):     # noqa: SLF001
        owner.health_check = AsyncMock(return_value=False)


async def _run_health_rounds(agent: Agent, n_rounds: int) -> None:
    """直接驱动 n 轮健康探测/恢复（`Agent._db_health_round`）。

    **不再 patch `asyncio.sleep`**：全局 patch 会误伤调度器 `_prune_loop`(10s) /
    idle(1s) / mailbox(0.5s) 等后台循环的 sleep，抢占轮次预算，使健康循环首轮就被
    取消、循环体从不执行（2026-09-11 实测：测试假绿）。直接调单轮方法，确定性强、
    与其它后台任务无关。
    """
    prev: dict[str, bool] = {}
    for _ in range(n_rounds):
        prev = await agent._db_health_round(prev)  # noqa: SLF001


@pytest.mark.asyncio
async def test_user_identity_survives_agent_restart():
    db = _DurableFakeDB()

    agent_a = _make_agent(db)
    await agent_a.start()
    try:
        store_a = agent_a.user_identity_store
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
        loaded = agent_b.user_identity_store.get()
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
        await agent_a.user_identity_store.update_if_revision(
            1, {"displayName": "妖梦", "initialized": True})
    finally:
        await agent_a.stop()

    agent_b = _make_agent(db)
    await agent_b.start()
    try:
        loaded = agent_b.user_identity_store.get()
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
        updated = await agent_a.user_identity_store.update_if_revision(
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
        await seed.user_identity_store.update_if_revision(
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


@pytest.mark.asyncio
async def test_identity_recovered_on_first_healthy_probe():
    """P0-2：离线启动 + 首轮探测即 healthy（prev={} 无 False→True 边界）也必须重载权威档案。

    否则 identity 缓存永远为 None，后续聊天持续 STORAGE_UNAVAILABLE。
    """
    db = _DurableFakeDB()
    seed = _make_agent(db)
    await seed.start()
    try:
        await seed.user_identity_store.update_if_revision(
            1, {"displayName": "妖梦", "preferredAddress": "主人",
                "pronouns": "她", "initialized": True})
    finally:
        await seed.stop()

    agent = _make_agent(db)
    agent._user_identity_repo.connect = AsyncMock(return_value=False)  # noqa: SLF001
    await agent.start()
    try:
        assert agent.user_identity_store.get() is None
        # Mongo 在健康循环首轮探测前已恢复：撤销打桩（探针直接 True）
        del agent._user_identity_repo.connect  # noqa: SLF001
        _isolate_probes(agent)

        await _run_health_rounds(agent, 1)

        loaded = agent.user_identity_store.get()
        assert loaded is not None
        assert loaded.revision == 2              # 用 Mongo 权威，非本地 bootstrap 的 revision 1
        assert loaded.displayName == "妖梦"
        assert loaded.preferredAddress == "主人"
        assert agent.user_identity_store.is_available is True
        assert agent._user_identity.revision == 2        # agent 侧同步  # noqa: SLF001
        assert agent._user_identity.displayName == "妖梦"  # noqa: SLF001
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_identity_recovery_retries_on_later_rounds_while_failing():
    """恢复失败 → 保持 fail-closed，健康循环后续轮次继续重试（不静默放弃）。"""
    agent = Agent(config=AppConfig())
    attempts = 0

    async def _down() -> bool:
        nonlocal attempts
        attempts += 1
        return False

    agent._user_identity_repo.connect = _down      # type: ignore[method-assign]  # noqa: SLF001
    await agent.start()
    try:
        _isolate_probes(agent)
        before = attempts
        await _run_health_rounds(agent, 3)

        assert attempts - before >= 3            # 每轮都重试
        assert agent.user_identity_store.get() is None
        assert agent.user_identity_store.is_available is False
    finally:
        await agent.stop()
