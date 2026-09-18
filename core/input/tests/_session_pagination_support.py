"""会话分页测试共享脚手架（Batch 8 / P2-01r）。

**为什么单独一个模块**：分页完整性要在两层各自钉住——route 层（用户入口、
公开合同）与 manager 层（本地摘要覆盖层 + 持久层合并）。两处都需要同一套
"真实 SessionManager + 真实 SessionRepository + 内存 DocumentDB 后端" 装配，
以及一个**只依据公开合同翻页**的 driver。

**driver 的纪律（本批最容易写出假绿的地方）**：
`paginate_sessions()` **只**使用响应里的 `has_more` / `cursor` 决定是否继续，
**不**读数据库、**不**读 `len(sessions)` 之外的总数、**不**预先知道数据集大小。
否则"客户端其实知道该继续"会让 metadata 撒谎时测试照样绿——正是 P2-01r 这类
"没有更多"型 bug 的盲区。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from core.cognitive.generation_registry import GenerationRegistry
from core.memory.session_manager import SessionManager
from core.storage.generation_repo import GenerationRepository
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient

#: 分页测试统一页大小（<= route 的 `_MAX_LIMIT`=100，且能整除 200 便于临界观察）。
PAGE_LIMIT = 50


# ── fake agent（route 层测试用；只暴露路由实际消费的公开成员） ────────────────


class _FakeCognitive:
    """暴露 `_generation_registry` 给路由（对齐 agent.cognitive）。"""

    def __init__(self, registry: GenerationRegistry) -> None:
        self._generation_registry = registry


class _FakeToolManager:
    def __init__(self, approvals: list[Any]) -> None:
        self._approvals = approvals

    def get_pending_approvals(self) -> list[Any]:
        return list(self._approvals)


class _FakeToolAdapter:
    def __init__(self, manager: _FakeToolManager) -> None:
        self._manager = manager

    def get_pending_approvals(self) -> list[Any]:
        return self._manager.get_pending_approvals()


class _FakeStatus:
    def __init__(self, accepting: bool = True) -> None:
        self._accepting = accepting

    @property
    def is_accepting_input(self) -> bool:
        return self._accepting


class SessionTestAgent:
    """REST 路由消费的假 agent：真实 SessionManager + 真实 SessionRepository。

    仓储落在**内存 DocumentDB 后端**（`FakeDocumentDBBackend`）上——分页查询
    （keyset 谓词 / sort / limit / $regex / $in）走真实的仓储代码路径，只有
    后端是内存实现；真实驱动语义由 `tests/real_mongo/` 层另行证明。
    """

    def __init__(self, backend: FakeDocumentDBBackend | None = None,
                 offline: bool = False) -> None:
        self.backend = backend or FakeDocumentDBBackend()
        self.session_repo = SessionRepository(db=None if offline else self.backend)
        self.message_repo = MessageRepository(db=self.backend)
        self.gen_repo = GenerationRepository(db=self.backend)
        self.gen_registry = GenerationRegistry(repo=self.gen_repo)
        self.session_manager = SessionManager(repo=self.session_repo,
                                              msg_repo=self.message_repo)
        self.pending_messages: list[Any] = []
        self._approvals: list[Any] = []
        self._cognitive = _FakeCognitive(self.gen_registry)
        self.tool_adapter = _FakeToolAdapter(_FakeToolManager(self._approvals))
        self.status = _FakeStatus()

    @property
    def active_session_id(self) -> str:
        return str(self.session_manager.active_id)

    @property
    def cognitive(self) -> _FakeCognitive:
        return self._cognitive

    @property
    def generation_registry(self) -> GenerationRegistry:
        return self.gen_registry

    async def switch_session(self, sid: Any) -> None:
        await self.session_manager.switch(sid)

    async def resolve_session(self, conversation_id: str | None, protocol_version: int):
        return await self.session_manager.resolve(conversation_id, protocol_version)


def make_app(agent: SessionTestAgent, access_token: str = ""):
    """按生产装配建 app（`create_app` 真身，serve_static 关闭）。"""
    from core.input.server import create_app

    return create_app(agent, serve_static=False, access_token=access_token)


# ── 播种 helper ────────────────────────────────────────────────────────────


def session_doc(sid: str, *, title: str = "", updated_at: str,
                character_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造与 `SessionRepository.create()` 同形的 sessions doc（可指定时间戳）。"""
    return {
        "session_id": sid,
        "title": title,
        "messages": [],
        "character_binding": character_binding or {},
        "updated_at": updated_at,
    }


def ts(second: int, *, day: int = 1) -> str:
    """确定性 UTC 时间戳（2026-08-<day>T00:00:<second>+00:00，字典序 = 时间序）。"""
    return f"2026-08-{day:02d}T00:{second // 60:02d}:{second % 60:02d}+00:00"


async def seed_session_docs(agent: SessionTestAgent, count: int, *,
                            prefix: str = "conv_seed", day: int = 1) -> list[str]:
    """直接向内存后端播种 count 条 sessions doc（时间戳递增，末条最新）。

    **有意绕过 repo.create**：分页测试需要**确定性**的 updated_at 序列，
    而 `create()` 用墙钟时间。doc 形态与 `create()` 完全一致。
    """
    ids: list[str] = []
    for i in range(count):
        sid = f"{prefix}_{i:03d}"
        await agent.backend.insert_one(
            "sessions", session_doc(sid, title=f"会话{i:03d}", updated_at=ts(i, day=day)))
        ids.append(sid)
    return ids


# ── 公开合同 driver（route 层） ────────────────────────────────────────────


async def paginate_sessions(client: "TestClient", *, limit: int = PAGE_LIMIT,
                            search: str = "", max_pages: int = 100) -> dict[str, Any]:
    """按**公开合同**逐页拉取 `GET /api/sessions`，返回 {ids, pages, last_page}。

    只依据 `has_more` / `cursor` 决定继续；不读数据库、不读 total。
    metadata 自相矛盾（has_more=True 无 cursor / has_more=False 带 cursor /
    cursor 不推进）一律当场断言失败——这正是 P2-01r 的"没有更多"盲区。
    """
    ids: list[str] = []
    cursor: str | None = None
    pages: list[dict[str, Any]] = []
    while True:
        url = f"/api/sessions?limit={limit}"
        if search:
            url += f"&search={quote(search)}"
        if cursor:
            url += f"&cursor={quote(cursor)}"
        resp = await client.get(url)
        assert resp.status == 200, f"分页请求应 200，实际 {resp.status}: {url}"
        data = await resp.json()
        pages.append(data)
        ids.extend(s["conversationId"] for s in data["sessions"])
        if not data["has_more"]:
            assert not data.get("cursor"), "has_more=False 时不得返回 cursor"
            break
        assert data.get("cursor"), "has_more=True 时必须返回 cursor"
        assert data["cursor"] != cursor, "cursor 未推进——客户端会原地死循环"
        cursor = data["cursor"]
        assert len(pages) < max_pages, f"翻页超过 {max_pages} 页，疑似不收敛"
    return {"ids": ids, "pages": pages, "last_page": pages[-1]}


