"""WS voice 域 handler：三模式语音（普通录音 / 实时语音 / 输入法转写）+ 音频取消。

ARCH-INC-P2-01（批次 D）领域化拆分；详见 `chat.py` 模块说明。
本模块同时承载语音专用小工具（`_is_v2` / `_send_voice_error` /
`_voice_descriptor` / `_voice_stt_gate`）——它们只被本域消费。
"""

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from Module.output import WebSocketChannel

if TYPE_CHECKING:
    from core.agent import Agent
from core.input.ws_handlers.context import WsRequestContext

# 复用父模块 logger 名（保持日志可追溯性：日志过滤/告警规则按
# `core.input.ws_handler` 配置，拆包不应改变运维可观测性）
logger = logging.getLogger("core.input.ws_handler")
from pydantic import ValidationError

from core.input._common import _find_channel
from core.input.voice_sessions import VoiceSessionError, VoiceSessionManager
from core.input.ws_v2 import _handle_voice_commit
from frontend_contract.ws_message import AudioInputDescriptor


async def _dispatch_voice_record_start(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice.record.start: 非 v2 或无 mgr，忽略")
    else:
        try:
            await ctx.voice_mgr.start_recording(
                msg.recordingId if msg is not None else data.get("recordingId", ""),
                audio_descriptor=_voice_descriptor(msg, data))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_voice_record_stop(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice.record.stop: 非 v2 或无 mgr，忽略")
    else:
        try:
            await ctx.voice_mgr.stop_recording(
                msg.recordingId if msg is not None else data.get("recordingId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_voice_commit(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice.commit: 非 v2 或无 mgr，忽略")
    else:
        await _handle_voice_commit(ctx.agent, ctx.ws, ctx.voice_mgr, data,
                                   sent_message_ids=ctx.sent_message_ids)

async def _dispatch_voice_discard(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice.discard: 非 v2 或无 mgr，忽略")
    else:
        try:
            await ctx.voice_mgr.discard_recording(
                msg.recordingId if msg is not None else data.get("recordingId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_voice_session_start(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice_session.start: 非 v2，忽略")
    else:
        if await _voice_stt_gate(ctx.agent, ctx.voice_mgr):
            return  # S3-08：门禁已发终态帧，本命令不得再产出第二帧
        try:
            await ctx.voice_mgr.start_realtime(
                msg.sessionId if msg is not None else data.get("sessionId", ""),
                msg.conversationId if msg is not None else data.get("conversationId"),
                audio_descriptor=_voice_descriptor(msg, data))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_voice_session_pause(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice_session.pause: 非 v2，忽略")
    else:
        if await _voice_stt_gate(ctx.agent, ctx.voice_mgr):
            return  # S3-08：门禁已发终态帧，本命令不得再产出第二帧
        try:
            await ctx.voice_mgr.pause_session(
                msg.sessionId if msg is not None else data.get("sessionId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_voice_session_resume(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice_session.resume: 非 v2，忽略")
    else:
        if await _voice_stt_gate(ctx.agent, ctx.voice_mgr):
            return  # S3-08：门禁已发终态帧，本命令不得再产出第二帧
        try:
            await ctx.voice_mgr.resume_session(
                msg.sessionId if msg is not None else data.get("sessionId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_voice_session_stop(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice_session.stop: 非 v2，忽略")
    else:
        if await _voice_stt_gate(ctx.agent, ctx.voice_mgr):
            return  # S3-08：门禁已发终态帧，本命令不得再产出第二帧
        try:
            await ctx.voice_mgr.stop_session(
                msg.sessionId if msg is not None else data.get("sessionId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_voice_session_cancel(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("voice_session.cancel: 非 v2，忽略")
    else:
        if await _voice_stt_gate(ctx.agent, ctx.voice_mgr):
            return  # S3-08：门禁已发终态帧，本命令不得再产出第二帧
        try:
            await ctx.voice_mgr.cancel_session(
                msg.sessionId if msg is not None else data.get("sessionId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_dictation_start(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("dictation.start: 非 v2，忽略")
    else:
        if await _voice_stt_gate(ctx.agent, ctx.voice_mgr):
            return  # S3-08：门禁已发终态帧，本命令不得再产出第二帧
        try:
            await ctx.voice_mgr.start_dictation(
                msg.clientRequestId if msg is not None else data.get("clientRequestId", ""),
                data.get("sttProfileId"),
                audio_descriptor=_voice_descriptor(msg, data))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_dictation_stop(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("dictation.stop: 非 v2，忽略")
    else:
        if await _voice_stt_gate(ctx.agent, ctx.voice_mgr):
            return  # S3-08：门禁已发终态帧，本命令不得再产出第二帧
        try:
            await ctx.voice_mgr.stop_dictation(
                msg.clientRequestId if msg is not None else data.get("clientRequestId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_dictation_cancel(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("dictation.cancel: 非 v2，忽略")
    else:
        if await _voice_stt_gate(ctx.agent, ctx.voice_mgr):
            return  # S3-08：门禁已发终态帧，本命令不得再产出第二帧
        try:
            await ctx.voice_mgr.cancel_dictation(
                msg.clientRequestId if msg is not None else data.get("clientRequestId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ctx.ws, e)

async def _dispatch_audio_cancel(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    # 批次 6（BE-063）：取消在途 TTS 播放实例（全局播放操作，v2-only）。
    if ctx.voice_mgr is None or not _is_v2(ctx.agent, ctx.ws):
        logger.debug("audio.cancel: 非 v2，忽略")
    else:
        sid = msg.stream_id if msg is not None else data.get("stream_id")
        if sid is None:
            await _send_voice_error(ctx.ws, VoiceSessionError(
                "CONTRACT_VALIDATION", "缺少 stream_id"))
            return
        try:
            sid_int = int(sid)  # 非数字 → CONTRACT_VALIDATION（review #17）
        except (TypeError, ValueError):
            await _send_voice_error(ctx.ws, VoiceSessionError(
                "CONTRACT_VALIDATION", "stream_id 非法"))
            return
        tts_mgr = getattr(ctx.agent, "tts_manager", None)
        if tts_mgr is None or not tts_mgr.cancel_stream(sid_int):
            await _send_voice_error(ctx.ws, VoiceSessionError(
                "PLAYBACK_NOT_FOUND", "播放实例不存在或已结束"))

def _is_v2(agent: "Agent", ws: "web.WebSocketResponse | None") -> bool:
    """三种语音模式消息 v2-only 门禁（仿 activate_session/tool.status）。"""
    ch = _find_channel(agent, ws)
    return ch is not None and ch.protocol_version >= 2


async def _send_voice_error(ws: "web.WebSocketResponse | None",
                            error: VoiceSessionError) -> None:
    """VoiceSessionError → error 信封（ws 可空——缺发送连接时静默跳过）。"""
    if ws is None:
        return
    # 连接级 helper 在 ws_handler（本包父模块）——函数内懒导入避免模块级环
    from core.input.ws_handler import _current_request_id
    from frontend_contract import ErrorMessage
    try:
        await ws.send_json(ErrorMessage(
            message=error.message, code=error.code,
            requestId=_current_request_id(),
            retryable=error.retryable,
        ).wire_dict())
    except Exception:
        logger.debug("发送 voice error 失败", exc_info=True)

def _voice_descriptor(msg: Any, data: dict) -> AudioInputDescriptor | None:
    """从 v2 判别联合 msg（已校验模型）或原始 data 提取输入音频描述（9C-1）。

    v2 连接 msg 为校验后的 start 帧模型，``msg.audio`` 已是 ``AudioInputDescriptor``；
    msg 为 None（防御路径）→ 走 ``data.get("audio")`` 原始 dict 解析，结构非法返回
    None（语义校验 codec/rate/channels 由 ``VoiceSessionManager._validate_audio_descriptor``
    负责，二者任一不符都会 CONTRACT_VALIDATION 拒绝）。
    """
    raw = msg.audio if msg is not None else data.get("audio")
    if raw is None:
        return None
    if isinstance(raw, AudioInputDescriptor):
        return raw
    try:
        return AudioInputDescriptor.model_validate(raw)
    except ValidationError:
        return None

async def _voice_stt_gate(agent: "Agent",
                          voice_mgr: "VoiceSessionManager | None") -> bool:
    """会话 JSON 消息入口检查 stt_enabled（spec §5.3 NEW-D/NEW-W）。

    stt 关闭 → 结束活动 REALTIME/DICTATION 会话——暂停中会话无帧流入、idle 计时
    挂起，靠此入口检测避免静默悬挂至连接关闭；RECORDING 由 notify_stt_disabled
    内部忽略（spec §3.2 NEW-XA）。9abc P1-2：仅对依赖全局 STT 的会话生效——
    profile dictation（`_dictation_stt` 快照）独立于全局开关，stop 不被此门禁取消。

    Returns:
        True 表示**门禁已终结会话并发出终态帧**——调用方必须**直接 return**，
        不得再执行原命令（S3-08）。否则：会话已 IDLE，命令的 `_require_session` 抛
        `VOICE_SESSION_NOT_FOUND`，客户端对**同一条命令**收到两帧语义矛盾的消息
        （先 `ended{stt_disabled}` 再 `NOT_FOUND`）。RECORDING / 无关会话返回 False，
        命令照常执行。
    """
    if (voice_mgr is not None and not getattr(agent, "stt_enabled", False)
            and voice_mgr.depends_on_global_stt()):
        try:
            return await voice_mgr.notify_stt_disabled()
        except Exception:
            logger.debug("voice stt 门控 notify 失败", exc_info=True)
    return False

