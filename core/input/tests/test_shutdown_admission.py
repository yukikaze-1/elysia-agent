# 测试文件：core/input/tests/test_shutdown_admission.py
"""停机窗口准入（CR-08）—— 非 RUNNING 必须拒收，且不得留下"已接受"的谎。

缺陷原状：准入判定读 FSM 投影（`status.is_accepting_input`），事件消费者读
`_state`。`stop()` 首行即置 STOPPED、消费者随即退出，但 FSM 仍显示 accepting，
于是整个停机窗口（20 步清理，数秒~数十秒）内新消息被投进**无界** bus 队列并
`return True`：调用方回 ok、v2 还把 accepted 写进幂等表，同 clientRequestId
重试永远重放这个 ACK，重启也补不回来。

覆盖：
1. `process_message` 在三个非 RUNNING 状态下一律返回 False（且不投 bus、不登记
   v2 accepted work）；
2. `/ready` 停机窗口 503、`/health` 仍 200 且暴露两个状态源的分歧；
3. v2 WS 真实链路：停机窗口收到 **rejected**（AGENT_BUSY），且**绝不重放 accepted**。
   S2-C1 之后，这条瞬时拒绝不落终态——同 key 立即重试拿到 `IN_PROGRESS`
   （"原请求可能仍在途"，可退避重试），而不是被逐字重放成永久失败。
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from aiohttp import WSMsgType

from core.agent import RunState
from core.storage.idempotency import reset_store

pytestmark = pytest.mark.service


NON_RUNNING = (RunState.STOPPING, RunState.STOPPED, RunState.RESTARTING)


# ═══════════════════════════════════════════════════════════════════
# 1. process_message 准入
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("state", NON_RUNNING)
async def test_process_message_rejected_when_not_running(state):
    """非 RUNNING → 返回 False，且**一个事件都不投**（消费者已退出）。"""
    from core.input.tests.test_server import _make_agent

    agent = _make_agent()
    await agent.start()
    try:
        agent._bus.post = MagicMock()
        agent._state = state
        assert await agent.process_message("hello") is False
        assert agent._bus.post.call_count == 0, "停机窗口不得投递 bus 事件"
    finally:
        agent._state = RunState.RUNNING
        await agent.stop()


@pytest.mark.asyncio
async def test_process_message_rejects_before_v2_work_reservation():
    """v2 拒收必须发生在 `reserve_work` **之前**——否则登记了没人释放。"""
    from unittest.mock import MagicMock

    from core.input.tests.test_server import _make_agent

    agent = _make_agent()
    await agent.start()
    try:
        agent._state = RunState.STOPPING
        agent._session_manager.reserve_work = MagicMock()
        assert await agent.process_message(
            "hello", protocol_version=2, conversation_id="default") is False
        assert agent._session_manager.reserve_work.call_count == 0
    finally:
        agent._state = RunState.RUNNING
        await agent.stop()


@pytest.mark.asyncio
async def test_process_message_accepted_when_running():
    """回归网：RUNNING 时行为不变（仍投 bus、仍返回 True）。"""
    from unittest.mock import MagicMock

    from core.input.tests.test_server import _make_agent

    agent = _make_agent()
    await agent.start()
    try:
        agent._bus.post = MagicMock()
        assert await agent.process_message("hello") is True
        assert agent._bus.post.call_count == 1
    finally:
        await agent.stop()


# ═══════════════════════════════════════════════════════════════════
# 2. 探针：/ready 摘除，/health 仍活且分歧可见
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ready_503_during_shutdown_window(aiohttp_client):
    """停机窗口 /ready 必须 503，否则负载均衡继续把流量导进正在丢消息的实例。"""
    from core.input.server import create_app
    from core.input.tests.test_health import _make_mock_agent

    agent = _make_mock_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    assert (await client.get("/ready")).status == 200

    agent._state = RunState.STOPPING
    agent.state = RunState.STOPPING
    resp = await client.get("/ready")
    assert resp.status == 503
    data = await resp.json()
    assert data["chatReady"] is False
    assert data["dependencies"]["lifecycle"] is False
    assert data["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_stays_200_and_exposes_state_divergence(aiohttp_client):
    """/health 恒 200（liveness 不因停机失败），且 run_state 与 fsm 的分歧可见。

    这正是缺陷的观测面：`fsm` 说 idle（accepting）、`run_state` 说 STOPPING。
    """
    from core.input.server import create_app
    from core.input.tests.test_health import _make_mock_agent

    agent = _make_mock_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    agent._state = RunState.STOPPING
    agent.state = RunState.STOPPING
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["run_state"] == "STOPPING"
    assert data["fsm"] == "idle", "两个状态源在停机窗口本就不同步——两个都要报"


@pytest.mark.asyncio
async def test_health_never_500s_on_non_runstate_state(aiohttp_client):
    """/health 的「恒 200」是硬不变量：state 不是 RunState 时降级而非 500。"""
    from unittest.mock import MagicMock

    from core.input.server import create_app
    from core.input.tests.test_health import _make_mock_agent

    agent = _make_mock_agent()
    agent.state = MagicMock()          # 非 RunState：无 .name 字符串
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.get("/health")
    assert resp.status == 200
    assert (await resp.json())["run_state"] == "unknown"


# ═══════════════════════════════════════════════════════════════════
# 3. REST /api/chat：503 而非 429（成因必须报对）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rest_chat_503_when_agent_shutting_down(aiohttp_client):
    """停机窗口回 503（服务不可用），不是 429（队列满）——后者是观测面说谎。"""
    from unittest.mock import AsyncMock, MagicMock

    from core.input.server import create_app

    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    agent._state = RunState.STOPPING
    agent.state = RunState.STOPPING
    agent.process_message = AsyncMock(return_value=False)
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat", json={"content": [{"type": "text", "text": "hi"}]})
    assert resp.status == 503

    agent._state = RunState.RUNNING
    agent.state = RunState.RUNNING
    resp = await client.post("/api/chat", json={"content": [{"type": "text", "text": "hi"}]})
    assert resp.status == 429, "队列满仍应是 429"


# ═══════════════════════════════════════════════════════════════════
# 4. v2 WS 真实链路：rejected，且重试不得重放 accepted
# ═══════════════════════════════════════════════════════════════════

async def _drain(client, headers):
    ws = await client.session.ws_connect(client.make_url("/ws"), headers=headers)
    for _ in range(5):
        try:
            m = await ws.receive()
            if m.type == WSMsgType.TEXT:
                t = json.loads(m.data).get("type")
                if t in ("capabilities", "server_info"):
                    break
        except Exception:
            break
    return ws


async def _send_and_read_ack(ws, content: str, client_request_id: str) -> dict:
    await ws.send_json({
        "type": "text", "content": content, "clientRequestId": client_request_id,
    })
    for _ in range(8):
        m = await asyncio.wait_for(ws.receive(), timeout=5)
        if m.type == WSMsgType.TEXT:
            d = json.loads(m.data)
            if d.get("type") == "user_message.ack":
                return d
    raise AssertionError("未收到 user_message.ack")


@pytest.mark.asyncio
async def test_v2_text_rejected_during_shutdown_and_never_replays_accepted(aiohttp_client):
    """停机窗口 → rejected(AGENT_BUSY)；同 key 重试**绝不重放 accepted**。

    S2-C1 之后这条拒绝（`retryable=True`）**不落终态**，故重试不再逐字重放它，
    而是回到「原请求可能仍在途」的 `IN_PROGRESS`——客户端按 `retryable` 退避后
    同键重试仍能真正被处理。CR-08 的原始诉求（"不得重放一个撒谎的 accepted"）
    由此更强地成立：没有 accepted 被写进去。
    """

    from core.cognitive.tests.test_v2_generation import _make_agent_with, _UsageLLM
    from core.input.server import create_app

    reset_store()
    agent = _make_agent_with(_UsageLLM())
    await agent.start()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    ws = None
    try:
        ws = await _drain(client, {"X-Protocol-Version": "2"})

        agent._state = RunState.STOPPING
        ack = await _send_and_read_ack(ws, "停机窗口的消息", "req_sd")
        assert ack["status"] == "rejected", ack
        assert ack["error"]["code"] == "AGENT_BUSY", ack
        assert ack["error"]["retryable"] is True, ack

        # 恢复运行后再发同 key 同内容 → 瞬时拒绝不落终态，重试回到 IN_PROGRESS
        # （幂等表里**没有** accepted 被写进去，这正是 CR-08 的诉求）
        agent._state = RunState.RUNNING
        ack2 = await _send_and_read_ack(ws, "停机窗口的消息", "req_sd")
        assert ack2["status"] == "rejected", f"不得变成 accepted：{ack2}"
        assert ack2["error"]["code"] == "IN_PROGRESS", (
            f"瞬时拒绝不该被逐字重放成永久失败：{ack2['error']}")
        assert ack2["error"]["retryable"] is True
    finally:
        # 断言失败也必须关 WS——否则未关闭的连接会让 aiohttp 测试服务器收尾挂死，
        # 把「一条失败」变成「整个 job 超时」。
        if ws is not None:
            await ws.close()
        agent._state = RunState.RUNNING
        await agent.stop()
