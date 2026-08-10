#!/usr/bin/env python3
"""契约层 golden frames 生成（Task 4，9B-1）。

无网络：构造 `ServerMessage` 契约模型 → `wire_dict()` → 标准 JSONL（每个非空行
都是 JSON 对象）写
`docs/archive/frontend-handoff-v2/traces/ws-v2-{handshake,session,chat,cancel,fail,tool,voice}.jsonl`。

帧序列为固定测试数据（确定性——漂移门禁要求字节稳定，不取 datetime.now），
由 frontend_contract 契约源生成，**非事实源**：真实捕获已停用
（`scripts/capture_ws_trace.py` DEPRECATED），本文件仅供 Electron 联调参考与漂移门禁兜底。
真实 wire 形状/事件序列以 `frontend_contract/` 契约 + `docs/electron-backend-protocol-v2.md` 为准。

模式：
  python scripts/gen_contract_frames.py         # 写 7 个 frames 到 traces 归档
  python scripts/gen_contract_frames.py --check # 生成到 tempfile 后 git diff 比对（漂移返回 1）
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frontend_contract import rest, ws_message as wm  # noqa: E402
from frontend_contract.version import PROTOCOL_VERSION  # noqa: E402

TRACES_DIR = (Path(__file__).resolve().parent.parent
              / "docs" / "archive" / "frontend-handoff-v2" / "traces")

# 固定测试时间戳（确定性——漂移门禁要求字节稳定）
_TS = "2026-08-09T00:00:00+00:00"


def _f(*msgs: wm.ContractModel) -> list[str]:
    """契约模型 → 逐行 JSON 帧（ensure_ascii=False 保中文，与 schema 产物同一序列化）。"""
    return [json.dumps(m.wire_dict(), ensure_ascii=False) for m in msgs]


def _server_info() -> wm.ServerInfoMessage:
    """v2 握手 server_info 帧（能力声明以当前 HEAD 契约能力为准）。"""
    return wm.ServerInfoMessage(
        protocol_version=PROTOCOL_VERSION,
        server_version="0.1.0",
        features=wm.ServerInfoFeatures(
            structured_generation=True, multi_conversation=True, desktop_runtime=True,
            dictation=True, voice_recording=True, voice_realtime=True, media_upload=True,
            idempotency_durable=True),  # D6c：当前契约支持幂等跨重启持久化
        services={
            "llm": wm.ServiceCapability(available=True, enabled=True,
                                        model="deepseek/deepseek-chat",
                                        default="deepseek/deepseek-chat"),
            "stt": wm.ServiceCapability(available=False, enabled=False,
                                        model=None, default="sensevoice/sense-voice"),
            "tts": wm.ServiceCapability(available=False, enabled=False,
                                        model=None, default="gpt-sovits/default"),
        },
        connectionId="conn_frame_0001",
        build_sha="0123abcd",
    )


def _handshake() -> list[str]:
    """v2 建连三帧：session → server_info → expression。"""
    session = wm.SessionMessage(conversationId="default", messages=[],
                                token_stats=wm.TokenStats(total=0, byModel={}))
    expression = wm.ExpressionMessage(expression="开心", intensity=0.65)
    return _f(session, _server_info(), expression)


def _chat() -> list[str]:
    """v2 结构化聊天：ack → generation.* 完整序列 → idle。

    9abc P2-2：ACK 与全部 generation.* 补 conversationId="default"——真实 v2 text
    缺省会话解析为活动会话（default），ACK 填实际 ID、generation.* 六类事件携带
    非空实际 conversationId（runtime 语义对齐，供跨仓 replay）。
    """
    ack = wm.UserAckMessage(messageId="msg_frame_ack1", clientRequestId="req_frame_chat1",
                            conversationId="default", receivedAt=_TS,
                            status=wm.AckStatus.ACCEPTED)
    thinking = wm.StateMessage(state="thinking")
    typing_on = wm.TypingMessage(typing=True)
    started = wm.GenerationStartedMessage(generationId="gen_frame_1",
                                          messageId="msg_frame_ast1",
                                          conversationId="default")
    iv_start = wm.GenerationSegmentStartedMessage(
        generationId="gen_frame_1", segmentId="seg_frame_iv0", segmentIndex=0,
        channel=wm.MessageChannel.INNER_VOICE, kind=wm.SegmentKind.TEXT,
        conversationId="default")
    iv_done = wm.GenerationSegmentCompletedMessage(
        generationId="gen_frame_1", segmentId="seg_frame_iv0", segmentIndex=0,
        channel=wm.MessageChannel.INNER_VOICE, kind=wm.SegmentKind.TEXT,
        content="我想认真回应这个话题。", conversationId="default")
    reply_start = wm.GenerationSegmentStartedMessage(
        generationId="gen_frame_1", segmentId="seg_frame_pub1", segmentIndex=1,
        channel=wm.MessageChannel.PUBLIC_REPLY, kind=wm.SegmentKind.TEXT,
        conversationId="default")
    delta = wm.GenerationDeltaMessage(
        generationId="gen_frame_1", segmentId="seg_frame_pub1", segmentIndex=1,
        deltaSequence=0, delta="你好！这是第一句话，回答你的问题。",
        conversationId="default")
    typing_off = wm.TypingMessage(typing=False)
    speaking = wm.StateMessage(state="speaking")
    reply_done = wm.GenerationSegmentCompletedMessage(
        generationId="gen_frame_1", segmentId="seg_frame_pub1", segmentIndex=1,
        channel=wm.MessageChannel.PUBLIC_REPLY, kind=wm.SegmentKind.TEXT,
        content="你好！这是第一句话，回答你的问题。", conversationId="default")
    completed = wm.GenerationCompletedMessage(
        generationId="gen_frame_1", messageId="msg_frame_ast1",
        channel=wm.MessageChannel.PUBLIC_REPLY, finishReason=rest.FinishReason.STOP,
        usage=wm.TokenUsage(
            rounds=[wm.TokenRound(model="deepseek/deepseek-chat", session_id="default",
                                  input_tokens=12, output_tokens=18, total_tokens=30,
                                  cached_tokens=0, reasoning_tokens=0, round_num=0,
                                  timestamp=_TS)],
            turn_total=30),
        model="deepseek/deepseek-chat", mood="开心", completeness="complete",
        conversationId="default",
        conversationUsage=rest.ConversationUsage(
            total=30, byModel={"deepseek/deepseek-chat": 30}))
    idle = wm.StateMessage(state="idle")
    return _f(ack, thinking, typing_on, started, iv_start, iv_done, reply_start, delta,
              typing_off, speaking, reply_done, completed, idle)


def _cancel() -> list[str]:
    """v2 取消：当前握手 + accepted → started → partial segment → failed → idle。

    事件顺序对齐 CognitiveLoop：流式任务取消的异常收尾先 typing=false；随后
    turn_cancelled 发唯一 generation.failed{GENERATION_CANCELLED}；统一 interrupt
    清理再发 typing=false、state interrupted、state idle。未完成 segment 不伪造
    segment.completed，前端以 generation.failed 结束父回复并保留已收增量。
    """
    ack = wm.UserAckMessage(messageId="msg_frame_cancel_ack",
                            clientRequestId="req_frame_cancel1",
                            conversationId="default", receivedAt=_TS,
                            status=wm.AckStatus.ACCEPTED)
    thinking = wm.StateMessage(state="thinking")
    typing_on = wm.TypingMessage(typing=True)
    started = wm.GenerationStartedMessage(
        generationId="gen_frame_cancel1", messageId="msg_frame_cancel_ast",
        conversationId="default", model="deepseek/deepseek-chat")
    seg_start = wm.GenerationSegmentStartedMessage(
        generationId="gen_frame_cancel1", segmentId="seg_frame_cancel_pub0",
        segmentIndex=0, channel=wm.MessageChannel.PUBLIC_REPLY,
        kind=wm.SegmentKind.TEXT, conversationId="default")
    delta = wm.GenerationDeltaMessage(
        generationId="gen_frame_cancel1", segmentId="seg_frame_cancel_pub0",
        segmentIndex=0, deltaSequence=0, delta="正在生成的部分内容",
        conversationId="default")
    typing_off_stream = wm.TypingMessage(typing=False)
    failed = wm.GenerationFailedMessage(
        generationId="gen_frame_cancel1", messageId="msg_frame_cancel_ast",
        model="deepseek/deepseek-chat", code="GENERATION_CANCELLED",
        message="已取消", retryable=False, conversationId="default")
    typing_off_interrupt = wm.TypingMessage(typing=False)
    interrupted = wm.StateMessage(state="interrupted")
    idle = wm.StateMessage(state="idle")
    return _handshake() + _f(
        ack, thinking, typing_on, started, seg_start, delta, typing_off_stream,
        failed, typing_off_interrupt, interrupted, idle)


def _fail() -> list[str]:
    """v2 Provider 失败：当前握手 + accepted → started → failed → idle。

    Provider 在首个 token 前失败，因此没有 segment 帧；generation.failed 是唯一父终态，
    retryable=true。异常收尾与统一 interrupt 清理会各发一次 typing=false。
    """
    ack = wm.UserAckMessage(messageId="msg_frame_fail_ack",
                            clientRequestId="req_frame_fail1",
                            conversationId="default", receivedAt=_TS,
                            status=wm.AckStatus.ACCEPTED)
    thinking = wm.StateMessage(state="thinking")
    typing_on = wm.TypingMessage(typing=True)
    started = wm.GenerationStartedMessage(
        generationId="gen_frame_fail1", messageId="msg_frame_fail_ast",
        conversationId="default", model="deepseek/deepseek-chat")
    typing_off_stream = wm.TypingMessage(typing=False)
    failed = wm.GenerationFailedMessage(
        generationId="gen_frame_fail1", messageId="msg_frame_fail_ast",
        model="deepseek/deepseek-chat", code="GENERATION_PROVIDER_UNAVAILABLE",
        message="模型服务暂时不可用", retryable=True, conversationId="default")
    typing_off_interrupt = wm.TypingMessage(typing=False)
    interrupted = wm.StateMessage(state="interrupted")
    idle = wm.StateMessage(state="idle")
    return _handshake() + _f(
        ack, thinking, typing_on, started, typing_off_stream, failed,
        typing_off_interrupt, interrupted, idle)


def _session() -> list[str]:
    """v2 多会话：建连 → activate 回放（conversationId 贯通）→ 生成 → idle。"""
    session_default = wm.SessionMessage(conversationId="default", messages=[],
                                        token_stats=wm.TokenStats(total=0, byModel={}))
    expression = wm.ExpressionMessage(expression="开心", intensity=0.65)
    session_conv = wm.SessionMessage(conversationId="conv_frame_1", messages=[],
                                     token_stats=wm.TokenStats(total=0, byModel={}))
    ack = wm.UserAckMessage(messageId="msg_frame_ack2", clientRequestId="req_frame_session1",
                            conversationId="conv_frame_1", receivedAt=_TS,
                            status=wm.AckStatus.ACCEPTED)
    started = wm.GenerationStartedMessage(generationId="gen_frame_2",
                                          messageId="msg_frame_ast2",
                                          conversationId="conv_frame_1")
    seg_start = wm.GenerationSegmentStartedMessage(
        generationId="gen_frame_2", segmentId="seg_frame_pub0", segmentIndex=0,
        channel=wm.MessageChannel.PUBLIC_REPLY, kind=wm.SegmentKind.TEXT,
        conversationId="conv_frame_1")
    seg_done = wm.GenerationSegmentCompletedMessage(
        generationId="gen_frame_2", segmentId="seg_frame_pub0", segmentIndex=0,
        channel=wm.MessageChannel.PUBLIC_REPLY, kind=wm.SegmentKind.TEXT,
        content="已切换到会话 conv_frame_1。", conversationId="conv_frame_1")
    completed = wm.GenerationCompletedMessage(
        generationId="gen_frame_2", messageId="msg_frame_ast2",
        channel=wm.MessageChannel.PUBLIC_REPLY, finishReason=rest.FinishReason.STOP,
        usage=wm.TokenUsage(rounds=[], turn_total=0),
        model="deepseek/deepseek-chat", mood="平静", completeness="complete",
        conversationId="conv_frame_1",
        conversationUsage=rest.ConversationUsage(total=0, byModel={}))
    idle = wm.StateMessage(state="idle")
    return _f(session_default, _server_info(), expression, session_conv, ack, started,
              seg_start, seg_done, completed, idle)


def _tool() -> list[str]:
    """桌面工具：tool.request → tool_approval 指令 → tool_calls 完成 → idle。"""
    request = wm.ToolRequestMessage(
        callId="call_frame_1", toolName="desktop.file.read", toolVersion="1.0.0",
        runtime="desktop",
        arguments={"path": "/tmp/frame_demo.txt", "offset": 0, "limit": 4096},
        reason="读取文件内容供用户确认", riskLevel="high", permissionRequired=True,
        approvalAuthority="client", paramHash="sha256_frame_demo",
        expiresAt=_TS, requestId="req_frame_tool1")
    command = wm.CommandMessage(command="tool_approval", params={
        "request_id": "req_frame_tool1", "call_id": "call_frame_1",
        "tool": "desktop.file.read", "tool_version": "1.0.0",
        "runtime": "desktop", "risk": "high"})
    completed = wm.GenerationCompletedMessage(
        generationId="gen_frame_tool1", messageId="msg_frame_tool1",
        channel=wm.MessageChannel.PUBLIC_REPLY, finishReason=rest.FinishReason.TOOL_CALLS,
        usage=wm.TokenUsage(rounds=[], turn_total=0),
        model="deepseek/deepseek-chat", completeness="complete",
        conversationId="default")
    idle = wm.StateMessage(state="idle")
    return _f(request, command, completed, idle)


def _voice() -> list[str]:
    """实时语音会话：started → partial 字幕 → turn 提交 → ack → 音频流 → ended。

    9abc P2-2：首轮 partial/turn 的 turnId=1——真实 _on_segment 先 `self._turn_id
    += 1` 再发帧（公开协议文档同样写 turnId=1）。
    """
    started = wm.VoiceSessionStartedMessage(sessionId="vs_frame_1", conversationId="default")
    partial = wm.VoiceSessionPartialMessage(sessionId="vs_frame_1", turnId=1,
                                            segmentIndex=0, text="你好")
    turn = wm.VoiceSessionTurnMessage(sessionId="vs_frame_1", turnId=1,
                                      messageId="msg_frame_vs1",
                                      conversationId="default", text="你好")
    ack = wm.VoiceSessionAckMessage(sessionId="vs_frame_1", seq=1)
    audio_start = wm.AudioStartMessage(stream_id=1, format=wm.AudioFormat.WAV,
                                       sampleRate=24000, durationMs=3200)
    audio_end = wm.AudioEndMessage(stream_id=1, format=wm.AudioFormat.WAV)
    ended = wm.VoiceSessionEndedMessage(sessionId="vs_frame_1",
                                        reason=wm.VoiceEndReason.STOPPED)
    return _f(started, partial, turn, ack, audio_start, audio_end, ended)


# 文件名 → 标准 JSONL 完整内容（模块级求值——确定性；元数据放 traces/README）
_ARTIFACTS = {
    "ws-v2-handshake.jsonl": "\n".join(_handshake()) + "\n",
    "ws-v2-session.jsonl": "\n".join(_session()) + "\n",
    "ws-v2-chat.jsonl": "\n".join(_chat()) + "\n",
    "ws-v2-cancel.jsonl": "\n".join(_cancel()) + "\n",
    "ws-v2-fail.jsonl": "\n".join(_fail()) + "\n",
    "ws-v2-tool.jsonl": "\n".join(_tool()) + "\n",
    "ws-v2-voice.jsonl": "\n".join(_voice()) + "\n",
}


def _check() -> int:
    """生成到临时目录后 git diff --exit-code 比对归档（防双写污染），漂移返回 1。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for name, content in _ARTIFACTS.items():
            gen = tmpdir / name
            gen.write_text(content, encoding="utf-8")
            archived = TRACES_DIR / name
            diff = subprocess.run(
                ["git", "diff", "--exit-code", "--no-index", "--", str(gen), str(archived)],
                capture_output=True, text=True)
            if diff.returncode != 0:
                print(f"{name} 漂移！请运行 python scripts/gen_contract_frames.py "
                      "重新生成后提交。", file=sys.stderr)
                print(diff.stdout, file=sys.stderr)
                return 1
    return 0


def main() -> int:
    """生成入口：默认写归档，--check 只校验不写。返回进程退出码。"""
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    if "--check" in sys.argv:
        return _check()
    for name, content in _ARTIFACTS.items():
        (TRACES_DIR / name).write_text(content, encoding="utf-8")
    print(f"已生成 {len(_ARTIFACTS)} 个 frames 到 {TRACES_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
