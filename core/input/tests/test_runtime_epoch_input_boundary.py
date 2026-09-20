# 测试文件：core/input/tests/test_runtime_epoch_input_boundary.py
"""KB-48（Residual Correctness Closure）：输入生产者不得跨 runtime epoch 投递。

## 窗口的形状（为什么 Batch 14 的清扫挡不住它）

Batch 14 的 RO1 在 `stop()` **最后一步**清扫 USER_INPUT（`discard_user_inputs`），
但输入层有两条**直投**入口跑在 aiohttp 的 WS 任务上：

```text
stop()  :  _state = STOPPED → …20 个关停步骤… → RO1 清扫 → "Agent stopped"（返回）
runner.cleanup()  ← 在这之后才取消 WS 连接/请求任务
```

`do_transcription`（STT 结果回传，单段预算 ≤30s）与 `_handle_voice_commit`
（v2 语音提交）都在**那之后**才真正执行 `bus.post(UserInputData)`。清扫之后再投的
事件留在队列里，同一实例 `start()` 后被消费者当**新输入**消费 ⇒ 旧 epoch 的输入
在新 epoch 复活。

同类入口已按 S2-R2-4/CR-08 加了 `status.is_service_running` 判据
（REALTIME 语音段、`process_message`、`do_interrupt`）——这两条没有。

## 本文件钉住的合同（RE1–RE3）

- **RE1** 在 epoch N 创建的生产者，不得在 N 已停止后把用户输入投进 N+1；
- **RE2** `stop()` 建立的是**硬准入边界**（拒收而不排队——停机窗口没有"稍后再处理"，
  与 `process_message` 同口径）；
- **RE3** 拒收陈旧生产者输出**不得**误伤 durable 的 `TaskResultData`（Batch 1.7/14
  的 TaskResult 保全）。

## 确定性

不靠 sleep 造窗口：先用 `agent.stop()` **真的跑完**（RO1 清扫已发生、`stop()` 已返回），
再调用生产者的真实函数——这正是生产窗口里的时序（消费者已退出、清扫已完成）。

`RE-T7`（stop 有界）由既有 `test_shutdown_resilience.py` / `test_shutdown_steps_matrix.py`
覆盖；本文件只补 KB-48 真正缺的那四条 + 新 epoch 的 liveness。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.agent import RunState
from core.schema.event import EventType, TaskResultData
from core.storage.idempotency import reset_store

pytestmark = pytest.mark.service

_WS_EVENTS: list[dict] = []


class _StubVoiceMgr:
    """最小 voice manager 替身：`commit_recording` 返回 pending 文本。

    修复**前**的代码会真的走到 `commit_recording`（准入判据不存在），所以替身必须
    能正常返回——否则用例会红在 `AttributeError`（一个不存在的符号）而不是红在缺陷
    的断言上，那违反本仓的 PRE_FIX_RED 纪律。
    """

    def commit_recording(self, recording_id: str, text: str):
        return text, "", []


class _StubWs:
    """最小 WS 替身：只记 `send_json`（转写回显 / ACK 都走它）。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _make_agent():
    """与 `core/input/tests/test_server.py::_make_agent` 同源的最小真 Agent。"""
    from core.config import AppConfig
    from core.agent import Agent
    from core.tests.fake_llm import FakeLLMBackend

    agent = Agent(config=AppConfig())
    agent.llm._backend = FakeLLMBackend(response="test")
    return agent


async def _started_agent():
    agent = _make_agent()
    await agent.start()
    return agent


def _user_texts(agent: Any) -> list[str]:
    return [str(m.content) for m in agent.session.messages if m.role == "user"]


# ═══════════════════════════════════════════════════════════════════
# RE-T1 / RE-T2：STT 转写跨 stop / restart
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_re_t1_late_transcription_after_stop_is_refused():
    """RE-T1（PRE_FIX_RED）：`stop()` 返回后到达的 STT 转写不得进总线。"""
    from core.input.ws_handler import do_transcription

    agent = await _started_agent()
    await agent.stop()
    assert agent.state is RunState.STOPPED, "用例前提：stop() 已返回"

    ws = _StubWs()
    before = agent.bus.pending_user_inputs
    await do_transcription(agent, ws, "停机窗口里才回来的转写", stt_emotion_state=None)

    assert agent.bus.pending_user_inputs == before, (
        "停机窗口里 STT 仍把用户输入投进了总线 —— 它会跨 epoch 复活")
    assert not agent.pending_messages


@pytest.mark.asyncio
async def test_re_t2_late_transcription_cannot_revive_across_restart():
    """RE-T2：重启后不得消费到旧 epoch 的转写（跨 epoch 复活的完整形态）。"""
    from core.input.ws_handler import do_transcription

    agent = await _started_agent()
    await agent.stop()

    ws = _StubWs()
    await do_transcription(agent, ws, "旧 epoch 的转写内容", stt_emotion_state=None)

    await agent.start()
    try:
        await asyncio.sleep(0)
        for _ in range(200):
            await asyncio.sleep(0)
            if agent.status.is_completely_idle and not agent.pending_messages:
                break
        texts = _user_texts(agent)
        assert not any("旧 epoch 的转写内容" in t for t in texts), (
            f"旧 epoch 的输入在新 epoch 被消费（跨 epoch 复活）：{texts}")
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_re_t3_current_epoch_transcription_still_works():
    """RE-T3（负向控制）：运行中的转写照常投递——新判据不得误伤正常路径。"""
    from core.input.ws_handler import do_transcription

    agent = await _started_agent()
    try:
        ws = _StubWs()
        await do_transcription(agent, ws, "正常运行中的转写", stt_emotion_state=None)
        assert agent.bus.pending_user_inputs >= 1, "运行中的转写被误拒"
        # 回显仍发出（客户端需要清录音态）——被拒的只是入队
        assert any(e.get("type") == "transcription" for e in ws.sent), ws.sent
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_re_t4_new_epoch_producer_works():
    """RE-T4：epoch 2 的新转写照常投递（重启不把入口焊死）。"""
    from core.input.ws_handler import do_transcription

    agent = await _started_agent()
    await agent.stop()
    await agent.start()
    try:
        ws = _StubWs()
        await do_transcription(agent, ws, "新 epoch 的转写", stt_emotion_state=None)
        assert agent.bus.pending_user_inputs >= 1, "新 epoch 的转写被误拒（入口被焊死）"
    finally:
        await agent.stop()


# ═══════════════════════════════════════════════════════════════════
# RE-T5：v2 voice.commit
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_re_t5_late_voice_commit_after_stop_is_refused():
    """RE-T5（PRE_FIX_RED）：`stop()` 返回后的 `voice.commit` 不得进总线，且回 rejected。"""
    from core.input.ws_v2 import _handle_voice_commit

    reset_store()
    agent = await _started_agent()
    await agent.stop()

    ws = _StubWs()
    before = agent.bus.pending_user_inputs
    msg = {
        "type": "voice.commit",
        "recordingId": "rec-kb48",
        "text": "停机窗口里提交的语音",
        "clientRequestId": "req-kb48",
        "clientMessageId": "cmsg-kb48",
        "conversationId": "default",
    }
    await _handle_voice_commit(agent, ws, _StubVoiceMgr(), msg)

    assert agent.bus.pending_user_inputs == before, (
        "停机窗口里 voice.commit 仍投进了总线 —— 它会跨 epoch 复活")
    assert not agent.pending_messages


# ═══════════════════════════════════════════════════════════════════
# RE-T6：durable TaskResultData 不受影响（RO2 / Batch 1.7/14）
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_re_t6_task_results_are_not_filtered():
    """RE3：陈旧用户输入的保护**不得**误伤 durable 的任务终态事件。

    `discard_user_inputs()` 的既有边界就是"只按 event type 分类，不是 clear()"
    （RO2）。本批没有改它——这里把它钉在**同一文件**里，防止将来有人把
    "旧生产者不许投递"实现成"停机就清空一切"。
    """
    agent = await _started_agent()
    payload = TaskResultData(
        task_id="wf-kb48-durable", result="结果正文", original_intent="意图",
        dispatched_at="2026-09-20T00:00:00+00:00")

    # 停机清扫（RO1）之后 durable 事件仍在总线上
    agent.bus.post(payload)
    agent._discard_abandoned_user_inputs()  # noqa: SLF001 — RO1 的清扫点
    survivors = [e for e in agent.bus.drain()]
    assert any(e.type == EventType.TASK_RESULT for e in survivors), (
        f"RO1 清扫误杀了 durable 的 TaskResultData：{[e.type for e in survivors]}")
    await agent.stop()


@pytest.mark.asyncio
async def test_re_t7_stop_remains_bounded():
    """RE-T7：加了准入判据之后 `stop()` 仍是有界的（不引入新的等待）。"""
    agent = await _started_agent()
    await asyncio.wait_for(agent.stop(), timeout=20.0)
    assert agent.state is RunState.STOPPED
