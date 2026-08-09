"""批次 3 Task 11：WS activate_session + conversationId 路由 + 建连 v1/v2 resolve（marker=service）。

覆盖 spec §6：
- activate_session 切 active + 重发新会话回放（fake 会话预置消息）
- 未知 conversationId → ErrorMessage{code: SESSION_NOT_FOUND}（不切 active）
- v2 text 带 conversationId → 落该会话（ACK/generation 事件 conversationId = 目标会话）
- v2 text 无 conversationId → 落当前活动会话（ACK conversationId = active）
- v1 连接发 text → 恒 default（active 切走后 v1 回放/写入仍 default）

生产数据保护：全部用隔离会话（conv_test_*），不删/不清 default。
"""

import asyncio
import json
import time

import pytest
from aiohttp import WSMsgType

from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
from core.input.idempotency import get_store, reset_store
from core.schema.message import MessageId, UserMessage
from core.schema.part import TextPart


@pytest.fixture
def fresh_idempotency():
    """每个测试独立幂等表。"""
    reset_store()
    yield


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


async def _drain_with_session(client, headers):
    """建连，返回 (ws, 首个 session 回放帧)，同时排空其余建连帧。"""
    ws = await client.session.ws_connect(client.make_url("/ws"), headers=headers)
    session = None
    for _ in range(6):
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            if m.type == WSMsgType.TEXT:
                d = json.loads(m.data)
                t = d.get("type")
                if t == "session" and session is None:
                    session = d
                elif t in ("capabilities", "server_info"):
                    break
        except asyncio.TimeoutError:
            break
    return ws, session


def _seed_session(agent, sid: str, text: str) -> None:
    """预置隔离会话（内存构造 + 标记 loaded，避免 Mongo/JSONL 加载）。"""
    session = agent.session_manager.get(sid)
    agent.session_manager._loaded.add(sid)  # noqa: SLF001 — 测试确定性
    session.add_message(UserMessage(
        content=[TextPart(content=text)],
        message_id=MessageId(f"msg_seed_{sid}"),
    ))


def _message_ids(agent, sid: str) -> set:
    """会话最近消息 ID 集合（不触发任何加载/写入）。"""
    return {str(m.message_id) for m in agent.session_manager.get(sid).recent_messages()}


def _message_text(m) -> str:
    """提取单条 Pydantic Message 的纯文本。"""
    return "".join(p.content for p in m.content if isinstance(p, TextPart))


def _first_text(item: dict) -> str:
    """从回放 item 提取纯文本（parts 形状）。"""
    parts = item.get("parts") or []
    return "".join(p.get("content", "") for p in parts)


async def _receive_type(ws, wanted: str, max_iters: int = 30, timeout: float = 10) -> dict:
    """循环收帧直到出现 wanted 类型，返回该帧。"""
    for _ in range(max_iters):
        m = await asyncio.wait_for(ws.receive(), timeout=timeout)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == wanted:
            return d
    raise AssertionError(f"未收到 {wanted} 帧")


@pytest.mark.service
class TestActivateSession:
    @pytest.mark.asyncio
    async def test_activate_session_replays_new_active(self, aiohttp_client, fresh_idempotency):
        """activate_session 切 active + 重发新会话 SessionMessage 回放。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            _seed_session(agent, "conv_test_a", "会话A的预置消息")
            assert agent.active_session_id == "default"

            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "activate_session", "conversationId": "conv_test_a"})

            replay = await _receive_type(ws, "session")
            assert replay.get("conversationId") == "conv_test_a", "回放应携带被切会话 ID"
            texts = [_first_text(item) for item in replay.get("messages", [])]
            assert "会话A的预置消息" in texts, "activate_session 应重发目标会话回放"
            assert agent.active_session_id == "conv_test_a"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_activate_session_unknown_404(self, aiohttp_client, fresh_idempotency):
        """未知 conversationId → ErrorMessage{code: SESSION_NOT_FOUND}，不切 active。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "activate_session", "conversationId": "conv_does_not_exist"})

            err = await _receive_type(ws, "error")
            assert err.get("code") == "SESSION_NOT_FOUND"
            assert agent.active_session_id == "default", "404 不应切 active"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_activate_session_busy_double_insurance_AGENT_BUSY(self, aiohttp_client, fresh_idempotency):
        """批次 C：switch() 双保险 busy check 抛 SessionBusyError → 回复 AGENT_BUSY（非 INTERNAL）。

        显式门禁（is_accepting_input=True）通过，但 switch 内 double-insurance busy
        check 命中——此前被通用 except Exception 吞成 INTERNAL。
        """
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            _seed_session(agent, "conv_test_double", "会话")
            # 显式门禁通过（FSM IDLE → is_accepting_input True），仅 switch 内双保险命中
            assert agent.status.is_accepting_input is True
            agent.session_manager.set_busy_check(lambda: True)
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "activate_session", "conversationId": "conv_test_double"})

            err = await _receive_type(ws, "error")
            assert err.get("code") == "AGENT_BUSY", f"实际 {err.get('code')}"
            assert agent.active_session_id == "default", "忙时不应切 active"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_activate_session_general_failure_uses_INTERNAL(self, aiohttp_client, fresh_idempotency):
        """review 修复（兼容#4）：activate_session 通用失败 → ErrorMessage{code: INTERNAL}。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            _seed_session(agent, "conv_test_fail", "会话")

            async def _boom(sid):
                raise RuntimeError("boom")

            agent.switch_session = _boom
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "activate_session", "conversationId": "conv_test_fail"})

            err = await _receive_type(ws, "error")
            assert err.get("code") == "INTERNAL", f"实际 {err.get('code')}"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_activate_session_busy_rejected(self, aiohttp_client, fresh_idempotency):
        """F1(a): 生成/TTS 中（FSM 非 IDLE，is_accepting_input=False）→ activate_session 拒绝
        AGENT_BUSY，不切 active（防 engine 换绑 vs 在途生成竞态）。"""
        from core.input.server import create_app
        from core.schema import AgentState

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            _seed_session(agent, "conv_test_busy", "忙时目标会话")
            # 模拟生成/TTS 中：FSM 推进到 THINKING（IDLE → THINKING 合法）
            agent._fsm.transition(AgentState.THINKING)
            assert agent.status.is_accepting_input is False

            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "activate_session", "conversationId": "conv_test_busy"})

            err = await _receive_type(ws, "error")
            assert err.get("code") == "AGENT_BUSY", f"实际 {err.get('code')}"
            assert agent.active_session_id == "default", "忙时不应切 active"
            # 恢复 IDLE（THINKING → INTERRUPTED → IDLE，避免测试残留非 IDLE 状态）
            agent._fsm.interrupt()
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestConversationIdRouting:
    @pytest.mark.asyncio
    async def test_v2_text_with_conversation_id_routes_to_session(self, aiohttp_client, fresh_idempotency):
        """v2 text 带 conversationId → 落该会话（ACK + generation 事件 conversationId = 目标会话）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            _seed_session(agent, "conv_test_b", "已存在会话B")
            default_before = _message_ids(agent, "default")
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text",
                "content": "这条要落到 conv_test_b",
                "clientRequestId": f"req_c_{time.monotonic_ns()}",
                "conversationId": "conv_test_b",
            })

            ack = await _receive_type(ws, "user_message.ack")
            assert ack["status"] == "accepted"
            assert ack.get("conversationId") == "conv_test_b", "ACK conversationId = 实际目标会话"

            # generation 事件 conversationId 非空且等于目标会话
            gen_conv_ids = set()
            for _ in range(40):
                m = await asyncio.wait_for(ws.receive(), timeout=10)
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d.get("type") == "generation.started":
                    gen_conv_ids.add(d.get("conversationId"))
                elif d.get("type") == "generation.completed":
                    if d.get("conversationId"):
                        gen_conv_ids.add(d.get("conversationId"))
                    break
            assert gen_conv_ids, "generation 事件应带 conversationId"
            assert all(cid == "conv_test_b" for cid in gen_conv_ids), f"实际 {gen_conv_ids}"

            # 预分配用户消息 ID 落 conv_test_b，default 未新增该消息（按 ID 比对，规避 JSONL 回放污染）
            assert ack["messageId"] in _message_ids(agent, "conv_test_b"), \
                "预分配用户消息 ID 应落在目标会话"
            assert ack["messageId"] not in _message_ids(agent, "default") - default_before, \
                "default 不应新增路由到其他会话的消息"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_text_default_routes_to_active(self, aiohttp_client, fresh_idempotency):
        """v2 text 无 conversationId → 落当前活动会话（ACK conversationId = active）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await agent.switch_session("conv_test_d")
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text",
                "content": "v2 无 conversationId 应落 active",
                "clientRequestId": f"req_d_{time.monotonic_ns()}",
            })

            ack = await _receive_type(ws, "user_message.ack")
            assert ack["status"] == "accepted"
            assert ack.get("conversationId") == "conv_test_d", "v2 缺省 → 当前活动会话"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_text_unknown_conversation_rejected(self, aiohttp_client, fresh_idempotency):
        """9a3：v2 text 显式未知 conversationId → ack rejected SESSION_NOT_FOUND。

        禁止懒创建幽灵会话：未知显式 ID 拒绝——不生成 messageId、不落库、不落幂等 entry。
        """
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            default_before = _message_ids(agent, "default")
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text",
                "content": "hi",
                "clientRequestId": "req_deadbeef",
                "conversationId": "conv_deadbeef",
            })

            ack = await _receive_type(ws, "user_message.ack")
            assert ack["status"] == "rejected"
            assert ack.get("error", {}).get("code") == "SESSION_NOT_FOUND", \
                f"实际 {ack.get('error')}"
            assert not ack.get("messageId"), "未知会话拒绝不应生成 messageId"
            # 不落库：default 无新增消息（未知会话也未构造）
            assert _message_ids(agent, "default") == default_before
            assert not await agent.session_manager.has_session("conv_deadbeef"), \
                "未知会话不应被懒创建"
            # 不落幂等 entry：重试同 ID 仍走拒绝路径（非 accepted 重放）
            assert get_store().lookup("req_deadbeef") is None
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestV1AlwaysDefault:
    @pytest.mark.asyncio
    async def test_v1_connect_replay_and_text_stay_default(self, aiohttp_client, fresh_idempotency):
        """v1 连接：建连回放 + text 写入恒 default（active 切到 conv_test_c 后不受影响）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            # v2 语义切 active 到隔离会话（模拟另一客户端已切会话）
            _seed_session(agent, "conv_test_c", "会话C的预置消息")
            await agent.switch_session("conv_test_c")
            assert agent.active_session_id == "conv_test_c"

            # v1 建连回放应为 default（resolve(None, 1)），不含 conv_test_c 消息
            ws, replay = await _drain_with_session(client, None)
            assert replay is not None, "v1 建连应收到 session 回放"
            texts = [_first_text(item) for item in replay.get("messages", [])]
            assert "会话C的预置消息" not in texts, "v1 回放应来自 default，不含 active 会话消息"

            # v1 text → 写入 default，conv_test_c 不受影响
            default_before = _message_ids(agent, "default")
            conv_c_before = _message_ids(agent, "conv_test_c")
            await ws.send_json({"type": "text", "content": "v1 应恒落 default"})
            for _ in range(40):
                m = await asyncio.wait_for(ws.receive(), timeout=10)
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d.get("type") == "state" and d.get("state") == "idle":
                    break
            new_default = [
                m for m in agent.session_manager.get("default").recent_messages()
                if str(m.message_id) not in default_before
            ]
            assert any("v1 应恒落 default" in _message_text(m) for m in new_default), \
                "v1 text 应写入 default"
            assert _message_ids(agent, "conv_test_c") == conv_c_before, \
                "v1 写入不应影响 conv_test_c"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v1_activate_session_ignored(self, aiohttp_client, fresh_idempotency):
        """review 修复（兼容#5）：v1 连接发 activate_session 不生效（active 不切）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            _seed_session(agent, "conv_test_act_v1", "会话V1")
            ws = await _drain(client, None)  # v1 握手
            await ws.send_json({"type": "activate_session", "conversationId": "conv_test_act_v1"})
            # v1 忽略：active 保持 default；短窗口内不出现 session 回放帧
            try:
                m = await asyncio.wait_for(ws.receive(), timeout=0.8)
                assert m.type != WSMsgType.TEXT or json.loads(m.data).get("type") != "session"
            except asyncio.TimeoutError:
                pass
            assert agent.active_session_id == "default", "v1 activate_session 不应切 active"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v1_connect_mood_from_default(self, aiohttp_client, fresh_idempotency):
        """review 修复（兼容#5）：v1 建连 expression mood 读 default 会话（active 切走不影响）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            _seed_session(agent, "conv_test_mood", "会话")
            agent.session_manager.get("default").character_state.mood = "平静"
            await agent.switch_session("conv_test_mood")
            agent.character_engine.state.mood = "兴奋"  # active 会话 mood ≠ default

            ws = await _drain(client, None)  # v1 握手
            mood = None
            for _ in range(6):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d.get("type") == "expression":
                    mood = d.get("expression")
                    break
            assert mood == "平静", f"v1 建连 mood 应读 default 会话 mood，实际 {mood!r}"
            await ws.close()
        finally:
            await agent.stop()
