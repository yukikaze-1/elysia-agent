# core/agent/tests/test_identity_readiness_lifecycle.py
"""Batch 4（N-1 + N-11）——**Agent lifecycle** 层的身份可用性 → readiness 真值。

与 `core/profile/tests/test_ready_identity_truthfulness.py` 的分工：那一份钉 store
与路由的判据来源（含 fresh 子进程），本份走**真实 `Agent.start()`** —— 装载发生在
`_start_persistence` 的启动步骤里，失败必须在运行时状态与 `/ready` 上同时可见。

本份要回答的一个具体问题（§31/§39）：**装载失败之后 Agent 还在不在跑？**
会。`connect()` 不 raise（调用方按 None 判定），进程继续存活、`/health` 仍 200、
`/ready` 摘除——这正是 readiness 存在的意义：不把「进程死了」和「核心依赖不可用」
混为一谈，也不为了表达失败而回滚整个 Agent。
"""


import pytest

from core.agent.agent import Agent
from core.config import AppConfig
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service


@pytest.fixture(autouse=True)
def _fake_deepseek_key(monkeypatch):
    """Agent 构造要求 `DEEPSEEK_API_KEY` 非空（模块内提供假 key，用完还原）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")


class _DurableFakeDB(FakeDocumentDBBackend):
    """进程退出但数据仍在（close 不清库）——对齐其它身份持久化测试。"""

    async def close(self) -> None:  # noqa: D102
        pass


def _make_agent(db: FakeDocumentDBBackend) -> Agent:
    """真实 Agent + 注入假库（身份仓储直连；其余仓储由根 conftest 防线导流）。"""
    agent = Agent(config=AppConfig())
    agent._user_identity_repo._db = db                 # noqa: SLF001
    agent._user_identity_repo._injected_db = True      # noqa: SLF001
    return agent


def _seed_bad_identity_doc(db: FakeDocumentDBBackend) -> None:
    """往库里塞一条**不合当前域模型**的身份文档（真实反序列化失败）。"""
    db.collections["user_identity_profiles"] = [{
        "user_id": "usr_local_default",
        "display_name": "妖梦",
        "revision": 0,          # 域模型约束 ge=1 → model_validate 抛 ValidationError
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }]


async def _ready_status(agent: Agent) -> tuple[int, dict]:
    """走**真实路由**取一次 /ready（含 create_app 的完整装配）。"""
    from aiohttp.test_utils import TestClient, TestServer

    from core.input.server import create_app

    app = create_app(agent, serve_static=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/ready")
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_start_with_undecodable_identity_keeps_process_alive_but_not_ready():
    """真实启动装载失败：进程继续跑（liveness 200），readiness 摘除（503）。

    这是本批的核心不变量在一个真实 lifecycle 上的样子：
    `connect()` 的失败**不**打穿 `Agent.start()`（不整体回滚、不崩进程），
    但失败事实留在 store 上，`/ready` 据此如实降级。
    """
    db = _DurableFakeDB()
    _seed_bad_identity_doc(db)
    agent = _make_agent(db)

    await agent.start()          # 装载失败不应让 start() 抛错
    try:
        store = agent.user_identity_store
        assert store.get() is None
        assert store.is_available is False
        assert store.last_error == "ValidationError", (
            "失败类型必须留在运行时状态里（N-11），而不是只写进日志")
        # 进程确实活着：Agent 处于 RUNNING、身份相关业务 fail-closed
        from core.agent import RunState
        assert agent.state == RunState.RUNNING

        status_code, payload = await _ready_status(agent)
        assert status_code == 503, "身份不可用 + 进程存活 → readiness 必须摘除"
        assert payload["chatReady"] is False
        assert payload["dependencies"]["identity"] is False
        assert payload["dependencies"]["lifecycle"] is True   # 不是停机窗口
        assert payload["status"] == "degraded"
        assert "last_error" not in payload
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_identity_recovery_via_coordinator_restores_readiness():
    """恢复走**现有** lifecycle（恢复协调器重载），readiness 随之恢复——无新框架。

    §35：只有当前 lifecycle 已支持 reload/reconnect 才测恢复；这里用的就是
    `_recover_dependency("identity")`（`_db_health_round` 已在跑的既有路径）。
    """
    db = _DurableFakeDB()
    _seed_bad_identity_doc(db)
    agent = _make_agent(db)

    await agent.start()
    try:
        store = agent.user_identity_store
        assert store.is_available is False
        assert (await _ready_status(agent))[0] == 503

        # 运维修好了那条坏文档（把 revision 改成合法值）
        db.collections["user_identity_profiles"][0]["revision"] = 1

        assert await agent._recover_dependency("identity") is True   # noqa: SLF001

        assert store.is_available is True
        assert store.last_error is None
        assert store.get().displayName == "妖梦"                      # type: ignore[union-attr]
        status_code, payload = await _ready_status(agent)
        assert status_code == 200
        assert payload["chatReady"] is True
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_healthy_start_is_ready_and_does_not_touch_health_check_probe():
    """/ready 对身份**不再发起 repo 探针**：`repo.health_check()` 会
    `_mark_unavailable()` 并重连（有副作用），而 readiness 应读拥有的运行时状态。

    本用例用"探针会被计数"的方式钉住这一点：把 repo.health_check 换成计数桩，
    断言 /ready 打不到它（若有人把判据改回 ping，计数会立刻不为 0）。
    """
    db = _DurableFakeDB()
    agent = _make_agent(db)
    await agent.start()
    try:
        calls = {"n": 0}
        original = agent._user_identity_repo.health_check    # noqa: SLF001

        async def _counting() -> bool:
            calls["n"] += 1
            return await original()

        agent._user_identity_repo.health_check = _counting    # type: ignore[method-assign]  # noqa: SLF001

        status_code, payload = await _ready_status(agent)

        assert status_code == 200 and payload["chatReady"] is True
        assert calls["n"] == 0, (
            "readiness 不应触发 repo 探针（判据是 is_available，纯读；且探针会重连）")
    finally:
        await agent.stop()
