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
            if line.strip()]


def test_all_artifacts_are_standard_jsonl():
    """跨仓 parser 可逐行直接 json.loads，不需要私有 `#` 注释跳过规则。"""
    for name, content in _ARTIFACTS.items():
        lines = [line for line in content.splitlines() if line.strip()]
        assert lines, f"{name} 不应为空"
        for line_no, line in enumerate(lines, 1):
            parsed = json.loads(line)
            assert isinstance(parsed, dict), f"{name}:{line_no} 必须是 JSON object"


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


def test_all_generation_frames_have_conversation_id():
    """所有 golden 的 generation.* 都携带非空实际会话，含 tool_calls 终态。"""
    for name in _ARTIFACTS:
        for frame in _frames(name):
            if frame["type"].startswith("generation."):
                assert frame.get("conversationId"), \
                    f"{name} 的 {frame['type']} 缺 conversationId: {frame}"


@pytest.mark.parametrize("name", ["ws-v2-cancel.jsonl", "ws-v2-fail.jsonl"])
def test_cancel_and_fail_are_current_contract_goldens(name: str):
    """取消/失败文件由当前契约生成：能力全开、ACK/生成归属完整、父终态唯一。"""
    frames = _frames(name)
    info = next(f for f in frames if f["type"] == "server_info")
    assert all(info["features"].values()), f"{name} 不应保留旧 features=false 快照"
    ack = next(f for f in frames if f["type"] == "user_message.ack")
    assert ack["conversationId"] == "default"
    terminals = [f for f in frames
                 if f["type"] in ("generation.completed", "generation.failed")]
    assert len(terminals) == 1, f"{name} 父终态必须唯一: {terminals}"
    assert terminals[0]["type"] == "generation.failed"
    assert terminals[0]["conversationId"] == "default"
    assert terminals[0]["model"] == "deepseek/deepseek-chat"
    assert [f.get("state") for f in frames if f["type"] == "state"][-2:] \
        == ["interrupted", "idle"]


def test_cancel_and_fail_error_semantics():
    """取消不可重试；Provider 故障可重试，错误码使用当前稳定目录值。"""
    cancelled = next(f for f in _frames("ws-v2-cancel.jsonl")
                     if f["type"] == "generation.failed")
    assert cancelled["code"] == "GENERATION_CANCELLED"
    assert cancelled["retryable"] is False

    failed = next(f for f in _frames("ws-v2-fail.jsonl")
                  if f["type"] == "generation.failed")
    assert failed["code"] == "GENERATION_PROVIDER_UNAVAILABLE"
    assert failed["retryable"] is True
