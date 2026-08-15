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
            if frame.get("type", "").startswith("generation."):
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


_MISSING_FRAMES = {
    "voice.transcript", "dictation.partial", "dictation.final", "dictation.cancelled",
    "audio", "audio_cancelled", "switch_model_result", "service_mode_result",
    "set_tts_voice_result",
}


def test_stage4_frames_present_in_goldens():
    """9 类阶段4缺帧必须在 golden 中至少出现一次（BE-S4-INT-P0-02）。

    仅统计带 `type` 判别键的 WS ServerMessage 帧——REST 样例文件（rest-*.jsonl）
    是无判别键的裸 wire 响应对象，不计入。
    """
    present: set[str] = set()
    for name in _ARTIFACTS:
        for frame in _frames(name):
            if "type" in frame:
                present.add(frame["type"])
    missing = _MISSING_FRAMES - present
    assert not missing, f"golden 缺帧: {sorted(missing)}"


def test_stage4_ws_frames_parse_as_server_message():
    """每个阶段4 WS 帧可被 ServerMessage 判别联合解析（spec §4.3 P2）。

    ServerMessage 是 Annotated[Union[...]] 类型别名，无 model_validate——
    用 TypeAdapter(ServerMessage).validate_python(wire_dict)（对齐既有
    frontend_contract/tests/test_batch5_contract.py 的 pydantic 等价机制与
    ws_handler.py:1023 同法）。
    """
    from pydantic import TypeAdapter
    from frontend_contract.ws_message import ServerMessage
    adapter = TypeAdapter(ServerMessage)
    for name in ("ws-v2-recording.jsonl", "ws-v2-dictation.jsonl",
                 "ws-v2-audio.jsonl", "ws-v2-service.jsonl"):
        for line in _frames(name):
            parsed = adapter.validate_python(line)
            assert parsed.type == line["type"], f"{name} {line['type']} 解析不一致"


def test_stage4_rest_frames_parse_as_named_models():
    """阶段4 REST 样例按文档化映射解析（spec §4.3 P3）。"""
    from frontend_contract import rest
    mapping = {
        "rest-providers.jsonl": rest.ProvidersResponse,
        "rest-provider-test.jsonl": rest.ProviderTestResult,
        "rest-tts-voices.jsonl": rest.TtsVoicesResponse,
        "rest-tts-preview.jsonl": rest.TtsPreviewResult,
        "rest-envelopes.jsonl": None,  # 混合 3 模型，逐行按 ok/code 判别（下方 P3b）
    }
    for name, model in mapping.items():
        if model is None:
            continue
        for line in _frames(name):
            parsed = model.model_validate(line)
            assert parsed is not None


def test_stage4_envelopes_parse():
    """rest-envelopes 行按 ok/code 判别：ApiOk(ok=true) / ApiFail(ok=false) / ProtocolError(code)。"""
    from frontend_contract import rest
    for line in _frames("rest-envelopes.jsonl"):
        if line.get("code"):
            parsed = rest.ProtocolError.model_validate(line)
            assert parsed.error == parsed.message
        elif line.get("ok") is True:
            rest.ApiOk.model_validate(line)
        else:
            rest.ApiFail.model_validate(line)


def test_stage4_result_frames_key_shapes():
    """结果帧正反例键存在性对齐模型注释/TS 生成（spec §4.3 P4）。"""
    svc_frames = _frames("ws-v2-service.jsonl")
    ok_sw = [f for f in svc_frames if f["type"] == "switch_model_result" and f["ok"]][0]
    assert ok_sw["ok"] is True and "error" not in ok_sw          # 成功无 error 键
    fail_sw = [f for f in svc_frames
               if f["type"] == "switch_model_result" and f["ok"] is False][0]
    assert "error" in fail_sw
    ok_mode = [f for f in _frames("ws-v2-service.jsonl")
               if f["type"] == "service_mode_result" and f["ok"] is True][0]
    assert ok_mode["error"] is None                              # 成功 error 显式 null（TsNull）
    ok_voice = [f for f in _frames("ws-v2-service.jsonl")
                if f["type"] == "set_tts_voice_result" and f["ok"] is True][0]
    assert ok_voice.get("voice") and "error" not in ok_voice
    # REST 成功/失败
    test_ok = [f for f in _frames("rest-provider-test.jsonl") if f["ok"]][0]
    assert "error" not in test_ok and test_ok["latency_ms"] == 12
    preview_ok = [f for f in _frames("rest-tts-preview.jsonl") if f["ok"]][0]
    assert preview_ok.get("mime") and preview_ok.get("audio_base64")


def test_stage4_audio_frames_stable_seq():
    """audio 帧 stream_id/seq/is_final 稳定、seq 从 0 递增、data 固定短值（spec §4.3 P5）。"""
    frames = _frames("ws-v2-audio.jsonl")
    audios = [f for f in frames if f["type"] == "audio"]
    assert [f["seq"] for f in audios] == [0, 1, 0]
    assert all(f["stream_id"] in (2, 3) for f in audios)
    assert all(len(f["data"]) <= 64 for f in audios)
    assert audios[1]["is_final"] is True and audios[0]["is_final"] is False
    assert all(f["is_final"] is False for f in audios if f["seq"] == 0)


def test_stage4_no_sensitive_data():
    """样例无真实音频/转写/密钥/绝对路径（spec §4.3 P6 + 验收 §7.5）。"""
    for name in ("ws-v2-recording.jsonl", "ws-v2-dictation.jsonl", "ws-v2-audio.jsonl",
                 "ws-v2-service.jsonl", "rest-providers.jsonl", "rest-provider-test.jsonl",
                 "rest-tts-voices.jsonl", "rest-tts-preview.jsonl", "rest-envelopes.jsonl"):
        blob = _ARTIFACTS[name]
        for banned in ("sk-", "Bearer ", "/home/", "BEGIN ", "http://127.0.0.1"):
            assert banned not in blob, f"{name} 含敏感内容 {banned!r}"
