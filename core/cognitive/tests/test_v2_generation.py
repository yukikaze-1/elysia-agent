"""批次 2 Task 6：v2 生成事件序列 + v1 wire 回归 + fault 注入（marker=service）。

覆盖 spec §10 测试 4/5/7：
- v2 连接：generation.started → inner_voice 段 → public_reply 段 → generation.completed
- v1 连接：wire 帧序列与现状一致（v1 回归防护）
- fault：v2 收 generation.failed 无 ❌ delta；v1 仍收 ❌ delta
"""

import asyncio
import json
import time

import pytest
from aiohttp import WSMsgType
from core.input.idempotency import reset_store


@pytest.fixture(autouse=True)
def _fresh_idempotency():
    reset_store()
    yield


# FakeLLM 带标签响应：inner_voice + mood + 多句（触发真实事件序列）
_TAGGED = (
    "<inner_voice>我觉得这个话题很有意思，想认真回应一下。</inner_voice>"
    "<mood>开心|0.8</mood>"
    "你好！这是第一句话。第二句话进一步补充。第三句话作为收尾。"
)


class _UsageLLM:
    """带 stream_think + usage 的 fake（镜像 scripts/capture_ws_trace.py）。"""

    def __init__(self, response: str = _TAGGED, fault: str | None = None):
        import re
        self.response = response
        self.fault = fault
        self._model = "fake/test"
        self._tok = re.compile(r"([一-鿿]|[\w]+|[^\w\s])")

    async def chat(self, messages, **kwargs):
        return self.response

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 0}

    async def close(self):
        pass

    async def stream_think(self, messages, **kwargs):
        if self.fault == "http_503":
            import httpx
            req = object()
            resp = type("R", (), {"status_code": 503})()
            raise httpx.HTTPStatusError("unavailable", request=req, response=resp)
        for tok in self._tok.findall(self.response):
            yield {"content": tok, "tool_calls": None}
        yield {
            "content": None, "tool_calls": None,
            "usage": {"input_tokens": 12, "output_tokens": 18, "total_tokens": 30,
                      "cached_tokens": 0, "reasoning_tokens": 0},
        }


def _inject_fake_storage(agent) -> "Agent":
    """把 agent 的全部 Mongo repo 换成内存 fake（生产数据保护，review 修复 I3）。

    这些测试原本连真 Mongo 写 default：start() 加载、stop() 的 save_all() 会把
    测试消息回写生产 default 会话。注入后 connect 全落内存 FakeDocumentDBBackend，
    任何测试都不覆盖/污染生产 default。generation registry 也换成同一 fake db 的
    实例（agent 与 loop 同一引用，生命周期 connect 即用）。
    """
    from core.cognitive.generation_registry import GenerationRegistry
    from core.storage.character_version_repo import CharacterVersionRepository
    from core.storage.desktop_call_repo import DesktopCallRepository
    from core.storage.generation_repo import GenerationRepository
    from core.storage.message_repo import MessageRepository
    from core.storage.session_repo import SessionRepository
    from core.storage.token_repo import TokenRepository
    from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

    backend = FakeDocumentDBBackend()
    session_repo = SessionRepository(db=backend)
    message_repo = MessageRepository(db=backend)
    token_repo = TokenRepository(db=backend)
    version_repo = CharacterVersionRepository(db=backend)
    agent._session_repo = session_repo
    agent._message_repo = message_repo
    agent._token_repo = token_repo
    agent._version_repo = version_repo
    agent.session_manager._repo = session_repo
    agent.session_manager._msg_repo = message_repo
    # eager default 与后续构造的 SessionMemory 持有旧 repo 引用——统一换到 fake
    for s in agent.session_manager._sessions.values():
        s._repo = session_repo
        s._msg_repo = message_repo
    reg = GenerationRegistry(repo=GenerationRepository(db=backend))
    agent._generation_registry = reg
    agent.cognitive._generation_registry = reg
    # 批次 4（spec §11 Fake 约定）：desktop 调用注册表也换同一 fake db——断言
    # `GET /api/tools/calls/{callId}` 需可注入；bridge._repo 同步换（构造时绑定原 repo）。
    desktop_repo = DesktopCallRepository(mongodb=backend)
    agent._desktop_call_repo = desktop_repo
    agent.desktop_bridge._repo = desktop_repo  # noqa: SLF001 — 测试内同步桥的 repo 引用
    return agent


def _make_agent_with(fake) -> "Agent":
    import os as _os
    _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
    from core.config import AppConfig
    from core.agent import Agent
    config = AppConfig()
    config.tts.enabled = False
    config.stt.enabled = False
    agent = Agent(config=config)
    agent.llm._backend = fake
    # 批次 3（Task 13 + review 修复 I3）：注入内存 fake 存储 + GenerationRegistry——
    # 取消/断线测试断言终态 mark（真 Mongo 离线自建 registry 为 no-op），且不碰生产 default。
    _inject_fake_storage(agent)
    return agent


async def _registry_docs(agent) -> list[dict]:
    """读取注入的 GenerationRegistry 全部文档（default 会话），供终态断言。

    与 test_session_routes / test_generation_wiring 同构：query_by_session
    返回 (docs, total, has_more) 三元组。
    """
    reg = agent.cognitive._generation_registry  # noqa: SLF001
    docs, _, _ = await reg.query_by_session("default")
    return docs


async def _send_and_collect(client, headers, content: str,
                            client_request_id: str | None = None):
    async with client.session.ws_connect(client.make_url("/ws"), headers=headers) as ws:
        # 排空建连帧（v1 收 capabilities；v2 收 server_info）
        for _ in range(5):
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                if msg.type == WSMsgType.TEXT:
                    t = json.loads(msg.data).get("type")
                    if t in ("capabilities", "server_info"):
                        break
            except asyncio.TimeoutError:
                break
        msg = {"type": "text", "content": content}
        if client_request_id:
            msg["clientRequestId"] = client_request_id
        await ws.send_json(msg)
        frames = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=30)  # 满载下放宽超时
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            frames.append(data)
            if data.get("type") == "state" and data.get("state") == "idle":
                break
        return frames


@pytest.mark.service
class TestV2GenerationSequence:
    @pytest.mark.asyncio
    async def test_v2_generation_sequence(self, aiohttp_client):
        """v2 连接：generation.started → inner_voice 段 → public_reply 段 → completed。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001 空会话
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            frames = await _send_and_collect(
                client, {"X-Protocol-Version": "2"}, "你好",
                client_request_id=f"req_{time.monotonic_ns()}")
            types = [f["type"] for f in frames]
            # 关键序列：generation.started 先于 segments；completed 是终态
            assert "generation.started" in types
            assert "generation.segment.started" in types
            assert "generation.segment.completed" in types
            assert "generation.completed" in types
            # 顺序：started → segment.started → ... → completed
            i_started = types.index("generation.started")
            i_completed = types.index("generation.completed")
            assert i_started < i_completed
            # inner_voice 段 + public_reply 段都存在
            seg_types = [f.get("channel") for f in frames if f["type"] == "generation.segment.started"]
            assert "inner_voice" in seg_types
            assert "public_reply" in seg_types
            # PRD BE-022：每句独立 segmentId——4 句回复应有 4 个 public_reply 段，ID 互异
            pub_segs = [f for f in frames
                        if f["type"] == "generation.segment.started"
                        and f.get("channel") == "public_reply"]
            assert len(pub_segs) == 4, f"每句应独立段，实际 {len(pub_segs)} 个 public_reply 段"
            assert len({s["segmentId"] for s in pub_segs}) == 4, "各段 segmentId 应互异"
            # v2 抑制 v1 token_usage/expression（spec §5.1：usage 由 completed 承载、mood 落位）
            assert not any(t == "token_usage" for t in types)
            # completed 携带 usage/model/mood
            completed = next(f for f in frames if f["type"] == "generation.completed")
            assert completed["finishReason"] == "stop"
            assert completed["usage"]["turn_total"] == 30
            assert completed["model"]  # 实际模型（fake 后端下为配置模型，非空即可）
            assert completed["mood"] == "开心"
            # 批次 3（Task 13）：generation.* 事件补 conversationId（非空）——
            # v2 缺省 conversationId → 活动会话（default），与 user_message.ack 解析一致
            gen_frames = [f for f in frames if f["type"].startswith("generation.")]
            assert gen_frames, "应收到 generation.* 事件"
            assert all(f.get("conversationId") for f in gen_frames), \
                "generation.* 六类事件应携带非空 conversationId"
            assert completed["conversationId"] == "default"
            # 终态 mark：成功路径 → registry 落 completed（BE-024）
            docs = await _registry_docs(agent)
            assert docs and docs[-1]["status"] == "completed"
            assert docs[-1]["finish_reason"] == "stop"
            assert docs[-1]["finished_at"]  # 终态带 finished_at
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v1_wire_sequence_unchanged(self, aiohttp_client):
        """v1 连接：wire 帧序列与现状一致（v1 回归防护——同 agent、v1 握手）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            frames = await _send_and_collect(client, None, "你好")
            types = [f["type"] for f in frames]
            # v1 现状序列：inner_voice command、expression（mood）、delta、message 句子边界、state idle
            assert "command" in types
            iv = next(f for f in frames if f["type"] == "command")
            assert iv["command"] == "inner_voice"
            assert "expression" in types  # <mood> 标签 → expression
            assert "delta" in types
            assert "message" in types     # 句子边界
            assert "token_usage" in types
            assert types[-1] == "state"   # 终态 idle
            # v1 不应收到任何 v2 generation 帧
            assert not any("generation" in t or t == "user_message.ack" for t in types)
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_fault_generation_failed_no_delta(self, aiohttp_client):
        """fault（503）：v2 收 generation.failed 且无 ❌ delta；v1 仍收 ❌ delta。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM(fault="http_503"))
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            # v2
            v2_frames = await _send_and_collect(
                client, {"X-Protocol-Version": "2"}, "hi",
                client_request_id=f"req_{time.monotonic_ns()}")
            v2_types = [f["type"] for f in v2_frames]
            assert "generation.failed" in v2_types
            failed = next(f for f in v2_frames if f["type"] == "generation.failed")
            assert failed["code"] in ("GENERATION_PROVIDER_UNAVAILABLE", "GENERATION_TIMEOUT")
            # 批次 3（Task 13）：generation.failed 携带非空 conversationId
            assert failed.get("conversationId"), "generation.failed 应携带 conversationId"
            # v2 无 ❌ delta
            v2_deltas = [f.get("content", "") for f in v2_frames if f["type"] == "delta"]
            assert not any("❌" in d for d in v2_deltas)
            # 终态 mark：错误路径 → registry 落 failed（终态唯一，_handle_interrupt 不覆盖）
            docs = await _registry_docs(agent)
            assert docs and docs[-1]["status"] == "failed"
            assert docs[-1]["finished_at"]
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v1_fault_still_delta_error(self, aiohttp_client):
        """fault（503）：v1 仍收 ❌ delta（兼容）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM(fault="http_503"))
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            v1_frames = await _send_and_collect(client, None, "hi")
            v1_deltas = [f.get("content", "") for f in v1_frames if f["type"] == "delta"]
            assert any("❌" in d for d in v1_deltas)
        finally:
            await agent.stop()


class _SlowLLM(_UsageLLM):
    """慢速 fake：token 间 sleep，供打断测试。"""

    def __init__(self, response: str = _TAGGED, token_delay: float = 0.1):
        import asyncio
        self._asyncio = asyncio
        super().__init__(response=response)
        self._token_delay = token_delay

    async def stream_think(self, messages, **kwargs):
        for tok in self._tok.findall(self.response):
            await self._asyncio.sleep(self._token_delay)
            yield {"content": tok, "tool_calls": None}
        yield {"content": None, "tool_calls": None, "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "cached_tokens": 0, "reasoning_tokens": 0}}


@pytest.mark.service
class TestV2Cancellation:
    @pytest.mark.asyncio
    async def test_v2_interrupt_emits_generation_cancelled(self, aiohttp_client, _fresh_idempotency):
        """v2 生成中 interrupt → generation.failed{CANCELLED}，无 ❌ delta。"""
        import time as _time
        from core.input.server import create_app

        agent = _make_agent_with(_SlowLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": "2"},
            ) as ws:
                for _ in range(5):
                    try:
                        m = await asyncio.wait_for(ws.receive(), timeout=5)
                        if m.type == WSMsgType.TEXT and json.loads(m.data).get("type") in ("capabilities", "server_info"):
                            break
                    except asyncio.TimeoutError:
                        break
                await ws.send_json({"type": "text", "content": "慢速生成", "clientRequestId": f"req_c_{_time.monotonic_ns()}"})
                await asyncio.sleep(0.6)  # 让生成开始
                await ws.send_json({"type": "interrupt"})
                got_failed = False
                for _ in range(20):
                    m = await asyncio.wait_for(ws.receive(), timeout=10)
                    if m.type != WSMsgType.TEXT:
                        continue
                    d = json.loads(m.data)
                    if d.get("type") == "generation.failed":
                        assert d["code"] == "GENERATION_CANCELLED"
                        got_failed = True
                        break
                assert got_failed, "未收到 generation.failed{CANCELLED}"
                # 批次 3（Task 13）：中断 → registry 落终态 cancelled
                # （ws interrupt source=websocket → 非 disconnect → cancelled）
                docs = await _registry_docs(agent)
                assert docs and docs[-1]["status"] == "cancelled"
                assert docs[-1]["finished_at"]  # 终态带 finished_at
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_error_path_single_terminal(self, aiohttp_client, _fresh_idempotency):
        """错误路径已发 failed → interrupt 不重复发 CANCELLED（终态唯一）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM(fault="http_503"))
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": "2"},
            ) as ws:
                for _ in range(5):
                    try:
                        m = await asyncio.wait_for(ws.receive(), timeout=5)
                        if m.type == WSMsgType.TEXT and json.loads(m.data).get("type") in ("capabilities", "server_info"):
                            break
                    except asyncio.TimeoutError:
                        break
                await ws.send_json({"type": "text", "content": "hi", "clientRequestId": "req_err_term"})
                failed_codes = []
                for _ in range(20):
                    m = await asyncio.wait_for(ws.receive(), timeout=10)
                    if m.type != WSMsgType.TEXT:
                        continue
                    d = json.loads(m.data)
                    if d.get("type") == "generation.failed":
                        failed_codes.append(d["code"])
                    if d.get("type") == "state" and d.get("state") == "idle":
                        break
                assert failed_codes, "未收到 generation.failed"
                # 终态唯一：只有一次 failed（错误路径的 code，不是 CANCELLED 双发）
                assert failed_codes[0] in ("GENERATION_PROVIDER_UNAVAILABLE", "GENERATION_TIMEOUT")
                assert len(failed_codes) == 1
                # 批次 3（Task 13）：错误路径终态唯一 → registry 只有一条 failed
                docs = await _registry_docs(agent)
                assert len(docs) == 1
                assert docs[0]["status"] == "failed"
        finally:
            await agent.stop()
