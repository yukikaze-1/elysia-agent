"""批次 3 Task 12：REST 会话路由（marker=service）。

覆盖 spec §4-§8：
- 会话 CRUD：列表（cursor/limit/search 大小写不敏感 + active 填充）、创建、详情、重命名、
  删除（联动 + active 回落）、激活；未知 id → 404 SESSION_NOT_FOUND。
- 历史：before/limit 分页 + has_more；after= 增量正序。
- 上下文：缺 conversationId → 400 CONTRACT_VALIDATION。
- 搜索：query/conversationId/日期/时区/category/cursor/limit（上限 100）；
  INVALID_TIMEZONE 400；responseId 反查 best-effort / segmentId 恒缺省 / snippet 前 100 字。
- 资源：image/link 命中、sticker 恒空、text 不支持 400；cursor=扫描停止 _id；peek 判 has_more。
- generation 状态：列表（status/cursor/limit keyset）+ 详情；未知 gid → 404 GENERATION_NOT_FOUND。
- pending：pendingMessages 按会话过滤 + pendingApprovals 全局返回。
- 全部 /api/* 匿名 401（产品认证接入）。

生产数据保护：全部用 FakeDocumentDBBackend 内存库 + 隔离会话 ID（conv_test_*），
不触碰真实 Mongo，绝不删/清 default。
"""

import base64
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from core.cognitive.generation_registry import GenerationRegistry
from core.character.service import CharacterNotFoundError
from core.memory.session_manager import SessionManager
from core.schema.message import MessageId, UserMessage, AssistantMessage
from core.schema.part import TextPart, ImagePart, VideoPart, FilePart, AudioPart, StickerPart
from core.schema.types import RequestId, SessionId
from core.storage.generation_repo import GenerationRepository
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend


# ── 测试用假 agent（隔离内存仓库，零生产副作用） ──────────────────────────────


class _FakeCognitive:
    """暴露 _generation_registry 给路由（对齐 agent.cognitive）。"""

    def __init__(self, registry) -> None:
        self._generation_registry = registry


class _FakeToolManager:
    def __init__(self, approvals) -> None:
        self._approvals = approvals

    def get_pending_approvals(self) -> list:
        return list(self._approvals)


class _FakeToolAdapter:
    def __init__(self, manager) -> None:
        self._manager = manager


class _FakeStatus:
    """F1(a) busy 门禁用的 fake status：is_accepting_input 可控。"""

    def __init__(self, accepting: bool = True) -> None:
        self._accepting = accepting

    @property
    def is_accepting_input(self) -> bool:
        return self._accepting


class _FakeAgent:
    """REST 路由消费的假 agent：真实 SessionManager + 内存 repo + 可控 pending/approvals。

    Args:
        backend: 共享 FakeDocumentDBBackend（恢复/重启场景复用同一内存库）。
        offline: True 时 session_repo 以 db=None 构造（模拟 Mongo 离线启动）。
    """

    def __init__(self, backend: FakeDocumentDBBackend | None = None,
                 offline: bool = False) -> None:
        self._backend = backend or FakeDocumentDBBackend()
        self.session_repo = SessionRepository(db=None if offline else self._backend)
        self.message_repo = MessageRepository(db=self._backend)
        self.gen_repo = GenerationRepository(db=self._backend)
        self.gen_registry = GenerationRegistry(repo=self.gen_repo)
        self.session_manager = SessionManager(repo=self.session_repo, msg_repo=self.message_repo)
        self.pending_messages = []
        self._approvals = []
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
    def generation_registry(self):
        """公开 property（对齐 Agent.generation_registry，2026-08-14 模块化修复）。"""
        return self.gen_registry

    async def switch_session(self, sid) -> None:
        await self.session_manager.switch(sid)

    async def resolve_session(self, conversation_id: str | None, protocol_version: int):
        """对齐 Agent.resolve_session——WS 建连回放 SessionMessage.conversationId 同源。"""
        return await self.session_manager.resolve(conversation_id, protocol_version)


def _make_app(agent, access_token: str = ""):
    from core.input.server import create_app

    return create_app(agent, serve_static=False, access_token=access_token)


# ── 数据播种 helper ──────────────────────────────────────────────────────


async def _create_session(agent, sid: str, title: str = "") -> None:
    """创建隔离会话（直接落 repo，不触碰 default）。"""
    await agent.session_repo.create(sid, title)


async def _append(agent, sid: str, msg) -> None:
    """归档一条消息到指定会话。"""
    await agent.message_repo.append(sid, msg)


async def _seed_text_messages(agent, sid: str, count: int, prefix: str = "消息") -> None:
    """按序播种 count 条 user 文本消息（timestamp 递增，确定性排序）。

    timestamp 用 分:秒 复合（支持 count>60，避免 second 越界 0..59）。
    """
    for i in range(count):
        await _append(agent, sid, UserMessage(
            content=[TextPart(content=f"{prefix}{i}")],
            message_id=MessageId(f"msg_{sid}_{i}"),
            timestamp=f"2026-08-01T10:{i // 60:02d}:{i % 60:02d}+00:00",
        ))


async def _seed_generation(agent, sid: str, gid: str, message_id: str,
                           user_message_id: str = "um_x", status: str = "completed") -> None:
    """播种一条 generation（register + 终态 mark）。"""
    await agent.gen_registry.register(
        gid, session_id=sid, user_message_id=user_message_id,
        message_id=message_id, model="deepseek/deepseek-v4-flash")
    if status == "completed":
        await agent.gen_registry.mark(gid, status="completed", finish_reason="stop")
    elif status == "failed":
        await agent.gen_registry.mark(gid, status="failed", finish_reason="error", error="boom")
    elif status in ("cancelled", "interrupted"):
        await agent.gen_registry.mark(gid, status=status)


def _encode_b64_cursor(raw: str) -> str:
    """构造 keyset 游标（base64url(`x|y`)）。"""
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _msg_id_from_page(data: dict, index: int) -> str:
    """从消息页响应取第 index 条消息的 _id（Mongo 归档游标）。"""
    return data["messages"][index]["_id"]


# ───────────────────────── 会话 CRUD（§4） ────────────────────────────────


@pytest.mark.service
class TestSessionsCRUD:
    @pytest.mark.asyncio
    async def test_list_empty(self, aiohttp_client):
        """clean-start 会话发现（BE-P1-05）：空 Mongo + 空 local 时 default 恒可见且 active。

        原语义（`sessions == []`）已被推翻——eager default 恒有合法本地摘要。
        """
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["sessions"]) == 1
        d = data["sessions"][0]
        assert d["conversationId"] == "default"
        assert d["active"] is True
        assert d["messageCount"] == 0
        assert d["updatedAt"]
        assert data["has_more"] is False
        assert "cursor" not in data  # 无更多时不带 cursor

    @pytest.mark.asyncio
    async def test_clean_start_default_detail_updated_at_parseable(self, aiohttp_client):
        """clean-start：GET /api/sessions/default detail updatedAt 非空且可解析。"""
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/default")
        assert resp.status == 200
        data = await resp.json()
        assert data["session"]["conversationId"] == "default"
        assert data["session"]["active"] is True
        assert data["session"]["messageCount"] == 0
        assert data["session"]["updatedAt"]
        datetime.fromisoformat(data["session"]["updatedAt"])  # 非空且可解析
        assert data["active"] is True

    @pytest.mark.asyncio
    async def test_list_exactly_one_active_when_nonempty(self, aiohttp_client):
        """不变量：列表会话非空时恒恰一个 active（active 由 _active_id 单点派生）。"""
        agent = _FakeAgent()
        for i in range(3):
            await _create_session(agent, f"conv_test_one{i}", f"会话{i}")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions")
        data = await resp.json()
        assert data["sessions"], "会话列表非空"
        actives = [s for s in data["sessions"] if s["active"]]
        assert len(actives) == 1
        assert actives[0]["conversationId"] == "default"

        # 切 active 后仍恒恰一个（default 让位）
        await agent.switch_session("conv_test_one1")
        resp2 = await client.get("/api/sessions")
        data2 = await resp2.json()
        actives2 = [s for s in data2["sessions"] if s["active"]]
        assert len(actives2) == 1
        assert actives2[0]["conversationId"] == "conv_test_one1"

    @pytest.mark.asyncio
    async def test_default_consistency_offline_recovered_restart(self, aiohttp_client):
        """Mongo 离线/恢复/重启后，list / detail / WS 首帧 conversationId 恒一致指向 default。

        WS 首帧 conversationId = _send_session_replay 的 SessionMessage.conversationId，
        其值 = agent.resolve_session(None, protocol_version).session_id（活动会话）——
        与 REST active、agent.active_session_id 同源（均派生自 _active_id 单点）。
        """
        # 1) 离线启动（session_repo db=None）：default 仍可见 + active，detail 非空
        offline = _FakeAgent(offline=True)
        app = _make_app(offline)
        client = await aiohttp_client(app)
        await self._assert_default_consistent(offline, client)

        # 2) Mongo 恢复（模拟 _recover_dependency("session")）：ensure_default_persisted 落库
        backend = FakeDocumentDBBackend()
        recovered = _FakeAgent(backend=backend)
        await recovered.session_manager.ensure_default_persisted()
        docs = backend.collections.get("sessions", [])
        assert any(d.get("session_id") == "default" for d in docs), "恢复后 default 文档应落库"
        app2 = _make_app(recovered)
        client2 = await aiohttp_client(app2)
        await self._assert_default_consistent(recovered, client2)

        # 3) 重启（同 backend，新进程视角）：default 仍可见 + active
        restarted = _FakeAgent(backend=backend)
        app3 = _make_app(restarted)
        client3 = await aiohttp_client(app3)
        await self._assert_default_consistent(restarted, client3)

    async def _assert_default_consistent(self, agent, client) -> None:
        """断言三条接口（REST list / REST detail / WS 首帧 conversationId）一致指向 default。"""
        # WS 首帧 conversationId 源：建连回放 resolve 活动会话
        ws_sid = str((await agent.resolve_session(None, 2)).session_id)
        assert ws_sid == "default"
        assert str(agent.active_session_id) == "default"

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        data = await resp.json()
        defaults = [s for s in data["sessions"] if s["conversationId"] == "default"]
        assert len(defaults) == 1
        assert defaults[0]["active"] is True
        assert defaults[0]["updatedAt"]

        resp2 = await client.get("/api/sessions/default")
        assert resp2.status == 200
        detail = await resp2.json()
        assert detail["session"]["conversationId"] == "default"
        assert detail["session"]["active"] is True
        assert detail["session"]["updatedAt"]
        datetime.fromisoformat(detail["session"]["updatedAt"])

    @pytest.mark.asyncio
    async def test_ensure_exists_does_not_clobber_messages(self):
        """ensure_exists 用 $setOnInsert——不覆盖既有 default 文档的 messages（LLM 上下文窗口）。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        # 预置 default 文档（含 messages + 显式 title——模拟重启前已持久化的会话上下文）
        await repo.create("default", title="旧标题")
        await backend.update_one(
            "sessions", {"session_id": "default"},
            {"$set": {"messages": [{"role": "user",
                                    "content": [{"type": "text", "content": "既有上下文"}]}]}})
        inserted = await repo.ensure_exists("default")
        assert inserted is False  # 已存在 → 未新插入
        docs = backend.collections["sessions"]
        doc = next(d for d in docs if d["session_id"] == "default")
        assert len(doc["messages"]) == 1  # messages 未被清空
        assert doc["messages"][0]["content"][0]["content"] == "既有上下文"
        assert doc["title"] == "旧标题"  # title 也未被重置

    @pytest.mark.asyncio
    async def test_ensure_exists_inserts_when_missing(self):
        """default 文档不存在时 ensure_exists 插入空会话并返回 True。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        inserted = await repo.ensure_exists("default")
        assert inserted is True
        docs = backend.collections["sessions"]
        doc = next(d for d in docs if d["session_id"] == "default")
        assert doc["messages"] == []
        assert doc["title"] == ""

    @pytest.mark.asyncio
    async def test_create_returns_summary(self, aiohttp_client):
        agent = _FakeAgent()
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.post("/api/sessions", json={"title": "我的会话"})
        assert resp.status == 200
        data = await resp.json()
        assert data["conversationId"].startswith("conv_")
        assert data["title"] == "我的会话"
        assert data["messageCount"] == 0
        assert data["active"] is False
        assert "updatedAt" in data
        assert data["character"] == {
            "characterId": "chr_builtin_elysia",
            "characterInstanceId": "chi_builtin_elysia",
            "characterRevision": 1,
            "displayName": "Elysia",
            "presentation": {
                "avatarAssetId": None,
                "live2dModelId": None,
                "themeColor": None,
            },
        }
        session = agent.session_manager.get(data["conversationId"])
        assert session.character_binding == {
            "character_id": "chr_builtin_elysia",
            "instance_id": "chi_builtin_elysia",
        }

    @pytest.mark.asyncio
    async def test_create_without_title_omits_field(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.post("/api/sessions", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["conversationId"].startswith("conv_")
        assert "title" not in data  # 未命名缺省省略

    @pytest.mark.asyncio
    async def test_create_explicit_character_and_replay_summary(self, aiohttp_client):
        agent = _FakeAgent()
        app = _make_app(agent)
        client = await aiohttp_client(app)
        created = await client.post("/api/sessions", json={
            "title": "Cyrene",
            "characterId": "chr_builtin_cyrene",
        })
        assert created.status == 200
        data = await created.json()
        assert data["character"]["characterId"] == "chr_builtin_cyrene"
        assert data["character"]["characterInstanceId"] == "chi_builtin_cyrene"
        sid = data["conversationId"]

        listed = await client.get("/api/sessions")
        item = next(
            row for row in (await listed.json())["sessions"]
            if row["conversationId"] == sid
        )
        assert item["character"] == data["character"]
        detail = await client.get(f"/api/sessions/{sid}")
        assert (await detail.json())["session"]["character"] == data["character"]

    @pytest.mark.asyncio
    async def test_create_rejects_unknown_character_and_unknown_request_field(
        self, aiohttp_client,
    ):
        app = _make_app(_FakeAgent())
        app["character_session_binding_service"].resolve_for_create = AsyncMock(
            side_effect=CharacterNotFoundError("chr_missing"),
        )
        client = await aiohttp_client(app)
        missing = await client.post("/api/sessions", json={
            "characterId": "chr_missing",
        })
        assert missing.status == 404
        assert (await missing.json())["code"] == "CHARACTER_NOT_FOUND"
        invalid = await client.post("/api/sessions", json={"unknown": True})
        assert invalid.status == 400
        assert (await invalid.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_list_active_fill_and_search(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_a", "Alpha 项目")
        await _create_session(agent, "conv_test_b", "Beta 项目")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        # active 填充：default 是活动会话
        resp = await client.get("/api/sessions")
        data = await resp.json()
        by_id = {s["conversationId"]: s for s in data["sessions"]}
        assert by_id["conv_test_a"]["active"] is False
        assert data["has_more"] is False

        # 切 active 后 conv_test_b 标记为 active
        await agent.switch_session("conv_test_b")
        resp = await client.get("/api/sessions")
        data = await resp.json()
        by_id = {s["conversationId"]: s for s in data["sessions"]}
        assert by_id["conv_test_b"]["active"] is True
        assert by_id["conv_test_a"]["active"] is False

        # search 仅 title 大小写不敏感
        resp = await client.get("/api/sessions", params={"search": "ALPHA"})
        data = await resp.json()
        ids = [s["conversationId"] for s in data["sessions"]]
        assert ids == ["conv_test_a"]

    @pytest.mark.asyncio
    async def test_list_cursor_pagination(self, aiohttp_client):
        agent = _FakeAgent()
        for i in range(3):
            await _create_session(agent, f"conv_test_p{i}", f"会话{i}")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions", params={"limit": "2"})
        data = await resp.json()
        assert len(data["sessions"]) == 2
        assert data["has_more"] is True
        assert data["cursor"]

        resp2 = await client.get("/api/sessions", params={"limit": "2", "cursor": data["cursor"]})
        data2 = await resp2.json()
        # 下一页与第一页不重叠（3 conv + default = 4 → 第二页 2 条，含 default）
        first_ids = {s["conversationId"] for s in data["sessions"]}
        second_ids = {s["conversationId"] for s in data2["sessions"]}
        assert len(second_ids) == 2
        assert "default" in second_ids
        assert not (first_ids & second_ids)

    @pytest.mark.asyncio
    async def test_sessions_list_malformed_cursor_400(self, aiohttp_client):
        """畸形 cursor → 400 CONTRACT_VALIDATION（非 500）。"""
        agent = _FakeAgent()
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions?cursor=garbage")
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_detail_unknown_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/conv_no_such")
        assert resp.status == 404
        data = await resp.json()
        assert data["code"] == "SESSION_NOT_FOUND"
        assert data["retryable"] is False

    @pytest.mark.asyncio
    async def test_list_message_count_matches_detail(self, aiohttp_client):
        """review 修复（链路#8）：列表与详情 messageCount 同源（归档计数）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_mc", "计数会话")
        await _seed_text_messages(agent, "conv_test_mc", 7)
        await _create_session(agent, "conv_test_mc2", "空会话")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions")
        data = await resp.json()
        by_id = {s["conversationId"]: s for s in data["sessions"]}
        assert by_id["conv_test_mc"]["messageCount"] == 7
        assert by_id["conv_test_mc2"]["messageCount"] == 0

        resp2 = await client.get("/api/sessions/conv_test_mc")
        detail = await resp2.json()
        assert detail["session"]["messageCount"] == 7  # 与列表一致

    @pytest.mark.asyncio
    async def test_detail_returns_session_and_first_page(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_d", "详情会话")
        await _seed_text_messages(agent, "conv_test_d", 5)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_d")
        assert resp.status == 200
        data = await resp.json()
        assert data["session"]["conversationId"] == "conv_test_d"
        assert data["session"]["title"] == "详情会话"
        assert data["session"]["messageCount"] == 5
        assert data["session"]["active"] is False
        assert len(data["messages"]) > 0
        # 第一页倒序：最新消息在前
        timestamps = [m["timestamp"] for m in data["messages"]]
        assert timestamps == sorted(timestamps, reverse=True)
        assert data["active"] is False

    @pytest.mark.asyncio
    async def test_rename_and_missing_title_400(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_r", "旧标题")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.patch("/api/sessions/conv_test_r", json={"title": "新标题"})
        assert resp.status == 200
        data = await resp.json()
        assert data["title"] == "新标题"
        assert data["conversationId"] == "conv_test_r"

        # 缺 title → 400
        resp = await client.patch("/api/sessions/conv_test_r", json={})
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_offline_create_list_detail_and_rename_consistent(self, aiohttp_client):
        """Mongo 会话仓库离线时，REST create 的成功结果在后续读写接口中仍可见。"""
        agent = _FakeAgent()
        agent.session_repo = SessionRepository(db=None)
        agent.session_manager = SessionManager(
            repo=agent.session_repo, msg_repo=agent.message_repo)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        created_resp = await client.post("/api/sessions", json={"title": "离线会话"})
        assert created_resp.status == 200
        created = await created_resp.json()
        sid = created["conversationId"]

        list_resp = await client.get("/api/sessions")
        listed = {item["conversationId"]: item for item in (await list_resp.json())["sessions"]}
        assert listed[sid]["title"] == "离线会话"

        rename_resp = await client.patch(
            f"/api/sessions/{sid}", json={"title": "离线已改名"})
        assert rename_resp.status == 200
        assert (await rename_resp.json())["title"] == "离线已改名"

        detail_resp = await client.get(f"/api/sessions/{sid}")
        assert detail_resp.status == 200
        detail = await detail_resp.json()
        assert detail["session"]["title"] == "离线已改名"

    @pytest.mark.asyncio
    async def test_rename_unknown_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.patch("/api/sessions/conv_no_such", json={"title": "x"})
        assert resp.status == 404
        assert (await resp.json())["code"] == "SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_delete_cascades_and_active_fallback(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_del", "待删")
        await _seed_text_messages(agent, "conv_test_del", 3)
        await agent.switch_session("conv_test_del")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.delete("/api/sessions/conv_test_del")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["deleted"] == 3  # 联动删除归档消息

        # 删除的是活动会话 → active 回落 default
        assert agent.active_session_id == "default"
        # 会话不再存在
        resp = await client.get("/api/sessions/conv_test_del")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_unknown_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.delete("/api/sessions/conv_no_such")
        assert resp.status == 404
        assert (await resp.json())["code"] == "SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_delete_default_rejected_409(self, aiohttp_client):
        """删除 default 被拒绝：409 SESSION_PROTECTED，default 不被删除。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_keep", "保留会话")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.delete("/api/sessions/default")
        assert resp.status == 409
        data = await resp.json()
        assert data["code"] == "SESSION_PROTECTED"

        # default 仍在 repo 列表，其他会话不受影响
        resp2 = await client.get("/api/sessions")
        data2 = await resp2.json()
        ids = {s["conversationId"] for s in data2["sessions"]}
        assert "conv_test_keep" in ids
        assert agent.active_session_id == "default"

    @pytest.mark.asyncio
    async def test_delete_active_busy_rejected_409(self, aiohttp_client):
        """批次 C：删除活动会话且 agent 忙（生成/TTS 中）→ 409 AGENT_BUSY，不删除。

        删活动会话会 attach_session(default) 重绑 engine——忙时换绑与在途生成/TTS
        竞态，门禁与 activate 一致（防未保护 engine 换绑向量）。
        """
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_delbusy", "忙时待删")
        await agent.switch_session("conv_test_delbusy")
        agent.status = _FakeStatus(accepting=False)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.delete("/api/sessions/conv_test_delbusy")
        assert resp.status == 409
        data = await resp.json()
        assert data["code"] == "AGENT_BUSY"
        assert data["retryable"] is True
        # 未删除：会话仍存在，active 未回落
        assert agent.active_session_id == "conv_test_delbusy"
        assert await agent.session_manager.has_session("conv_test_delbusy") is True

    @pytest.mark.asyncio
    async def test_delete_non_active_allowed_while_busy(self, aiohttp_client):
        """批次 C：忙时删除非活动会话不拒绝（无 engine 换绑，不涉及竞态）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_nonactive", "非活动")
        await _create_session(agent, "conv_test_activebusy", "活动")
        await agent.switch_session("conv_test_activebusy")
        agent.status = _FakeStatus(accepting=False)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.delete("/api/sessions/conv_test_nonactive")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert agent.active_session_id == "conv_test_activebusy", "非活动删除不影响 active"
        assert await agent.session_manager.has_session("conv_test_nonactive") is False

    @pytest.mark.asyncio
    async def test_delete_session_with_pending_rejected_409(self, aiohttp_client):
        """9abc R3：会话有排队消息（已 ACK accepted 未出队）→ DELETE 409 AGENT_BUSY 不删除。

        loop 捕获 SessionNotFoundError 丢弃在途消息且无错误帧——客户端已收到
        user_message.ack{accepted} 却永久无响应。防护：该会话有 pending 时拒绝
        删除（retryable，先清空/等待），不产生孤儿排队消息。
        """
        from core.agent._types import _PendingMessage

        agent = _FakeAgent()
        await _create_session(agent, "conv_test_pending", "有排队消息")
        agent.pending_messages.append(_PendingMessage(
            text="排队消息", user_name="用户", request_id=RequestId("req_p"),
            content=None, message_id=None,
            conversation_id=SessionId("conv_test_pending"), protocol_version=2))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.delete("/api/sessions/conv_test_pending")
        assert resp.status == 409
        data = await resp.json()
        assert data["code"] == "AGENT_BUSY"
        assert data["retryable"] is True
        # 未删除：会话仍存在
        assert await agent.session_manager.has_session("conv_test_pending") is True

    @pytest.mark.asyncio
    async def test_delete_session_with_eventbus_or_inflight_work_rejected_409(self, aiohttp_client):
        """accepted work 已离开 FIFO 也受 manager 门禁保护，非活动会话同样不能删。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_reserved", "已接收工作")
        await agent.session_manager.reserve_work("conv_test_reserved", 2)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.delete("/api/sessions/conv_test_reserved")
        assert resp.status == 409
        data = await resp.json()
        assert data["code"] == "AGENT_BUSY"
        assert data["retryable"] is True
        assert await agent.session_manager.has_session("conv_test_reserved") is True

        agent.session_manager.release_work("conv_test_reserved")
        resp = await client.delete("/api/sessions/conv_test_reserved")
        assert resp.status == 200
        assert await agent.session_manager.has_session("conv_test_reserved") is False

    @pytest.mark.asyncio
    async def test_delete_allowed_when_pending_on_other_session(self, aiohttp_client):
        """9abc R3：pending 归属其他会话 → 本会话无 pending，正常删除（门禁按会话过滤）。"""
        from core.agent._types import _PendingMessage

        agent = _FakeAgent()
        await _create_session(agent, "conv_test_delclean", "待删")
        await _create_session(agent, "conv_test_other", "其他会话")
        agent.pending_messages.append(_PendingMessage(
            text="别的会话排队", user_name="用户", request_id=RequestId("req_o"),
            content=None, message_id=None,
            conversation_id=SessionId("conv_test_other"), protocol_version=2))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.delete("/api/sessions/conv_test_delclean")
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
        assert await agent.session_manager.has_session("conv_test_delclean") is False
        assert await agent.session_manager.has_session("conv_test_other") is True

    @pytest.mark.asyncio
    async def test_list_message_count_concurrent_gather(self, aiohttp_client):
        """批次 C：列表 messageCount 用 asyncio.gather 并发统计（N+1 修复），计数正确。"""
        agent = _FakeAgent()
        for i in range(5):
            await _create_session(agent, f"conv_test_g{i}", f"会话{i}")
            await _seed_text_messages(agent, f"conv_test_g{i}", i + 1)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        data = await resp.json()
        by_id = {s["conversationId"]: s for s in data["sessions"]}
        assert len(by_id) == 6  # 5 个 conv_test + default（clean-start 恒可见）
        for i in range(5):
            assert by_id[f"conv_test_g{i}"]["messageCount"] == i + 1, \
                f"conv_test_g{i} messageCount 应等于归档计数 {i + 1}"
        assert by_id["default"]["messageCount"] == 0

    @pytest.mark.asyncio
    async def test_activate_switches_and_returns_id(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_act", "激活会话")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.post("/api/sessions/conv_test_act/activate")
        assert resp.status == 200
        data = await resp.json()
        assert data["activeConversationId"] == "conv_test_act"
        assert agent.active_session_id == "conv_test_act"

    @pytest.mark.asyncio
    async def test_activate_unknown_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.post("/api/sessions/conv_no_such/activate")
        assert resp.status == 404
        assert (await resp.json())["code"] == "SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_activate_busy_rejected_409(self, aiohttp_client):
        """F1(a): agent 忙（生成/TTS 中，is_accepting_input=False）→ 409 AGENT_BUSY，不切换。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_busy", "忙时目标会话")
        agent.status = _FakeStatus(accepting=False)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.post("/api/sessions/conv_test_busy/activate")
        assert resp.status == 409
        data = await resp.json()
        assert data["code"] == "AGENT_BUSY"
        assert data["retryable"] is True
        assert agent.active_session_id == "default", "忙时不应切换 active"


# ───────────────────────── 会话内历史（§5.1） ──────────────────────────────


@pytest.mark.service
class TestMessagesHistory:
    @pytest.mark.asyncio
    async def test_page_before_limit_has_more(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_m")
        await _seed_text_messages(agent, "conv_test_m", 5)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_m/messages", params={"limit": "2"})
        assert resp.status == 200
        data = await resp.json()
        assert len(data["messages"]) == 2
        assert data["has_more"] is True
        assert data["total"] == 5
        # 倒序：最新的两条
        assert [m["message_id"] for m in data["messages"]] == ["msg_conv_test_m_4", "msg_conv_test_m_3"]

        # before 游标翻页 → 下一批，不重叠
        before = _msg_id_from_page(data, -1)  # 最后一条的 _id
        resp2 = await client.get("/api/sessions/conv_test_m/messages", params={"limit": "2", "before": before})
        data2 = await resp2.json()
        assert [m["message_id"] for m in data2["messages"]] == ["msg_conv_test_m_2", "msg_conv_test_m_1"]
        assert data2["has_more"] is True
        assert data2["total"] == 5

    @pytest.mark.asyncio
    async def test_assistant_history_exposes_character_snapshot_only_on_assistant(
        self, aiohttp_client,
    ):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_character")
        await _append(agent, "conv_test_character", UserMessage(
            content=[TextPart(content="hi")], message_id=MessageId("msg_user"),
        ))
        await _append(agent, "conv_test_character", AssistantMessage.model_validate({
            "content": [{"type": "text", "content": "hello"}],
            "message_id": "msg_assistant",
            "session_id": "conv_test_character",
            "character": {
                "character_id": "chr_builtin_cyrene",
                "character_instance_id": "chi_builtin_cyrene",
                "character_revision": 1,
                "display_name": "Cyrene",
                "presentation": {},
            },
        }))
        app = _make_app(agent)
        client = await aiohttp_client(app)
        response = await client.get("/api/sessions/conv_test_character/messages")
        assert response.status == 200
        messages = (await response.json())["messages"]
        by_id = {message["message_id"]: message for message in messages}
        assert "character" not in by_id["msg_user"]
        assert by_id["msg_assistant"]["character"] == {
            "characterId": "chr_builtin_cyrene",
            "characterInstanceId": "chi_builtin_cyrene",
            "characterRevision": 1,
            "displayName": "Cyrene",
            "presentation": {
                "avatarAssetId": None,
                "live2dModelId": None,
                "themeColor": None,
            },
        }

    @pytest.mark.asyncio
    async def test_malformed_before_cursor_400(self, aiohttp_client):
        """畸形 before 游标 → 400 CONTRACT_VALIDATION（验收修复：原 ObjectId 未捕获 500）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_bad")
        await _seed_text_messages(agent, "conv_test_bad", 2)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_bad/messages",
                                params={"before": "not-a-valid-objectid"})
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_after_incremental_asc(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_inc")
        await _seed_text_messages(agent, "conv_test_inc", 5)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        # 9A-2：after 只认 _id 游标（messageId 形式 → 400，见 test_invalid_cursor_400）——
        # 先取锚点 msg_2 的 Mongo _id
        resp = await client.get("/api/sessions/conv_test_inc/messages", params={"limit": "50"})
        by_id = {m["message_id"]: m["_id"] for m in (await resp.json())["messages"]}
        anchor = by_id["msg_conv_test_inc_2"]

        # 锚点之后的消息正序返回
        resp2 = await client.get("/api/sessions/conv_test_inc/messages", params={"after": anchor})
        assert resp2.status == 200
        data2 = await resp2.json()
        assert [m["message_id"] for m in data2["messages"]] == ["msg_conv_test_inc_3", "msg_conv_test_inc_4"]
        assert data2["total"] == 5

    @pytest.mark.asyncio
    async def test_page_returns_cursor(self, aiohttp_client):
        """9A-2：before 分页响应携带 cursor（=页内最旧消息 _id），回传 before=<cursor> 无重无漏。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_pc")
        await _seed_text_messages(agent, "conv_test_pc", 5)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_pc/messages", params={"limit": "2"})
        assert resp.status == 200
        data = await resp.json()
        assert len(data["messages"]) == 2
        assert data["has_more"] is True
        # cursor = 页内最旧消息（raw 最后一条）的 _id
        assert data["cursor"] == _msg_id_from_page(data, -1)
        first_ids = {m["message_id"] for m in data["messages"]}

        resp2 = await client.get("/api/sessions/conv_test_pc/messages",
                                 params={"limit": "2", "before": data["cursor"]})
        data2 = await resp2.json()
        assert len(data2["messages"]) == 2
        second_ids = {m["message_id"] for m in data2["messages"]}
        assert first_ids.isdisjoint(second_ids), "两页不重叠"
        assert data2["has_more"] is True
        # 两页无漏：前 4 条 = msg_4..msg_1
        assert first_ids | second_ids == {
            "msg_conv_test_pc_4", "msg_conv_test_pc_3",
            "msg_conv_test_pc_2", "msg_conv_test_pc_1"}

    @pytest.mark.asyncio
    async def test_after_cursor_pagination(self, aiohttp_client):
        """9A-2：after=<cursor> 正序分页两页无重无漏；末页无更多省略 cursor。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_ac")
        await _seed_text_messages(agent, "conv_test_ac", 5)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_ac/messages", params={"limit": "50"})
        by_id = {m["message_id"]: m["_id"] for m in (await resp.json())["messages"]}
        anchor = by_id["msg_conv_test_ac_1"]

        resp2 = await client.get("/api/sessions/conv_test_ac/messages",
                                 params={"after": anchor, "limit": "2"})
        data2 = await resp2.json()
        assert [m["message_id"] for m in data2["messages"]] == ["msg_conv_test_ac_2", "msg_conv_test_ac_3"]
        assert data2["has_more"] is True
        assert data2["cursor"] == by_id["msg_conv_test_ac_3"]

        resp3 = await client.get("/api/sessions/conv_test_ac/messages",
                                 params={"after": data2["cursor"], "limit": "2"})
        data3 = await resp3.json()
        assert [m["message_id"] for m in data3["messages"]] == ["msg_conv_test_ac_4"]
        assert data3["has_more"] is False
        assert "cursor" not in data3  # 无更多 → 省略

    @pytest.mark.asyncio
    async def test_invalid_cursor_400(self, aiohttp_client):
        """9A-2：before/after 只认 _id 游标——messageId 形式/非 hex → 400 CONTRACT_VALIDATION。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_ic")
        await _seed_text_messages(agent, "conv_test_ic", 3)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        for params in ({"before": "msg_conv_test_ic_1"},
                       {"after": "msg_conv_test_ic_1"},
                       {"after": "nothex"}):
            resp = await client.get("/api/sessions/conv_test_ic/messages", params=params)
            assert resp.status == 400, params
            assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_deleted_anchor_empty_page(self, aiohttp_client):
        """9A-2/Q8：after=已删锚点——锚点为会话最新 → 空页 + 无 cursor + has_more=false 不 500；
        删非最新锚点 → 返回更新的消息（_id > oid 过滤器语义，非空页）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_da")
        await _seed_text_messages(agent, "conv_test_da", 4)  # msg_0..msg_3
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_da/messages", params={"limit": "50"})
        by_id = {m["message_id"]: m["_id"] for m in (await resp.json())["messages"]}
        assert "msg_conv_test_da_3" in by_id

        # 删除锚点（会话最新一条 msg_3）→ after 无更近消息 → 空页 + False，不 500
        await agent.message_repo.delete_one(MessageId(by_id["msg_conv_test_da_3"]))
        resp2 = await client.get("/api/sessions/conv_test_da/messages",
                                 params={"after": by_id["msg_conv_test_da_3"]})
        assert resp2.status == 200
        data2 = await resp2.json()
        assert data2["messages"] == []
        assert data2["has_more"] is False
        assert "cursor" not in data2
        assert data2["total"] == 3

        # 删非最新锚点（msg_1）→ 返回更新的消息（_id > oid 语义，非空页）
        await agent.message_repo.delete_one(MessageId(by_id["msg_conv_test_da_1"]))
        resp3 = await client.get("/api/sessions/conv_test_da/messages",
                                 params={"after": by_id["msg_conv_test_da_1"]})
        assert resp3.status == 200
        data3 = await resp3.json()
        assert [m["message_id"] for m in data3["messages"]] == ["msg_conv_test_da_2"]
        assert data3["has_more"] is False

    @pytest.mark.asyncio
    async def test_page_cursor_from_raw_not_contract(self, aiohttp_client, monkeypatch):
        """9A-2：cursor 从 raw 消息取（契约转换前）——页内含契约化失败被丢弃的消息，cursor 仍指向 raw 边界。

        当前契约下 `_doc_to_message` 输出与 rest.Message 近同构，自然归档消息几乎不会契约化失败；
        用 stub query_page 注入一条 metadata._mongo_id 非 str 的 raw 坏消息（rest.Message 校验失败被丢弃），
        验证 cursor 仍取 raw 列表最后一条（非契约输出），下一 before 页不会因此重复/漏消息。
        """
        from unittest.mock import AsyncMock
        from core.schema.message import UserMessage
        from core.schema.part import TextPart

        agent = _FakeAgent()
        await _create_session(agent, "conv_test_rnc")
        raw_msgs = [
            UserMessage(content=[TextPart(content="ok")], message_id=MessageId("m0"),
                        timestamp="2026-08-01T10:00:00+00:00", metadata={"_mongo_id": "a" * 24}),
            UserMessage(content=[TextPart(content="bad")], message_id=MessageId("m1"),
                        timestamp="2026-08-01T10:00:01+00:00", metadata={"_mongo_id": 123}),
            UserMessage(content=[TextPart(content="ok2")], message_id=MessageId("m2"),
                        timestamp="2026-08-01T10:00:02+00:00", metadata={"_mongo_id": "b" * 24}),
        ]
        # stub 仓库查询：query_page 返回含坏消息的 raw 列表，count 返回总数
        monkeypatch.setattr(agent.message_repo, "query_page",
                            AsyncMock(return_value=(raw_msgs, True)))
        monkeypatch.setattr(agent.message_repo, "count_by_session", AsyncMock(return_value=3))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_rnc/messages", params={"limit": "2"})
        assert resp.status == 200
        data = await resp.json()
        # 坏消息（m1）契约化失败被丢弃，不出现在响应
        assert [m["message_id"] for m in data["messages"]] == ["m0", "m2"]
        assert data["has_more"] is True
        # cursor 从 raw 消息列表最后一条取（契约转换前）→ 指向 raw 边界
        assert data["cursor"] == "b" * 24
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_empty_session(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_empty")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/conv_test_empty/messages")
        assert resp.status == 200
        data = await resp.json()
        assert data["messages"] == []
        assert data["has_more"] is False
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_unknown_session_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/conv_no_such/messages")
        assert resp.status == 404
        assert (await resp.json())["code"] == "SESSION_NOT_FOUND"


# ───────────────────────── 消息上下文窗口（§5.2） ──────────────────────────


@pytest.mark.service
class TestMessageContext:
    @pytest.mark.asyncio
    async def test_missing_conversation_id_400(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/messages/msg_x/context")
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_context_window(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_ctx")
        await _seed_text_messages(agent, "conv_test_ctx", 6)  # msg_0..msg_5
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get(
            "/api/messages/msg_conv_test_ctx_3/context",
            params={"conversationId": "conv_test_ctx", "before": "2", "after": "2"})
        assert resp.status == 200
        data = await resp.json()
        assert data["conversationId"] == "conv_test_ctx"
        assert data["anchor"]["message_id"] == "msg_conv_test_ctx_3"
        # before 窗口（正序，锚点前 2 条）
        assert [m["message_id"] for m in data["before"]] == ["msg_conv_test_ctx_1", "msg_conv_test_ctx_2"]
        # after 窗口（正序，锚点后 2 条）
        assert [m["message_id"] for m in data["after"]] == ["msg_conv_test_ctx_4", "msg_conv_test_ctx_5"]

    @pytest.mark.asyncio
    async def test_context_anchor_not_found_404(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_ctx")
        await _seed_text_messages(agent, "conv_test_ctx", 3)
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get(
            "/api/messages/msg_ghost/context",
            params={"conversationId": "conv_test_ctx"})
        assert resp.status == 404
        # review 修复（兼容#4）：消息不存在 → RESOURCE_NOT_FOUND（非 SESSION_NOT_FOUND）
        data = await resp.json()
        assert data["code"] == "RESOURCE_NOT_FOUND"


# ───────────────────────── 搜索（§5.3） ────────────────────────────────────


@pytest.mark.service
class TestSearchMessages:
    @pytest.mark.asyncio
    async def test_query_search_basic(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_s")
        await _seed_text_messages(agent, "conv_test_s", 3, prefix="hello world")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={"query": "WORLD"})
        assert resp.status == 200
        data = await resp.json()
        assert len(data["items"]) == 3
        item = data["items"][0]
        assert item["conversationId"] == "conv_test_s"
        assert item["role"] in ("user", "assistant")
        assert "segmentId" not in item  # 恒缺省
        # 搜索按 timestamp DESC 排序 → 最新消息在前；snippet = 前 100 字
        assert item["snippet"] in {"hello world0", "hello world1", "hello world2"}

    @pytest.mark.asyncio
    async def test_search_conversation_filter(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_s1")
        await _create_session(agent, "conv_test_s2")
        await _seed_text_messages(agent, "conv_test_s1", 2, prefix="apple")
        await _seed_text_messages(agent, "conv_test_s2", 2, prefix="apple")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={"query": "apple", "conversationId": "conv_test_s1"})
        data = await resp.json()
        assert len(data["items"]) == 2
        assert {i["conversationId"] for i in data["items"]} == {"conv_test_s1"}

    @pytest.mark.asyncio
    async def test_search_invalid_timezone_400(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_tz")
        await _seed_text_messages(agent, "conv_test_tz", 2, prefix="tz")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={
            "query": "tz", "date_from": "2026-08-01", "timezone": "Mars/Olympus"})
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "INVALID_TIMEZONE"

    @pytest.mark.asyncio
    async def test_search_date_range(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_date")
        await _append(agent, "conv_test_date", UserMessage(
            content=[TextPart(content="old day")], message_id=MessageId("m_old"),
            timestamp="2026-07-31T12:00:00+00:00"))
        await _append(agent, "conv_test_date", UserMessage(
            content=[TextPart(content="new day")], message_id=MessageId("m_new"),
            timestamp="2026-08-01T12:00:00+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        # 自然日 08-01 含整天：[08-01 00:00, 08-02 00:00)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-08-01", "date_to": "2026-08-01", "timezone": "UTC"})
        data = await resp.json()
        ids = [i["messageId"] for i in data["items"]]
        assert ids == ["m_new"]

    @pytest.mark.asyncio
    async def test_search_category_image(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_img")
        await _append(agent, "conv_test_img", UserMessage(
            content=[TextPart(content="图"), ImagePart(src="http://x/a.png")],
            message_id=MessageId("m_img"), timestamp="2026-08-01T10:00:00+00:00"))
        await _append(agent, "conv_test_img", UserMessage(
            content=[TextPart(content="纯文本")],
            message_id=MessageId("m_txt"), timestamp="2026-08-01T10:01:00+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={"category": "image"})
        data = await resp.json()
        assert [i["messageId"] for i in data["items"]] == ["m_img"]

    @pytest.mark.asyncio
    async def test_search_response_id_best_effort(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_rid")
        await _append(agent, "conv_test_rid", AssistantMessage(
            content=[TextPart(content="回复正文")], message_id=MessageId("m_asst"),
            timestamp="2026-08-01T10:00:00+00:00"))
        await _seed_generation(agent, "conv_test_rid", "gen_rid_1", message_id="m_asst")
        await _append(agent, "conv_test_rid", UserMessage(
            content=[TextPart(content="没有 generation")], message_id=MessageId("m_no_gen"),
            timestamp="2026-08-01T10:01:00+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={"query": "回复"})
        data = await resp.json()
        assert data["items"][0]["responseId"] == "gen_rid_1"

        resp2 = await client.get("/api/search/messages", params={"query": "没有 generation"})
        data2 = await resp2.json()
        assert "responseId" not in data2["items"][0]  # 无反查结果 → 缺省

    @pytest.mark.asyncio
    async def test_search_category_tool_returns_tool_messages(self, aiohttp_client):
        """链路#4: category=tool 返回 tool 消息（role=tool），不因契约过滤被丢弃。"""
        from core.schema.message import ToolResultMessage

        agent = _FakeAgent()
        await _create_session(agent, "conv_test_tool")
        await _append(agent, "conv_test_tool", ToolResultMessage(
            content=[TextPart(content="工具执行结果：找到了数据")],
            tool_call_id="tc_1", tool_name="web_search", message_id=MessageId("m_tool1"),
            timestamp="2026-08-01T10:00:00+00:00"))
        await _append(agent, "conv_test_tool", UserMessage(
            content=[TextPart(content="普通消息")], message_id=MessageId("m_user1"),
            timestamp="2026-08-01T10:00:01+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={"category": "tool"})
        assert resp.status == 200
        data = await resp.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["role"] == "tool"
        assert item["messageId"] == "m_tool1"
        assert "找到了数据" in item["snippet"]

    @pytest.mark.asyncio
    async def test_search_unknown_category_400(self, aiohttp_client):
        """批次 8a D4：category 白名单外取值 → 400 CONTRACT_VALIDATION。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_cat")
        await _seed_text_messages(agent, "conv_test_cat", 1, prefix="cat")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={"category": "bogus"})
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_search_snippet_first_100(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_snip")
        long_text = "长" * 150
        await _append(agent, "conv_test_snip", UserMessage(
            content=[TextPart(content=long_text)], message_id=MessageId("m_long"),
            timestamp="2026-08-01T10:00:00+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={"query": "长"})
        data = await resp.json()
        assert len(data["items"][0]["snippet"]) == 100

    @pytest.mark.asyncio
    async def test_search_limit_cap_100(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_cap")
        await _seed_text_messages(agent, "conv_test_cap", 105, prefix="cap")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/search/messages", params={"query": "cap", "limit": "1000"})
        data = await resp.json()
        assert len(data["items"]) == 100  # 上限 100
        assert data["has_more"] is True
        assert data["cursor"]


# ───────────────────────── 资源索引（§8） ───────────────────────────────────


@pytest.mark.service
class TestResources:
    @pytest.mark.asyncio
    async def test_image_extraction(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_res")
        await _append(agent, "conv_test_res", UserMessage(
            content=[TextPart(content="配图"), ImagePart(src="http://x/pic.png", mime="image/png")],
            message_id=MessageId("m_res_img"), timestamp="2026-08-01T10:00:00+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_res/resources", params={"category": "image"})
        assert resp.status == 200
        data = await resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["kind"] == "image"
        assert data["items"][0]["url"] == "http://x/pic.png"
        assert data["items"][0]["messageId"] == "m_res_img"
        assert data["has_more"] is False
        assert "cursor" not in data

    @pytest.mark.asyncio
    async def test_link_extraction_from_text(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_link")
        await _append(agent, "conv_test_link", UserMessage(
            content=[TextPart(content="看这个 https://example.com/docs 和 http://x.io/y 很有用")],
            message_id=MessageId("m_link"), timestamp="2026-08-01T10:00:00+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_link/resources", params={"category": "link"})
        data = await resp.json()
        urls = [i["url"] for i in data["items"]]
        assert "https://example.com/docs" in urls
        assert "http://x.io/y" in urls
        assert all(i["kind"] == "link" for i in data["items"])

    @pytest.mark.asyncio
    async def test_video_audio_file_parts(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_va")
        await _append(agent, "conv_test_va", AssistantMessage(
            content=[VideoPart(src="http://x/v.mp4"), AudioPart(src="http://x/a.wav"),
                     FilePart(src="http://x/f.bin", filename="f.bin")],
            message_id=MessageId("m_va"), timestamp="2026-08-01T10:00:00+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        for cat, want in (("video", "http://x/v.mp4"), ("audio", "http://x/a.wav"), ("file", "http://x/f.bin")):
            resp = await client.get(f"/api/sessions/conv_test_va/resources", params={"category": cat})
            data = await resp.json()
            assert [i["url"] for i in data["items"]] == [want], cat

    @pytest.mark.asyncio
    async def test_sticker_always_empty(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_stk")
        await _seed_text_messages(agent, "conv_test_stk", 3)
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/conv_test_stk/resources", params={"category": "sticker"})
        assert resp.status == 200
        data = await resp.json()
        assert data["items"] == []
        assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_text_category_unsupported_400(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_txt")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/conv_test_txt/resources", params={"category": "text"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_cursor_peek_has_more(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_cur")
        # 5 条图片消息 + 2 条纯文本消息（穿插，验证跳过非命中）
        for i in range(5):
            await _append(agent, "conv_test_cur", UserMessage(
                content=[ImagePart(src=f"http://x/img{i}.png", mime="image/png")],
                message_id=MessageId(f"m_img{i}"), timestamp=f"2026-08-01T10:00:{i:02d}+00:00"))
        await _append(agent, "conv_test_cur", UserMessage(
            content=[TextPart(content="no image")], message_id=MessageId("m_txt0"),
            timestamp="2026-08-01T10:00:05+00:00"))
        await _append(agent, "conv_test_cur", UserMessage(
            content=[TextPart(content="no image 2")], message_id=MessageId("m_txt1"),
            timestamp="2026-08-01T10:00:06+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_cur/resources",
                                params={"category": "image", "limit": "2"})
        data = await resp.json()
        assert len(data["items"]) == 2
        assert data["has_more"] is True
        assert data["cursor"]  # 扫描停止处 _id
        assert {i["messageId"] for i in data["items"]} == {"m_img4", "m_img3"}  # 最新两条命中

        # 下页带 before=cursor → 继续，不重叠、不重复扫描跳过区段
        resp2 = await client.get("/api/sessions/conv_test_cur/resources",
                                 params={"category": "image", "limit": "2", "before": data["cursor"]})
        data2 = await resp2.json()
        assert {i["messageId"] for i in data2["items"]} == {"m_img2", "m_img1"}
        assert data2["has_more"] is True

        resp3 = await client.get("/api/sessions/conv_test_cur/resources",
                                 params={"category": "image", "limit": "2", "before": data2["cursor"]})
        data3 = await resp3.json()
        assert {i["messageId"] for i in data3["items"]} == {"m_img0"}
        assert data3["has_more"] is False

    @pytest.mark.asyncio
    async def test_resource_cursor_multiple_resources_in_message(self, aiohttp_client):
        """链路#2: 单条消息含多个同类资源 + limit 命中消息内部 → 资源级 cursor，两页各 2+1 不丢。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_multi")
        # 单条消息含 3 个链接
        await _append(agent, "conv_test_multi", UserMessage(
            content=[TextPart(content="a https://x/1 b https://x/2 c https://x/3")],
            message_id=MessageId("m_multi"), timestamp="2026-08-01T10:00:00+00:00"))
        # 较新消息含 1 个链接（用于 has_more/跨消息扫描）
        await _append(agent, "conv_test_multi", UserMessage(
            content=[TextPart(content="https://x/4")],
            message_id=MessageId("m_single"), timestamp="2026-08-01T10:00:01+00:00"))
        app = _make_app(agent)
        client = await aiohttp_client(app)

        # 第一页 limit=2：m_single 的 1 个 + m_multi 的第一个（最新在前）
        resp = await client.get("/api/sessions/conv_test_multi/resources",
                                params={"category": "link", "limit": "2"})
        data = await resp.json()
        assert len(data["items"]) == 2
        assert data["has_more"] is True
        assert data["cursor"]
        urls1 = {i["url"] for i in data["items"]}
        assert urls1 == {"https://x/4", "https://x/1"}

        # 第二页：cursor 指向消息内 offset——m_multi 剩余 2 个链接不丢失（消息级 cursor 会丢）
        resp2 = await client.get("/api/sessions/conv_test_multi/resources",
                                 params={"category": "link", "limit": "2", "before": data["cursor"]})
        data2 = await resp2.json()
        assert len(data2["items"]) == 2
        assert {i["url"] for i in data2["items"]} == {"https://x/2", "https://x/3"}
        assert data2["has_more"] is False
        assert urls1.isdisjoint({i["url"] for i in data2["items"]}), "两页不重叠、不丢资源"

    @pytest.mark.asyncio
    async def test_unknown_session_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/conv_no_such/resources", params={"category": "image"})
        assert resp.status == 404
        assert (await resp.json())["code"] == "SESSION_NOT_FOUND"


# ───────────────────────── generation 状态（§7.2） ──────────────────────────


@pytest.mark.service
class TestGenerationStatus:
    @pytest.mark.asyncio
    async def test_list_and_detail(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_gen")
        await _seed_generation(agent, "conv_test_gen", "gen_1", message_id="m_asst_1", status="completed")
        await _seed_generation(agent, "conv_test_gen", "gen_2", message_id="m_asst_2", status="running")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_gen/generations")
        assert resp.status == 200
        data = await resp.json()
        gens = {g["generationId"]: g for g in data["generations"]}
        assert set(gens) == {"gen_1", "gen_2"}
        g1 = gens["gen_1"]
        assert g1["conversationId"] == "conv_test_gen"
        assert g1["messageId"] == "m_asst_1"
        assert g1["userMessageId"] == "um_x"
        assert g1["status"] == "completed"
        assert g1["finishReason"] == "stop"
        assert g1["model"] == "deepseek/deepseek-v4-flash"
        assert g1["createdAt"]

        # 详情
        resp2 = await client.get("/api/generations/gen_1")
        assert resp2.status == 200
        detail = await resp2.json()
        assert detail["generationId"] == "gen_1"
        assert detail["status"] == "completed"

    @pytest.mark.asyncio
    async def test_status_filter(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_gf")
        await _seed_generation(agent, "conv_test_gf", "gen_a", message_id="m_a", status="completed")
        await _seed_generation(agent, "conv_test_gf", "gen_b", message_id="m_b", status="running")
        await _seed_generation(agent, "conv_test_gf", "gen_c", message_id="m_c", status="failed")
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_gf/generations", params={"status": "running"})
        data = await resp.json()
        assert [g["generationId"] for g in data["generations"]] == ["gen_b"]

        # failed 的 error 是 str（loop 存储形状）→ 契约归一为 dict
        resp = await client.get("/api/sessions/conv_test_gf/generations", params={"status": "failed"})
        data = await resp.json()
        failed = data["generations"][0]
        assert failed["error"] == {"message": "boom", "retryable": False}

    @pytest.mark.asyncio
    async def test_keyset_cursor_pagination(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_gc")
        for i, status in enumerate(("completed", "running", "failed", "cancelled")):
            await _seed_generation(agent, "conv_test_gc", f"gen_{i}", message_id=f"m_{i}", status=status)
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_gc/generations", params={"limit": "2"})
        data = await resp.json()
        assert len(data["generations"]) == 2
        assert data["has_more"] is True
        assert data["cursor"]

        resp2 = await client.get("/api/sessions/conv_test_gc/generations",
                                 params={"limit": "2", "cursor": data["cursor"]})
        data2 = await resp2.json()
        first_ids = {g["generationId"] for g in data["generations"]}
        second_ids = {g["generationId"] for g in data2["generations"]}
        assert not (first_ids & second_ids)

    @pytest.mark.asyncio
    async def test_unknown_session_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/conv_no_such/generations")
        assert resp.status == 404
        assert (await resp.json())["code"] == "SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_generation_detail_not_found_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/generations/gen_ghost")
        assert resp.status == 404
        data = await resp.json()
        assert data["code"] == "GENERATION_NOT_FOUND"
        assert data["retryable"] is False


# ───────────────────────── pending 查询（§7.3） ─────────────────────────────


@pytest.mark.service
class TestPending:
    def _pending(self, text: str, message_id: str, conversation_id: str | None):
        from core.agent._types import _PendingMessage

        return _PendingMessage(
            text=text, user_name="u", request_id=RequestId(f"req_{message_id}"),
            message_id=message_id, conversation_id=conversation_id)

    @pytest.mark.asyncio
    async def test_pending_messages_filtered_by_session(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_p1")
        await _create_session(agent, "conv_test_p2")
        agent.pending_messages = [
            self._pending("会话一的消息", "pm_1", "conv_test_p1"),
            self._pending("会话二的消息", "pm_2", "conv_test_p2"),
        ]
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_p1/pending")
        assert resp.status == 200
        data = await resp.json()
        assert [m["messageId"] for m in data["pendingMessages"]] == ["pm_1"]
        assert [m["snippet"] for m in data["pendingMessages"]] == ["会话一的消息"]

    @pytest.mark.asyncio
    async def test_pending_none_conversation_only_active_session(self, aiohttp_client):
        """conversation_id=None（v1/未归属）pending 仅活动会话展示，非活动 sid 不展示。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_active")
        await _create_session(agent, "conv_other")
        # 活动会话 = conv_active（显式 switch）
        await agent.switch_session("conv_active")
        agent.pending_messages = [
            self._pending("未归属消息", "pm_none", None),
            self._pending("other 会话消息", "pm_other", "conv_other"),
        ]
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_active/pending")
        data = await resp.json()
        ids_active = [m["messageId"] for m in data["pendingMessages"]]
        assert "pm_none" in ids_active        # None 归属 → 活动会话可见

        resp2 = await client.get("/api/sessions/conv_other/pending")
        data2 = await resp2.json()
        ids_other = [m["messageId"] for m in data2["pendingMessages"]]
        assert "pm_other" in ids_other
        assert "pm_none" not in ids_other     # None 归属不在非活动会话

    @pytest.mark.asyncio
    async def test_pending_approvals_global(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_ga")
        await _create_session(agent, "conv_test_gb")
        # 原地 extend（_FakeToolManager 持有同一 list 引用，重绑会断链）
        agent._approvals.extend([
            ("req_1", "web_search", {"q": "x"}), ("req_2", "read_file", {"p": "/tmp"})])
        app = _make_app(agent)
        client = await aiohttp_client(app)

        # 两个会话的 pending 都返回全局审批
        for sid in ("conv_test_ga", "conv_test_gb"):
            resp = await client.get(f"/api/sessions/{sid}/pending")
            data = await resp.json()
            assert [a["requestId"] for a in data["pendingApprovals"]] == ["req_1", "req_2"]
            assert data["pendingApprovals"][0]["tool"] == "web_search"
            assert data["pendingApprovals"][0]["params"] == {"q": "x"}

    @pytest.mark.asyncio
    async def test_pending_snippet_first_50(self, aiohttp_client):
        agent = _FakeAgent()
        await _create_session(agent, "conv_test_ps")
        long_text = "啊" * 60
        agent.pending_messages = [self._pending(long_text, "pm_long", "conv_test_ps")]
        app = _make_app(agent)
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/conv_test_ps/pending")
        data = await resp.json()
        assert len(data["pendingMessages"][0]["snippet"]) == 50

    @pytest.mark.asyncio
    async def test_unknown_session_404(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions/conv_no_such/pending")
        assert resp.status == 404
        assert (await resp.json())["code"] == "SESSION_NOT_FOUND"


# ───────────────────────── 认证 401（批次 1 接入） ──────────────────────────


@pytest.mark.service
class TestSessionRoutesAuth:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/sessions"),
        ("POST", "/api/sessions"),
        ("GET", "/api/sessions/conv_x"),
        ("PATCH", "/api/sessions/conv_x"),
        ("DELETE", "/api/sessions/conv_x"),
        ("POST", "/api/sessions/conv_x/activate"),
        ("GET", "/api/sessions/conv_x/messages"),
        ("GET", "/api/sessions/conv_x/resources"),
        ("GET", "/api/sessions/conv_x/generations"),
        ("GET", "/api/sessions/conv_x/pending"),
        ("GET", "/api/messages/m1/context"),
        ("GET", "/api/search/messages"),
        ("GET", "/api/generations/gen_x"),
    ])
    @pytest.mark.asyncio
    async def test_anonymous_all_401(self, aiohttp_client, method, path):
        app = _make_app(_FakeAgent(), access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.request(method, path)
        assert resp.status == 401
        data = await resp.json()
        assert data["code"] in ("AUTH_REQUIRED", "AUTH_INVALID")

    @pytest.mark.asyncio
    async def test_valid_token_passes(self, aiohttp_client):
        app = _make_app(_FakeAgent(), access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions", headers={"Authorization": "Bearer secret"})
        assert resp.status == 200


# ─────────────────── 存量空 ID 工具消息导出回归（spec 6.3/6.8） ───────────────


@pytest.mark.unit
class TestArchiveExportNeverEmitsEmptyMessageId:
    """存量空 ID 工具文档经 archive→wire→contract 链路产出非空稳定 ID（spec 6.3/6.8）。

    这正是 Electron 阻断链：_doc_to_message（读侧派生）→ _archive_to_client →
    _archive_msg_to_contract（min_length=1）→ 前端 z.string().min(1)。
    """

    def test_legacy_empty_id_derived_through_export_chain(self):
        from core.input._common import _archive_msg_to_contract, _archive_to_client
        from core.schema.adapters.message_adapter import validate_message_legacy_compat

        doc = {
            "role": "tool", "message_id": "", "tool_call_id": "call_legacy_1",
            "timestamp": "2026-08-13T00:00:00Z", "tool_name": "get_time",
            "content": [{"type": "text", "content": "legacy"}],
        }
        msg = validate_message_legacy_compat(doc)          # 读侧派生（Task 3）
        wire = _archive_to_client([msg])[0]
        contract = _archive_msg_to_contract(wire)          # min_length=1（Task 6）
        assert contract is not None
        assert contract.message_id.startswith("msg_")

    def test_search_export_uses_same_derivation(self):
        """搜索出口经 _doc_to_message → 适配器派生，与历史同一派生键。"""
        from core.storage.message_repo import _doc_to_message

        doc = {
            "_id": "0123456789abcdef01234567", "session_id": "default",
            "role": "tool", "message_id": "", "tool_call_id": "call_legacy_2",
            "timestamp": "2026-08-13T00:00:01Z", "tool_name": "web_search",
            "content": [{"type": "text", "content": "hit"}],
        }
        msg = _doc_to_message(doc)
        assert str(msg.message_id).startswith("msg_")
        assert msg.metadata.get("_mongo_id") == "0123456789abcdef01234567"


@pytest.mark.service
class TestSearchSticker:
    """P0-01：/api/search/messages?category=sticker（spec 阶段 3 联调整改）。

    前端 HistoryCategory.sticker 已冻结 → category=sticker；后端白名单 + repo 集合补
    sticker，走 {"type": "sticker"} $elemMatch。纯贴图消息 snippet 空串合法。
    """

    @staticmethod
    async def _seed_stickers(agent, sid: str, sticker_ids: list[str]) -> None:
        """按序播种纯贴图 user 消息（StickerPart）。

        src 对齐生产构造（ws_v2.py：/static/stickers/{id}.png）——StickerPart 的
        src 字段读回时经 _validate_src 非空校验（schema/utils.py）。
        """
        for i, st_id in enumerate(sticker_ids):
            await _append(agent, sid, UserMessage(
                content=[StickerPart(sticker_id=st_id,
                                     src=f"/static/stickers/{st_id}.png")],
                message_id=MessageId(f"msg_stk_{sid}_{i}"),
                timestamp=f"2026-08-01T09:{i:02d}:00+00:00",
            ))

    @pytest.mark.asyncio
    async def test_sticker_category_200(self, aiohttp_client):
        """category=sticker 返回 200（不再 400）且命中贴图消息。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk1")
        await self._seed_stickers(agent, "conv_stk1", ["st_001", "st_002"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={"category": "sticker"})
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_sticker_snippet_empty(self, aiohttp_client):
        """纯贴图消息 snippet 空串（前端按文本行 + category chip 展示，spec 2.2.5）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk2")
        await self._seed_stickers(agent, "conv_stk2", ["st_001"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        data = await (await client.get(
            "/api/search/messages", params={"category": "sticker"})).json()
        assert data["items"][0]["snippet"] == ""

    @pytest.mark.asyncio
    async def test_sticker_conversation_scope(self, aiohttp_client):
        """当前会话范围只返回该会话贴图消息。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_a")
        await _create_session(agent, "conv_stk_b")
        await self._seed_stickers(agent, "conv_stk_a", ["st_001"])
        await self._seed_stickers(agent, "conv_stk_b", ["st_002"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        data = await (await client.get("/api/search/messages", params={
            "category": "sticker", "conversationId": "conv_stk_a"})).json()
        assert [i["conversationId"] for i in data["items"]] == ["conv_stk_a"]

    @pytest.mark.asyncio
    async def test_sticker_cross_session_paginate(self, aiohttp_client):
        """全部会话跨会话 + cursor 第二页无重复无遗漏。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_c1")
        await _create_session(agent, "conv_stk_c2")
        await self._seed_stickers(agent, "conv_stk_c1", ["st_001", "st_002"])
        await self._seed_stickers(agent, "conv_stk_c2", ["st_003", "st_004"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        p1 = await (await client.get("/api/search/messages", params={
            "category": "sticker", "limit": "2"})).json()
        assert len(p1["items"]) == 2 and p1["has_more"] is True
        p2 = await (await client.get("/api/search/messages", params={
            "category": "sticker", "limit": "2", "cursor": p1["cursor"]})).json()
        ids = [i["messageId"] for i in p1["items"]] + [i["messageId"] for i in p2["items"]]
        assert len(set(ids)) == 4, f"cursor 分页重复或遗漏: {ids}"

    @pytest.mark.asyncio
    async def test_sticker_text_plus_sticker_matches(self, aiohttp_client):
        """文字+贴图消息命中；纯贴图 + 关键词 query 不命中（无 text part）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_mix")
        await _append(agent, "conv_stk_mix", UserMessage(
            content=[TextPart(content="看看这个"),
                     StickerPart(sticker_id="st_001",
                                 src="/static/stickers/st_001.png")],
            message_id=MessageId("msg_stk_mix0"),
            timestamp="2026-08-01T09:00:00+00:00",
        ))
        await self._seed_stickers(agent, "conv_stk_mix", ["st_002"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        # 无 query：文字+贴图 与 纯贴图 都命中
        data = await (await client.get(
            "/api/search/messages", params={"category": "sticker"})).json()
        assert len(data["items"]) == 2
        # 有 query：纯贴图不命中，文字+贴图命中（text part 命中关键词）
        data2 = await (await client.get("/api/search/messages", params={
            "category": "sticker", "query": "看看"})).json()
        assert [i["messageId"] for i in data2["items"]] == ["msg_stk_mix0"]

    @pytest.mark.asyncio
    async def test_sticker_items_locatable(self, aiohttp_client):
        """返回项非空 conversationId/messageId/timestamp（可跳转来源/context）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_loc")
        await self._seed_stickers(agent, "conv_stk_loc", ["st_001"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        item = (await (await client.get(
            "/api/search/messages", params={"category": "sticker"})).json())["items"][0]
        assert item["conversationId"] and item["messageId"] and item["timestamp"]

    @pytest.mark.asyncio
    async def test_sticker_unknown_category_still_400(self, aiohttp_client):
        """未知 category 仍 400——加入 sticker 不放宽为任意字符串。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_unk")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={"category": "bogus"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"


@pytest.mark.service
class TestSearchDateFailFast:
    """P1-01：日期参数 handler 层 fail-fast（spec 阶段 3 联调整改）。

    非 YYYY-MM-DD → 400 CONTRACT_VALIDATION（不再吞 200 空结果）；未知时区仍
    INVALID_TIMEZONE。与前端 FE-S3-INT-P0-01（改发自然日）同步放行。
    """

    @pytest.mark.asyncio
    async def test_date_iso_datetime_400(self, aiohttp_client):
        """ISO datetime 负例（前端当前错误形态）→ 400 CONTRACT_VALIDATION。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_iso")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "query": "x", "date_from": "2026-08-13T16:00:00.000Z"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_date_compact_400(self, aiohttp_client):
        """紧凑日期 20260814 → 400（fromisoformat 接受但契约要求 YYYY-MM-DD）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_cpt")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "query": "x", "date_to": "20260814"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_date_week_calendar_400(self, aiohttp_client):
        """ISO 周历 2026-W33-4 → 400。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_wk")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "query": "x", "date_from": "2026-W33-4"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_date_invalid_calendar_400(self, aiohttp_client):
        """非法日历 2026-13-99 → 400（正则过、fromisoformat 拒）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_bad")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "query": "x", "date_from": "2026-13-99"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_date_valid_range_still_200(self, aiohttp_client):
        """合法自然日 range 仍 200 且命中（UTC 当天）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_ok")
        await _seed_text_messages(agent, "conv_date_ok", 2, prefix="day")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-08-01", "date_to": "2026-08-01",
            "timezone": "UTC"})
        assert resp.status == 200
        assert len((await resp.json())["items"]) == 2

    @pytest.mark.asyncio
    async def test_date_from_only_and_to_only(self, aiohttp_client):
        """仅 from / 仅 to 均 200 且半开区间命中。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_ft")
        await _seed_text_messages(agent, "conv_date_ft", 1, prefix="edge")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        r1 = await client.get("/api/search/messages", params={
            "date_from": "2026-08-01", "timezone": "UTC"})
        r2 = await client.get("/api/search/messages", params={
            "date_to": "2026-08-01", "timezone": "UTC"})
        assert r1.status == r2.status == 200

    @pytest.mark.asyncio
    async def test_date_unknown_timezone_still_400(self, aiohttp_client):
        """未知时区保持 400 INVALID_TIMEZONE（不与日期格式校验混淆）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_tz")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-08-01", "timezone": "Mars/Olympus"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "INVALID_TIMEZONE"

    @pytest.mark.asyncio
    async def test_date_dst_boundary(self, aiohttp_client):
        """DST 切换日自然日 → 23h 半开 UTC 区间命中（America/New_York 2026-03-08）。

        3/8 美东 = [2026-03-08T05:00:00Z, 2026-03-09T04:00:00Z)（凌晨跳 1 小时）：
        当天 12:00Z 命中；3/7 美东 23:30（= 3/8 04:30Z）不命中。
        """
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_dst")
        await _append(agent, "conv_date_dst", UserMessage(
            content=[TextPart(content="dst")],
            message_id=MessageId("msg_dst1"),
            timestamp="2026-03-08T12:00:00+00:00",  # 区间内
        ))
        await _append(agent, "conv_date_dst", UserMessage(
            content=[TextPart(content="dst-eve")],
            message_id=MessageId("msg_dst0"),
            timestamp="2026-03-08T04:30:00+00:00",  # 3/7 美东 23:30，区间外
        ))
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-03-08", "date_to": "2026-03-08",
            "timezone": "America/New_York"})
        assert resp.status == 200
        items = (await resp.json())["items"]
        assert [i["messageId"] for i in items] == ["msg_dst1"]

    @pytest.mark.asyncio
    async def test_date_from_gt_to_returns_empty_200(self, aiohttp_client):
        """date_from > date_to（倒置）保持 200 空结果（D2，不扩大范围，回归保险）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_inv")
        await _seed_text_messages(agent, "conv_date_inv", 1, prefix="inv")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-08-02", "date_to": "2026-08-01", "timezone": "UTC"})
        assert resp.status == 200
        assert (await resp.json())["items"] == []


# ---------------------------------------------------------------------------
# 读出期语义区分（问题单 §5.2 ★★☆）：缺口告警，wire 不变
# ---------------------------------------------------------------------------

class TestCharacterSnapshotCauseDiagnostics:
    """会话列表的角色快照取不到时，按成因分别处置——wire 不变，仅服务端可见性。

    legacy 无绑定（合法）静默；字段残缺/悬空引用（数据缺口）显著告警并带会话 ID，
    把前端「整页 fail-closed」的定位信息提前到后端日志。
    """

    @pytest.mark.asyncio
    async def test_gap_causes_log_warning_with_session_id(self, aiohttp_client, caplog):
        import logging

        from core.character.session_binding import SnapshotUnavailableCause

        for cause in (SnapshotUnavailableCause.INCOMPLETE_BINDING,
                      SnapshotUnavailableCause.DANGLING_REFERENCE):
            agent = _FakeAgent()
            await _create_session(agent, "conv_gap_probe", "缺口探针")
            app = _make_app(agent)
            app["character_session_binding_service"].snapshot_with_cause = AsyncMock(
                return_value=(None, cause))
            client = await aiohttp_client(app)
            with caplog.at_level(logging.WARNING):
                resp = await client.get("/api/sessions")
            assert resp.status == 200
            hits = [r.getMessage() for r in caplog.records
                    if "会话角色数据缺口" in r.getMessage()]
            assert hits, f"{cause} 应产生缺口告警"
            # 列表端点对每个会话各判一次（default 同为本探针对象）——故按「存在」断言，
            # 且必须能定位到目标会话。
            target = [h for h in hits if "conv_gap_probe" in h]
            assert target, f"告警须带会话 ID 以便定位；实得: {hits}"
            assert cause.value in target[-1]

    @pytest.mark.asyncio
    async def test_legacy_absent_is_silent(self, aiohttp_client, caplog):
        """legacy 无绑定是合法形态 → 不告警（避免刷日志）。"""
        import logging

        from core.character.session_binding import SnapshotUnavailableCause

        agent = _FakeAgent()
        await _create_session(agent, "conv_legacy_probe", "legacy 探针")
        app = _make_app(agent)
        app["character_session_binding_service"].snapshot_with_cause = AsyncMock(
            return_value=(None, SnapshotUnavailableCause.LEGACY_ABSENT))
        client = await aiohttp_client(app)
        with caplog.at_level(logging.WARNING):
            resp = await client.get("/api/sessions")
        assert resp.status == 200
        assert not [r for r in caplog.records if "会话角色数据缺口" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_wire_shape_unchanged_for_both_causes(self, aiohttp_client):
        """契约零变更：两种成因下响应都仍**省略** character 键（前端行为不变）。"""
        from core.character.session_binding import SnapshotUnavailableCause

        for cause in (SnapshotUnavailableCause.LEGACY_ABSENT,
                      SnapshotUnavailableCause.INCOMPLETE_BINDING):
            agent = _FakeAgent()
            await _create_session(agent, "conv_wire_probe", "wire 探针")
            app = _make_app(agent)
            app["character_session_binding_service"].snapshot_with_cause = AsyncMock(
                return_value=(None, cause))
            client = await aiohttp_client(app)
            resp = await client.get("/api/sessions")
            assert resp.status == 200
            row = next(s for s in (await resp.json())["sessions"]
                       if s["conversationId"] == "conv_wire_probe")
            assert "character" not in row, f"{cause} 不得改变 wire 形状"
