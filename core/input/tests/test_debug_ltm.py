"""/debug/api/ltm 列表过滤——lowercase 过滤应命中规范值存储（修复大小写 bug）。"""
import pytest

from core.input.server import create_app


def _make_agent():
    from core.config import AppConfig
    from core.agent import Agent
    from core.memory.long_term import MemoryEntry
    from core.schema.memory import MemoryType
    agent = Agent(config=AppConfig())
    agent.long_term._entries = [  # noqa: SLF001 — 测试直注入
        MemoryEntry(id="1", text="工作计划", memory_type=MemoryType.PLAN),
        MemoryEntry(id="2", text="用户偏好", memory_type=MemoryType.PREFERENCE),
    ]
    return agent


@pytest.mark.service
@pytest.mark.asyncio
async def test_ltm_list_filter_case_insensitive(aiohttp_client):
    """?type=plan（lowercase）应命中 PLAN 条目（此前匹配不上）。"""
    agent = _make_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/debug/api/ltm", params={"type": "plan"})
    assert resp.status == 200
    data = await resp.json()
    ids = [e["id"] for e in data["entries"]]
    assert ids == ["1"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_ltm_list_filter_unknown_returns_empty(aiohttp_client):
    """?type=bogus 应返回空（不误命中 NOTE）。"""
    agent = _make_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/debug/api/ltm", params={"type": "bogus"})
    assert resp.status == 200
    data = await resp.json()
    assert data["entries"] == []
