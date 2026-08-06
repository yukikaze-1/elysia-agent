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

import pytest

from core.cognitive.generation_registry import GenerationRegistry
from core.memory.session_manager import SessionManager
from core.schema.message import MessageId, UserMessage, AssistantMessage
from core.schema.part import TextPart, ImagePart, VideoPart, FilePart, AudioPart
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
    """REST 路由消费的假 agent：真实 SessionManager + 内存 repo + 可控 pending/approvals。"""

    def __init__(self) -> None:
        self._backend = FakeDocumentDBBackend()
        self.session_repo = SessionRepository(db=self._backend)
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

    async def switch_session(self, sid) -> None:
        await self.session_manager.switch(sid)


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
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        data = await resp.json()
        assert data["sessions"] == []
        assert data["has_more"] is False
        assert "cursor" not in data  # 无更多时不带 cursor

    @pytest.mark.asyncio
    async def test_create_returns_summary(self, aiohttp_client):
        app = _make_app(_FakeAgent())
        client = await aiohttp_client(app)
        resp = await client.post("/api/sessions", json={"title": "我的会话"})
        assert resp.status == 200
        data = await resp.json()
        assert data["conversationId"].startswith("conv_")
        assert data["title"] == "我的会话"
        assert data["messageCount"] == 0
        assert data["active"] is False
        assert "updatedAt" in data

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
        # 下一页与第一页不重叠
        first_ids = {s["conversationId"] for s in data["sessions"]}
        second_ids = {s["conversationId"] for s in data2["sessions"]}
        assert len(second_ids) == 1
        assert not (first_ids & second_ids)

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
        assert len(by_id) == 5
        for i in range(5):
            assert by_id[f"conv_test_g{i}"]["messageCount"] == i + 1, \
                f"conv_test_g{i} messageCount 应等于归档计数 {i + 1}"

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

        # 锚点 msg_2 之后的消息正序返回
        resp = await client.get("/api/sessions/conv_test_inc/messages", params={"after": "msg_conv_test_inc_2"})
        assert resp.status == 200
        data = await resp.json()
        assert [m["message_id"] for m in data["messages"]] == ["msg_conv_test_inc_3", "msg_conv_test_inc_4"]
        assert data["total"] == 5

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
            text=text, user_name="u", request_id=f"req_{message_id}",
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
