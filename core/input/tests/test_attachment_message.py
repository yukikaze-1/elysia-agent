"""批次 7 Task 5：附件/sticker 消息关联测试（marker=service）。

构造：真实 Agent（_make_agent_with 注入内存 fake 存储 + 真实/模拟 process_message）
+ 内存 fake AttachmentRepository（经 agent._attachment_repo 注入，noqa: SLF001）
+ aiohttp test client（pytest-aiohttp，仿 test_v2_ws / test_upload.py）。

覆盖：
- v2 text 带 attachments → accepted + process_message content 为 dict 列表 + refs+1
- 附件 404 → rejected ATTACHMENT_NOT_FOUND + 不落幂等 entry
- stickerId 不在注册表 → rejected CONTRACT_VALIDATION
- 空文本 sticker-only → 进门触发生成（content=[StickerPart dict]）
- 队列满（mock process_message=False）→ rejected QUEUE_FULL + 幂等 entry 删除 + refs 不递增
- 同 clientRequestId 不同附件 → 摘要不同 → 非幂等重放（§4.2 异摘要 → rejected）
- 删除消息 → refs 递减 + ref_count 归零 → mark_deleted + resolver unregister
- _resolve_image_source：/api/attachments/{id} → data URI（resolver 注册后）
- 资源索引：sticker 不再恒空；软删附件被过滤
"""

import asyncio
import json

import pytest
from aiohttp import WSMsgType

from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
from core.input.idempotency import get_store, reset_store
from core.schema.adapters import attachment_resolver as _ar
from core.storage.attachment_repo import StorageUnavailableError

pytestmark = pytest.mark.service

# 魔数 PNG 头（真实图片）
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


# ── 内存 fake AttachmentRepository（fail-closed 语义如实）──────────────────────


class _FakeAttachmentRepo:
    """内存 fake AttachmentRepository——消息关联测试用最小接口 + fail-closed。"""

    def __init__(self, offline: bool = False) -> None:
        self.offline = offline
        self.records: dict[str, dict] = {}

    async def connect(self) -> bool:
        """agent 生命周期启动调用；内存 fake 视为已连接。"""
        return True

    async def close(self) -> None:
        """agent 生命周期停止调用；内存 fake 无连接可关。"""

    def _require(self) -> None:
        if self.offline:
            raise StorageUnavailableError("attachments 集合不可用：MongoDB 未连接（离线）")

    async def create(self, doc: dict) -> None:
        self._require()
        self.records[doc["attachment_id"]] = dict(doc)

    async def get(self, attachment_id: str) -> dict | None:
        self._require()
        doc = self.records.get(attachment_id)
        return dict(doc) if doc is not None else None

    async def add_ref(self, attachment_id: str, message_id: str) -> None:
        self._require()
        doc = self.records.get(attachment_id)
        if doc is None:
            return
        refs = list(doc.get("refs") or [])
        if message_id not in refs:
            refs.append(message_id)
        doc["refs"] = refs
        doc["ref_count"] = len(refs)

    async def remove_ref(self, attachment_id: str, message_id: str) -> None:
        self._require()
        doc = self.records.get(attachment_id)
        if doc is None:
            return
        refs = [r for r in (doc.get("refs") or []) if r != message_id]
        doc["refs"] = refs
        doc["ref_count"] = len(refs)
        if not refs:
            doc["status"] = "deleted"
            doc["deleted_at"] = "now"

    async def mark_deleted(self, attachment_id: str) -> None:
        self._require()
        doc = self.records.get(attachment_id)
        if doc is not None:
            doc["status"] = "deleted"
            doc["deleted_at"] = "now"


def _add_attachment(repo, aid: str = "att_x", kind: str = "image",
                    mime: str = "image/png") -> None:
    """向 fake repo 预置一条 active 附件记录。"""
    repo.records[aid] = {
        "attachment_id": aid, "kind": kind, "mime": mime,
        "size": 1234, "filename": f"{aid}.png", "stored_name": f"{aid}.png",
        "ref_count": 0, "refs": [], "status": "active",
    }


@pytest.fixture(autouse=True)
def _clean_registries():
    """清空幂等表 + AttachmentFileResolver 模块级注册表（跨测试隔离）。"""
    reset_store()
    _ar._registry.clear()  # noqa: SLF001 — 测试清空模块级缓存
    yield
    _ar._registry.clear()  # noqa: SLF001
    reset_store()


# ── 建 app 工厂 ──────────────────────────────────────────────────────────────


def _make_agent_app(*, repo: _FakeAttachmentRepo | None = None):
    """构造真实 Agent（fake 存储）+ 注入 fake attachment repo + create_app。"""
    from core.input.server import create_app

    agent = _make_agent_with(_UsageLLM())
    agent._attachment_repo = repo or _FakeAttachmentRepo()  # noqa: SLF001
    app = create_app(agent, serve_static=False)
    return agent, app


async def _drain(client, headers):
    """建连并排空建连帧，返回 ws。"""
    ws = await client.session.ws_connect(client.make_url("/ws"), headers=headers)
    for _ in range(5):
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            if m.type == WSMsgType.TEXT:
                t = json.loads(m.data).get("type")
                if t in ("capabilities", "server_info"):
                    break
        except asyncio.TimeoutError:
            break
    return ws


async def _read_ack(ws, timeout: float = 5.0) -> dict:
    """读下一条 user_message.ack 帧。"""
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=timeout)
        if m.type == WSMsgType.TEXT:
            d = json.loads(m.data)
            if d.get("type") == "user_message.ack":
                return d


def _record_calls(agent, calls: list[dict]) -> None:
    """实例级替换 process_message：记录 kwargs 并返回 True（无真实生成）。"""
    async def _record(**kwargs) -> bool:
        calls.append(kwargs)
        return True
    agent.process_message = _record


# ── 测试 ─────────────────────────────────────────────────────────────────────


class TestAttachmentMessage:
    @pytest.mark.asyncio
    async def test_v2_text_with_attachments_processed(self, aiohttp_client):
        """v2 text 带 attachments → accepted + process_message 收到 dict 列表 + refs+1。"""
        agent, app = _make_agent_app()
        _add_attachment(agent._attachment_repo, "att_x")  # noqa: SLF001
        client = await aiohttp_client(app)
        await agent.start()
        try:
            calls: list[dict] = []
            _record_calls(agent, calls)
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "hi",
                "clientRequestId": "req_1",
                "attachments": [{"attachmentId": "att_x", "kind": "image", "mime": "image/png"}],
            })
            ack = await _read_ack(ws)
            assert ack["status"] == "accepted"
            assert ack["messageId"].startswith("msg_")
            # process_message 被调且 content 为 dict 列表（[{"type":"text",...}] 形式）
            assert len(calls) == 1
            content = calls[0]["content"]
            assert isinstance(content, list) and content, "content 应为非空 dict 列表"
            assert content[0]["type"] == "text" and content[0]["content"] == "hi"
            assert any(p["type"] == "image" and p["attachment_id"] == "att_x"
                       for p in content)
            assert calls[0]["text"] == "hi"
            assert calls[0]["message_id"] == ack["messageId"]
            assert calls[0]["protocol_version"] == 2
            # refs 引用计数 +1
            doc = agent._attachment_repo.records["att_x"]  # noqa: SLF001
            assert doc["ref_count"] == 1
            assert ack["messageId"] in doc["refs"]
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_attachment_not_found_rejected_no_entry(self, aiohttp_client):
        """附件不存在 → rejected ATTACHMENT_NOT_FOUND + 不落幂等 entry（重试不重发 accepted）。"""
        agent, app = _make_agent_app()  # 无 att_missing
        client = await aiohttp_client(app)
        await agent.start()
        try:
            calls: list[dict] = []
            _record_calls(agent, calls)
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            payload = {
                "type": "text", "content": "hi",
                "clientRequestId": "req_missing",
                "attachments": [{"attachmentId": "att_missing", "kind": "image", "mime": "image/png"}],
            }
            await ws.send_json(payload)
            ack = await _read_ack(ws)
            assert ack["status"] == "rejected"
            assert ack["error"]["code"] == "ATTACHMENT_NOT_FOUND"
            # 不落幂等 entry：重试同 ID 同内容 → 仍 rejected（非幂等重放）
            assert get_store().lookup("req_missing") is None
            await ws.send_json(payload)
            ack2 = await _read_ack(ws)
            assert ack2["status"] == "rejected"
            assert ack2["error"]["code"] == "ATTACHMENT_NOT_FOUND"
            # process_message 从未被调
            assert calls == []
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_sticker_not_in_registry_rejected(self, aiohttp_client):
        """stickerId 不在注册表 → rejected CONTRACT_VALIDATION。"""
        agent, app = _make_agent_app()
        client = await aiohttp_client(app)
        await agent.start()
        try:
            calls: list[dict] = []
            _record_calls(agent, calls)
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "hi",
                "clientRequestId": "req_st_unknown",
                "stickerId": "st_unknown_xyz",
            })
            ack = await _read_ack(ws)
            assert ack["status"] == "rejected"
            assert ack["error"]["code"] == "CONTRACT_VALIDATION"
            assert calls == []
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_sticker_only_empty_text_triggers_generation(self, aiohttp_client):
        """空文本 sticker-only → 进门（入口门放宽）→ 触发生成（content=[StickerPart dict]）。"""
        agent, app = _make_agent_app()
        client = await aiohttp_client(app)
        await agent.start()
        try:
            calls: list[dict] = []
            _record_calls(agent, calls)
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "",
                "stickerId": "st_happy",
                "clientRequestId": "req_st2",
            })
            ack = await _read_ack(ws)
            assert ack["status"] == "accepted"
            assert len(calls) == 1
            assert calls[0]["text"] == ""
            content = calls[0]["content"]
            assert isinstance(content, list)
            assert any(p["type"] == "sticker" and p["sticker_id"] == "st_happy"
                       and p["src"] == "/static/stickers/st_happy.png"
                       for p in content)
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_queue_full_rejected_no_refs_entry_deleted(self, aiohttp_client):
        """队列满（mock process_message=False）→ rejected QUEUE_FULL + 幂等 entry 删除 + refs 不递增。"""
        agent, app = _make_agent_app()
        _add_attachment(agent._attachment_repo, "att_x")  # noqa: SLF001
        client = await aiohttp_client(app)
        await agent.start()
        try:
            async def _drop(**kwargs) -> bool:
                return False
            agent.process_message = _drop
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "hi",
                "clientRequestId": "req_qfull",
                "attachments": [{"attachmentId": "att_x", "kind": "image", "mime": "image/png"}],
            })
            ack = await _read_ack(ws)
            assert ack["status"] == "rejected"
            assert ack["error"]["code"] == "QUEUE_FULL"
            assert ack["error"].get("retryable") is True
            # refs 不递增
            doc = agent._attachment_repo.records["att_x"]  # noqa: SLF001
            assert doc["ref_count"] == 0 and not doc["refs"]
            # 幂等 entry 已删：重试重新进入（不重发 accepted）
            assert get_store().lookup("req_qfull") is None
            await ws.send_json({
                "type": "text", "content": "hi",
                "clientRequestId": "req_qfull",
                "attachments": [{"attachmentId": "att_x", "kind": "image", "mime": "image/png"}],
            })
            ack2 = await _read_ack(ws)
            assert ack2["status"] == "rejected"
            assert ack2["error"]["code"] == "QUEUE_FULL"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_same_request_id_different_attachments_not_replayed(self, aiohttp_client):
        """同 clientRequestId 但附件不同 → 摘要不同 → 非幂等重放（§4.2 异摘要 → rejected）。

        核心：附件已进入幂等摘要——同文本不同附件 digest 不同，第二次不被当作同内容
        重放（同 ID 不同内容摘要 → CONTRACT_VALIDATION 拒绝，不重复生成）。
        """
        agent, app = _make_agent_app()
        _add_attachment(agent._attachment_repo, "att_x")  # noqa: SLF001
        _add_attachment(agent._attachment_repo, "att_y", kind="file",
                        mime="application/octet-stream")
        client = await aiohttp_client(app)
        await agent.start()
        try:
            calls: list[dict] = []
            _record_calls(agent, calls)
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "hi",
                "clientRequestId": "req_diff",
                "attachments": [{"attachmentId": "att_x", "kind": "image", "mime": "image/png"}],
            })
            ack1 = await _read_ack(ws)
            assert ack1["status"] == "accepted"
            # 同 ID 同文本但附件不同 → 摘要不同 → 拒绝（非重放、不重复生成）
            await ws.send_json({
                "type": "text", "content": "hi",
                "clientRequestId": "req_diff",
                "attachments": [{"attachmentId": "att_y", "kind": "file", "mime": "application/octet-stream"}],
            })
            ack2 = await _read_ack(ws)
            assert ack2["status"] == "rejected"
            assert ack2["error"]["code"] == "CONTRACT_VALIDATION"
            # 第二次在 process 前被拒 → 只处理过一次
            assert len(calls) == 1
            # 附件 refs：att_x 关联了第一条消息
            assert ack1["messageId"] in agent._attachment_repo.records["att_x"]["refs"]  # noqa: SLF001
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_attachment_storage_offline_rejected(self, aiohttp_client):
        """Mongo 离线（fake 抛 StorageUnavailableError）→ rejected STORAGE_UNAVAILABLE。"""
        agent, app = _make_agent_app(repo=_FakeAttachmentRepo(offline=True))
        client = await aiohttp_client(app)
        await agent.start()
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "hi",
                "clientRequestId": "req_offline",
                "attachments": [{"attachmentId": "att_x", "kind": "image", "mime": "image/png"}],
            })
            ack = await _read_ack(ws)
            assert ack["status"] == "rejected"
            assert ack["error"]["code"] == "STORAGE_UNAVAILABLE"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_delete_message_releases_attachment_refs(self, aiohttp_client):
        """删除消息 → 附件 refs 递减；ref_count 归零 → mark_deleted + resolver unregister。"""
        from core.schema.message import UserMessage
        from core.schema.part import ImagePart, TextPart

        agent, app = _make_agent_app()
        repo = agent._attachment_repo  # noqa: SLF001
        _add_attachment(repo, "att_x")
        _ar.register("att_x", "image", "att_x.png")
        client = await aiohttp_client(app)
        await agent.start()
        try:
            # 归档一条含附件 part 的消息
            msg = UserMessage(
                content=[TextPart(content="带图"),
                         ImagePart(src="/api/attachments/att_x", attachment_id="att_x")],
                message_id="msg_del_1", session_id="default",
            )
            await agent.message_repo.append("default", msg)
            msgs, _ = await agent.message_repo.query_offset(limit=10)
            target = next(m for m in msgs if str(m.message_id) == "msg_del_1")
            mongo_id = target.metadata["_mongo_id"]
            # 模拟已有引用：msg_del_1 + m2
            repo.records["att_x"]["refs"] = ["msg_del_1", "m2"]
            repo.records["att_x"]["ref_count"] = 2

            resp = await client.delete(f"/debug/api/archive/{mongo_id}")
            assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
            doc = repo.records["att_x"]
            assert doc["ref_count"] == 1
            assert doc["refs"] == ["m2"]
            # resolver 仍注册（还有 m2 引用）
            assert _ar.resolve("att_x") is not None

            # 再删掉 m2 的引用消息 → ref_count 归零 → mark_deleted + unregister
            msg2 = UserMessage(
                content=[TextPart(content="另一条"),
                         ImagePart(src="/api/attachments/att_x", attachment_id="att_x")],
                message_id="m2", session_id="default",
            )
            await agent.message_repo.append("default", msg2)
            msgs, _ = await agent.message_repo.query_offset(limit=10)
            target2 = next(m for m in msgs if str(m.message_id) == "m2")
            resp = await client.delete(f"/debug/api/archive/{target2.metadata['_mongo_id']}")
            assert resp.status == 200
            assert repo.records["att_x"]["ref_count"] == 0
            assert repo.records["att_x"]["status"] == "deleted"
            assert _ar.resolve("att_x") is None  # resolver 已 unregister
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_rest_delete_session_releases_attachments(self, aiohttp_client):
        """REST 删会话 → 先释放归档消息附件 refs 再删会话（remove_ref 被调，BE-030+BE-070）。

        批次 8a（D5）：sessions_delete_handler 在 session_manager.delete 前调
        release_session_attachments——归档含 attachment_id="att_x" 消息的会话删除时
        附件 remove_ref 必须被触发（否则附件记录 refs 泄漏成孤儿）。
        """
        from core.schema.message import UserMessage
        from core.schema.part import ImagePart, TextPart

        agent, app = _make_agent_app()
        repo = agent._attachment_repo  # noqa: SLF001
        _add_attachment(repo, "att_x")
        client = await aiohttp_client(app)
        await agent.start()
        try:
            # 会话 s1 存在（session_manager 内存缓存池构造）——REST 删除存在性检查前置
            agent.session_manager.get("s1")
            # 播种 s1 的一条含附件归档消息（attachment_id="att_x"）
            await agent.message_repo.append("s1", UserMessage(
                content=[TextPart(content="图片"),
                         ImagePart(src="/api/attachments/att_x", attachment_id="att_x")],
                message_id="msg_s1", session_id="s1",
            ))
            # 模拟已有引用
            repo.records["att_x"]["refs"] = ["msg_s1"]
            repo.records["att_x"]["ref_count"] = 1

            # spy remove_ref：断言 REST 删除路径触发了附件引用释放
            removed = []
            original = agent.attachment_repo.remove_ref

            async def spy(aid, mid):
                removed.append((aid, mid))
                await original(aid, mid)

            agent.attachment_repo.remove_ref = spy

            resp = await client.delete("/api/sessions/s1")
            assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
            assert any(aid == "att_x" for aid, _ in removed)
        finally:
            await agent.stop()


class TestResolveImageSource:
    @pytest.mark.asyncio
    async def test_attachment_src_resolves_to_data_uri(self, tmp_path, monkeypatch):
        """_resolve_image_source('/api/attachments/att_x') → data URI（resolver 注册后）。"""
        from core.schema.adapters.message_adapter import _resolve_image_source

        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS", str(tmp_path / "uploads"))
        img_dir = tmp_path / "uploads" / "image"
        img_dir.mkdir(parents=True)
        (img_dir / "att_x.png").write_bytes(PNG)
        _ar.register("att_x", "image", "att_x.png")
        try:
            uri = _resolve_image_source("/api/attachments/att_x")
            assert uri.startswith("data:image/png;base64,")
            # 未注册的 attachment → 原样返回
            assert _resolve_image_source("/api/attachments/att_missing") == \
                "/api/attachments/att_missing"
        finally:
            _ar.unregister("att_x")


class TestResourceIndex:
    @pytest.mark.asyncio
    async def test_sticker_resource_not_empty_and_soft_deleted_filtered(self, aiohttp_client):
        """资源索引：含 StickerPart 消息 → sticker 类别返回条目；软删附件被过滤。"""
        from core.schema.message import UserMessage
        from core.schema.part import ImagePart, StickerPart, TextPart

        agent, app = _make_agent_app()
        repo = agent._attachment_repo  # noqa: SLF001
        _add_attachment(repo, "att_x")
        client = await aiohttp_client(app)
        await agent.start()
        try:
            # sticker 消息（内置资源）
            await agent.message_repo.append("default", UserMessage(
                content=[TextPart(content="表情"), StickerPart(sticker_id="st_happy",
                                                              src="/static/stickers/st_happy.png")],
                message_id="msg_st1", session_id="default",
            ))
            # 附件消息（active）
            await agent.message_repo.append("default", UserMessage(
                content=[TextPart(content="图片"),
                         ImagePart(src="/api/attachments/att_x", attachment_id="att_x")],
                message_id="msg_img1", session_id="default",
            ))
            # sticker 类别：不再恒空
            resp = await client.get("/api/sessions/default/resources?category=sticker")
            assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
            data = await resp.json()
            sticker_urls = [it["url"] for it in data["items"]]
            assert "/static/stickers/st_happy.png" in sticker_urls
            # image 类别：att_x active → 返回
            resp = await client.get("/api/sessions/default/resources?category=image")
            data = await resp.json()
            assert any(it["url"] == "/api/attachments/att_x" for it in data["items"])
            # 软删附件 → image 类别过滤
            repo.records["att_x"]["status"] = "deleted"
            resp = await client.get("/api/sessions/default/resources?category=image")
            data = await resp.json()
            assert all(it["url"] != "/api/attachments/att_x" for it in data["items"])
        finally:
            await agent.stop()
