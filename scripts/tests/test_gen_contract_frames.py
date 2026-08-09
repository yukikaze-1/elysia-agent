"""Golden frames 语义断言（P2-2）——不只字节漂移，校验与真实运行时语义一致。

真实运行时语义（review 确认）：
- voice 首轮 `turnId=1`（_on_segment 先 `self._turn_id += 1` 再发 partial/turn）；
- v2 text 缺省会话解析为活动会话（default），ACK 与全部 generation.* 携带
  非空 conversationId。
"""
import json

import pytest

from scripts.gen_contract_frames import _ARTIFACTS

pytestmark = pytest.mark.unit


def _frames(name: str) -> list[dict]:
    return [json.loads(line) for line in _ARTIFACTS[name].splitlines()
            if line.startswith("{")]


def test_voice_first_turn_id_is_one():
    """voice golden 首轮 partial/turn 的 turnId=1（真实 _on_segment 先自增）。"""
    frames = _frames("ws-v2-voice.jsonl")
    partial = next(f for f in frames if f["type"] == "voice_session.partial")
    turn = next(f for f in frames if f["type"] == "voice_session.turn")
    assert partial["turnId"] == 1, f"首轮 partial turnId 应为 1: {partial}"
    assert turn["turnId"] == 1, f"首轮 turn turnId 应为 1: {turn}"


def test_chat_ack_and_generation_have_conversation_id():
    """chat golden 的 ack 与全部 generation.* 携带 conversationId="default"。

    真实 v2 text 缺省 → 活动会话（default）；ACK 填实际 ID、generation.* 六类事件
    也携带非空实际 conversationId。
    """
    frames = _frames("ws-v2-chat.jsonl")
    ack = next(f for f in frames if f["type"] == "user_message.ack")
    assert ack["conversationId"] == "default", f"ack 应带 conversationId=default: {ack}"
    gen_frames = [f for f in frames if f["type"].startswith("generation.")]
    assert gen_frames, "chat golden 应含 generation.* 事件"
    for f in gen_frames:
        assert f.get("conversationId") == "default", \
            f"{f['type']} 应带 conversationId=default: {f}"
