# 测试文件：core/input/tests/test_ready_embedding.py
"""`/ready` 的嵌入可用性（C / CR-10 收尾）。

缺陷原状：`/ready` 的 `dependencies` 只有 mongo/milvus/idempotency/identity。
Milvus 通了、**嵌入挂了**时所有探针与面板都正常，而长期记忆实际"只写进程内
`_entries`、重启即丢"——正是 CR-10 那条"正在无声丢数据、所有观测面显示正常"。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.service


def _agent(*, embedding_ready: bool, milvus_ok: bool = True) -> MagicMock:
    from core.agent import RunState
    from core.input.tests.test_health import _make_mock_agent

    agent = _make_mock_agent()
    agent.long_term.health_check = AsyncMock(return_value=milvus_ok)
    # 真实 Agent 上这是 `LongTermMemory.embedding_ready`（property，非阻塞）
    type(agent.long_term).embedding_ready = property(lambda self: embedding_ready)
    agent._state = RunState.RUNNING
    agent.state = RunState.RUNNING
    return agent


@pytest.mark.asyncio
async def test_embedding_not_ready_makes_full_ready_false(aiohttp_client):
    """嵌入未就绪 → chatReady 仍 200（文本可用），但 fullReady 与 status 如实降级。"""
    from core.input.server import create_app

    app = create_app(_agent(embedding_ready=False), serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/ready")

    assert resp.status == 200, "文本可用不应被嵌入拖成 503"
    data = await resp.json()
    assert data["chatReady"] is True
    assert data["fullReady"] is False, "长期记忆写不进去 → 不算完整就绪"
    assert data["dependencies"]["embedding"] is False
    assert data["status"] == "degraded"


@pytest.mark.asyncio
async def test_embedding_ready_makes_full_ready_true(aiohttp_client):
    """三个条件齐备 → fullReady true（回归网）。"""
    from core.input.server import create_app

    app = create_app(_agent(embedding_ready=True), serve_static=False)
    client = await aiohttp_client(app)
    data = await (await client.get("/ready")).json()

    assert data["chatReady"] is True
    assert data["fullReady"] is True
    assert data["dependencies"]["embedding"] is True
    assert data["status"] == "ok"


def test_is_ready_is_pure_and_does_not_trigger_load():
    """`is_ready` 必须是纯读——探针绝不能触发一次冷加载（实测 11s 起）。"""
    from core.memory.embedding_provider import EmbeddingProvider

    provider = EmbeddingProvider()
    assert provider._task is None          # noqa: SLF001
    assert provider.is_ready is False
    assert provider._task is None, "读属性不得创建加载任务"   # noqa: SLF001


@pytest.mark.asyncio
async def test_debug_info_reads_public_accessor_not_private():
    """debug 面板同样走公开访问器——不再穿透 `_embedding_provider._backend`。

    替身对任何未声明的属性访问直接抛错：只要采集还在穿透私有路径，这条就红。
    """
    from core.debug.info import _collect_session_and_memory, DebugInfo

    class _NoPrivateLongTerm:
        embedding_ready = True
        pending_sync_count = 0
        count = 0
        _entries: list = []          # LTM Recent 面板仍读它（append-only 展示，既有豁免）

        def __getattr__(self, name: str):
            raise AssertionError(f"debug 采集不应穿透私有属性：{name}")

    agent = MagicMock()
    agent.long_term = _NoPrivateLongTerm()
    agent.memory_repo.health_check = AsyncMock(return_value=True)
    agent.memory_repo.count = AsyncMock(return_value=0)
    agent.session_repo.health_check = AsyncMock(return_value=True)
    agent.session.messages = []
    agent.consolidator = None

    info = DebugInfo()
    await _collect_session_and_memory(agent, info)
    assert info.embedding_ok is True
