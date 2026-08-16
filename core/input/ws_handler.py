"""WebSocket handler —— WS 生命周期、消息路由、打断、转写。"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiohttp import web, WSMsgType
from pydantic import TypeAdapter, ValidationError

from core.memory.session_manager import SessionBusyError, SessionNotFoundError
from core.schema.event import InterruptData, UserInputData
from core.schema.message import ClientMessageDict
from core.schema.part import AudioPart, FilePart, ImagePart, StickerPart, TextPart, VideoPart
from core.schema.utils import now_iso
from core.schema.types import AttachmentId, SessionId
from core.storage.attachment_repo import StorageUnavailableError
from Module.output import WebSocketChannel
from core.input.audio_buffer import AudioBuffer
from core.input._common import _find_channel, _send_ack
from core.services.capabilities import build_capabilities
from core.input.ws_v2 import _handle_voice_commit, _route_v2_text
from core.storage.idempotency import (
    GRACE_SECONDS,
    _IN_PROGRESS_RETRY_AFTER_MS,
    IdempotencyEntry,
    get_store,
)
from core.network.rate_limiter import RateLimiter, rate_key_for_request
from core.input.sticker_registry import get as get_sticker
from core.input.voice_sessions import VoiceSessionError, VoiceSessionManager
from core.storage.idempotency_repo import STATUS_ACCEPTED, STATUS_REJECTED
from core.utils import get_request_id
from frontend_contract import (
    AcceptedUserAckMessage,
    AckError,
    CommandMessage,
    ErrorMessage,
    ExpressionMessage,
    RejectedUserAckMessage,
    SessionMessage,
    SessionMessageItem,
    ServiceModeResultMessage,
    SetTtsVoiceResultMessage,
    SwitchModelResultMessage,
    TokenStats,
    TranscriptionMessage,
    WakeMessage,
)
from frontend_contract.ws_message import (
    AudioInputDescriptor, ClientMessage, V2TextMessage,
)
from frontend_contract.version import TRANSPORT_VERSION
from core.input.tts_profile import _active_tts_profile

if TYPE_CHECKING:
    from core.agent import Agent
    from core.memory.session import SessionMemory

logger = logging.getLogger(__name__)


def resolve_protocol_version(header: str) -> int | None:
    """X-Protocol-Version 头 → 协议版本号（9B-3 单源判定）。

    支持集合：缺省/空 → 1（v1 legacy），``"1"`` → 1，``TRANSPORT_VERSION``("2") → 2；
    其余显式值 → None——调用方拒绝（server.py ws_handler wrapper 升级前 400，
    不静默降级 v1）。

    Args:
        header: X-Protocol-Version 头原始值（无头时 aiohttp 返回 ""）。

    Returns:
        1（v1）/ 2（v2）/ None（不支持的显式版本）。
    """
    if header in ("", "1"):
        return 1
    if header == TRANSPORT_VERSION:  # "2"
        return 2
    return None


def _current_request_id() -> str | None:
    """当前 ContextVar 请求 ID（批次 7 关联 ID；"-" 视为无 → None）。

    WS 错误发送点构造 ``ErrorMessage.requestId`` 的数据源——handle_ws 同任务内
    ``get_request_id()`` 反映最近一次 ``process_message``/``next_request_id`` 设置的
    请求 ID（来源 UserInputData.request_id）。有则带、无则 None（缺省省略）。
    """
    rid = get_request_id()
    return rid if rid and rid != "-" else None


# D6b：WS 消息类型 → 限流类别（仅高成本/可滥用类别；低成本帧不限流）。
# text/voice → 普通消息（generation 高成本）；switch_model → provider test；
# tool_approval → 工具审批。
_WS_MESSAGE_RATE_CATEGORY: dict[str, str] = {
    "text": "messages",
    "voice.commit": "messages",
    "voice.record.start": "messages",
    "voice_session.start": "messages",
    "switch_model": "provider_test",
    "tool_approval": "tools",
}


def _extract_client_request_id(data: dict) -> str | None:
    """限流块在完整校验前提取 clientRequestId（严格长度/类型，不触发完整解析）。

    Task 7（BE-P1-03）：``data.get("clientRequestId")`` 为 str 且 ``0 < len ≤ 128``
    才返回，否则 None——缺失/非法（非 str/超长）时无合法关联键 → 调用方退化
    generic ErrorMessage（前端对 text/voice.commit 恒发 clientRequestId，缺失即
    畸形帧，文档化限制）。不做完整解析，仅严格长度/类型提取。
    """
    crid = data.get("clientRequestId")
    if isinstance(crid, str) and 0 < len(crid) <= 128:
        return crid
    return None


def _build_session_items(
    session_msgs: list[ClientMessageDict] | list[dict],
) -> list[SessionMessageItem]:
    """逐条契约化会话消息——单条失败仅丢弃该条，保留其余回放（M2 加固）。

    直接 ``SessionMessage(messages=session_msgs)`` 会整批校验：任一消息无法
    契约化（如 content 含 Text/Image 之外 part）→ 整段回放丢失（try/except
    捕获后仅告警，客户端拿到空历史）。这里逐条 model_validate，失败只跳
    过该条并告警。
    """
    items: list[SessionMessageItem] = []
    for m in session_msgs:
        try:
            items.append(SessionMessageItem.model_validate(m))
        except Exception:
            logger.warning("会话回放丢弃无法契约化的消息: %r", m.get("role"), exc_info=True)
            continue
    return items


async def _send_session_replay(ws: "web.WebSocketResponse", agent: "Agent",
                               session: "SessionMemory") -> None:
    """发送指定会话的 SessionMessage 回放（建连与 activate_session 复用）。

    逐条契约化（M2）——单条失败仅丢弃该条，不整段丢失回放；token_stats 取该会话统计。
    """
    session_msgs = session.messages_for_client()
    token_stats = None
    if hasattr(agent, 'token_tracker') and agent.token_tracker is not None:
        raw = await agent.token_tracker.get_session_stats(session.session_id)
        if raw:
            # 内部 API 仍用 by_model 键；契约边界显式转换 to byModel（批次 8c F19，
            # 裸 dict 直传会因 TokenStats.byModel 必填缺失校验失败 → 回放被吞）。
            token_stats = TokenStats(total=raw.get("total", 0), byModel=raw.get("by_model", {}))
    items = _build_session_items(session_msgs)
    await ws.send_json(SessionMessage(
        conversationId=str(session.session_id),
        messages=items, token_stats=token_stats).wire_dict())


@dataclass
class _WsConnection:
    """单条 WebSocket 连接的运行时状态。"""

    ws: web.WebSocketResponse
    channel: WebSocketChannel
    client_id: str
    protocol_version: int
    desktop_conn_id: str | None
    voice_mgr: VoiceSessionManager
    sent_message_ids: set[str]
    audio_buf: AudioBuffer | None = None
    monitor_task: asyncio.Task | None = None


async def _open_ws_connection(
    request: web.Request,
    agent: "Agent",
    wake_word_enabled: bool,
) -> _WsConnection:
    """握手、初始帧回放、voice/audio 装配——连接级 setup 全部收口在此。"""
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=30, receive_timeout=60)
    await ws.prepare(request)

    proto_header = request.headers.get("X-Protocol-Version", "")
    resolved = resolve_protocol_version(proto_header)
    if resolved is None:
        logger.warning("协议版本协商失败（wrapper 应已拦截）: %r → 回退 v1", proto_header)
        protocol_version = 1
    else:
        protocol_version = resolved

    client_platform = ""
    if protocol_version >= 2:
        client_platform = request.headers.get("X-Client-Platform", "")

    from core.utils import new_message_id
    client_id = new_message_id()
    channel = WebSocketChannel(ws, protocol_version=protocol_version,
                               client_platform=client_platform)
    conn_id = f"conn_{uuid.uuid4().hex[:12]}" if protocol_version >= 2 else None
    channel.connection_id = conn_id
    agent.output.add_channel(channel)

    desktop_conn_id: str | None = conn_id if client_platform == "electron" else None
    if desktop_conn_id is not None:
        try:
            await agent.desktop_bridge.on_client_connect(desktop_conn_id)
            agent.sync_desktop_available()
            logger.info("WS desktop 客户端已连接: %s (%s)", client_id, desktop_conn_id)
        except Exception:
            logger.debug("Desktop bridge on_client_connect 失败", exc_info=True)

    try:
        target = await agent.resolve_session(None, protocol_version)
        await _send_session_replay(ws, agent, target)
    except Exception:
        logger.warning("Failed to send session on connect", exc_info=True)

    try:
        if protocol_version >= 2:
            from core.services.capabilities import build_server_info
            await ws.send_json(
                build_server_info(agent, connection_id=desktop_conn_id).wire_dict())
        else:
            await ws.send_json(build_capabilities(agent).wire_dict())
    except Exception:
        logger.debug("Failed to send capabilities on connect", exc_info=True)

    try:
        if protocol_version >= 2:
            cs = agent.character_engine.state
        else:
            default_session = await agent.resolve_session(None, 1)
            cs = default_session.character_state
        await ws.send_json(ExpressionMessage(
            expression=cs.mood, intensity=cs.mood_intensity,
        ).wire_dict())
    except Exception:
        logger.debug("Failed to send mood on connect", exc_info=True)

    try:
        if hasattr(agent, 'tool_adapter') and agent.tool_adapter is not None:
            pending = agent.tool_adapter._manager.get_pending_approvals()  # noqa: SLF001
            for request_id, tool_name, params in pending:
                base = {
                    "tool": tool_name,
                    "params": str(params)[:200],
                    "request_id": request_id,
                }
                if protocol_version >= 2:
                    manager = agent.tool_adapter._manager  # noqa: SLF001
                    plugin = manager._plugins.get(tool_name)  # noqa: SLF001
                    if plugin is not None:
                        enriched: dict[str, Any] = dict(base)
                        enriched["summary"] = manager._build_param_summary(params)  # noqa: SLF001
                        enriched["runtime"] = plugin.runtime
                        enriched["version"] = plugin.version
                        enriched["riskLevel"] = plugin.risk_level
                        enriched["permissionRequired"] = plugin.permission_required
                        enriched["approvalAuthority"] = plugin.approval_authority
                        await ws.send_json(CommandMessage(
                            command="tool_approval",
                            params=enriched,
                        ).wire_dict())
                        logger.debug(
                            "Re-broadcast pending approval to new client (v2 enrich): %s (%s)",
                            request_id, tool_name)
                        continue
                await ws.send_json(CommandMessage(
                    command="tool_approval",
                    params=base,
                ).wire_dict())
                logger.debug("Re-broadcast pending approval to new client: %s (%s)",
                             request_id, tool_name)
    except Exception:
        logger.debug("Failed to re-send pending approvals", exc_info=True)

    voice_mgr = VoiceSessionManager(
        agent=agent, emit=lambda data: ws.send_json(data),
        on_vad_interrupt=lambda: do_interrupt(agent, skip_vad=True),
    )
    sent_message_ids: set[str] = set()

    audio_buf: AudioBuffer | None = None
    monitor_task: asyncio.Task | None = None
    if agent.stt is not None:
        wake_words = None
        if wake_word_enabled:
            try:
                wake_words = agent.get_wake_words()
            except Exception:
                logger.debug("Could not read wake_words from agent", exc_info=True)

        audio_buf = AudioBuffer(
            stt_transcribe=agent.stt_transcribe,
            aggressiveness=3,
            on_interrupt=lambda: do_interrupt(agent, skip_vad=True),
            on_transcription=lambda text: do_transcription(
                agent, ws, text, audio_buf, agent.stt_emotion_state,
            ),
            wake_words=wake_words,
            on_wake_notify=lambda: send_wake(ws),
        )

        async def _monitor_state():
            while not ws.closed:
                try:
                    await asyncio.sleep(0.1)
                    if agent.status.is_speaking and audio_buf:
                        audio_buf.deafen(2.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("State monitor error", exc_info=True)
        monitor_task = asyncio.create_task(_monitor_state(), name=f"state-monitor-{client_id}")

    logger.info("WS client connected: %s (STT=%s)", client_id, "yes" if audio_buf else "no")
    return _WsConnection(
        ws=ws,
        channel=channel,
        client_id=client_id,
        protocol_version=protocol_version,
        desktop_conn_id=desktop_conn_id,
        voice_mgr=voice_mgr,
        sent_message_ids=sent_message_ids,
        audio_buf=audio_buf,
        monitor_task=monitor_task,
    )


async def _run_ws_message_loop(
    conn: _WsConnection,
    request: web.Request,
    agent: "Agent",
) -> None:
    """连接建立后的消息循环：TEXT 路由 / BINARY 音频分流 / 错误隔离。"""
    async for msg in conn.ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
                if not isinstance(data, dict):
                    await conn.ws.send_json(ErrorMessage(
                        message="WS 消息必须是 JSON 对象",
                        code="CONTRACT_VALIDATION",
                        requestId=_current_request_id(),
                    ).wire_dict())
                    continue
                await handle_json_message(
                    data, agent, conn.audio_buf, conn.ws, voice_mgr=conn.voice_mgr,
                    sent_message_ids=conn.sent_message_ids,
                    limiter=request.app.get("rate_limiter"),
                    rate_key=rate_key_for_request(
                        request,
                        request.app.get("rate_valid_tokens", frozenset()))
                    if request.app.get("rate_limiter") is not None else None,
                )
            except json.JSONDecodeError:
                await conn.ws.send_json(ErrorMessage(
                    message="Invalid JSON",
                    code="CONTRACT_VALIDATION",
                    requestId=_current_request_id(),
                ).wire_dict())
            except ValidationError as e:
                err_summary = "; ".join(
                    f"{'.'.join(str(x) for x in err['loc'])}:{err['type']}"
                    for err in e.errors(include_input=False)
                )
                logger.warning("WS 消息内容校验失败: %s", err_summary)
                await conn.ws.send_json(ErrorMessage(
                    message=f"Invalid message content: {err_summary}",
                    code="CONTRACT_VALIDATION",
                    requestId=_current_request_id(),
                ).wire_dict())
            except Exception:
                logger.exception("WS 消息处理未预期异常——回 INTERNAL error，连接存活")
                try:
                    await conn.ws.send_json(ErrorMessage(
                        message="内部错误",
                        code="INTERNAL",
                        requestId=_current_request_id(),
                    ).wire_dict())
                except Exception:
                    logger.debug("发送 INTERNAL error 失败", exc_info=True)
                continue

        elif msg.type == WSMsgType.BINARY:
            if conn.voice_mgr is not None and conn.voice_mgr.has_active_session():
                await conn.voice_mgr.feed_pcm(msg.data)
            elif conn.protocol_version < 2 and conn.audio_buf and agent.stt_enabled:
                await conn.audio_buf.feed(msg.data)

        elif msg.type == WSMsgType.ERROR:
            logger.warning("WS error: %s", conn.ws.exception())


async def _close_ws_connection(conn: _WsConnection, agent: "Agent") -> None:
    """连接清理：monitor/audio/voice 关闭、channel 移除、desktop 下线、断线取消。"""
    if conn.monitor_task and not conn.monitor_task.done():
        conn.monitor_task.cancel()
    if conn.audio_buf:
        await conn.audio_buf.flush()
    if conn.voice_mgr is not None:
        conn.sent_message_ids.update(conn.voice_mgr.emitted_message_ids)
        await conn.voice_mgr.close()
    agent.output.remove_channel(conn.channel)
    if conn.desktop_conn_id is not None:
        try:
            await agent.desktop_bridge.on_client_disconnect(conn.desktop_conn_id)
            agent.sync_desktop_available()
            logger.info("WS desktop 客户端已断开: %s (%s)",
                        conn.client_id, conn.desktop_conn_id)
        except Exception:
            logger.debug("Desktop bridge on_client_disconnect 失败", exc_info=True)
    logger.info("WS client disconnected: %s", conn.client_id)
    if conn.protocol_version >= 2:
        try:
            gen_uid = getattr(getattr(agent, "cognitive", None),
                              "current_generation_user_message_id", None)
            if gen_uid and gen_uid in conn.sent_message_ids:
                from core.monitoring.event_bus import EventPriority
                agent.bus.post(InterruptData(source="disconnect"),
                               priority=EventPriority.INTERRUPT)
        except Exception:
            logger.debug("v2 断线取消 generation 失败", exc_info=True)


async def handle_ws(
    request: web.Request,
    agent: "Agent",
    wake_word_enabled: bool = False,
) -> web.WebSocketResponse:
    """处理来自浏览器客户端的 WebSocket 连接。

    管理完整 WS 生命周期：连接、会话历史回放、能力宣告、音频缓冲区搭建、
    消息路由与清理。

    STT 从 create_app 参数注入改为读 ``agent.stt`` 槽位：建连时
    ``agent.stt is not None`` 才建 audio_buf，转写/情绪经 ``agent.stt_transcribe``
    / ``agent.stt_emotion_state`` 读取（支持热切换）。

    Args:
        request: 发起 WebSocket 升级的 aiohttp Request。
        agent: 提供输出通道与消息处理的 Agent 实例。
        wake_word_enabled: 是否对本连接启用唤醒词检测。

    Returns:
        web.WebSocketResponse: 关闭后的 WebSocket 连接对象。
    """
    conn = await _open_ws_connection(request, agent, wake_word_enabled)
    try:
        await _run_ws_message_loop(conn, request, agent)
    except asyncio.CancelledError:
        pass
    finally:
        await _close_ws_connection(conn, agent)
    return conn.ws


def _is_v2(agent: "Agent", ws: "web.WebSocketResponse | None") -> bool:
    """三种语音模式消息 v2-only 门禁（仿 activate_session/tool.status）。"""
    ch = _find_channel(agent, ws)
    return ch is not None and ch.protocol_version >= 2


async def _send_voice_error(ws: "web.WebSocketResponse | None",
                            error: VoiceSessionError) -> None:
    """VoiceSessionError → error 信封（ws 可空——缺发送连接时静默跳过）。"""
    if ws is None:
        return
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
                          voice_mgr: "VoiceSessionManager | None") -> None:
    """会话 JSON 消息入口检查 stt_enabled（spec §5.3 NEW-D/NEW-W）。

    stt 关闭 → 结束活动 REALTIME/DICTATION 会话——暂停中会话无帧流入、idle 计时
    挂起，靠此入口检测避免静默悬挂至连接关闭；RECORDING 由 notify_stt_disabled
    内部忽略（spec §3.2 NEW-XA）。9abc P1-2：仅对依赖全局 STT 的会话生效——
    profile dictation（`_dictation_stt` 快照）独立于全局开关，stop 不被此门禁取消。
    """
    if (voice_mgr is not None and not getattr(agent, "stt_enabled", False)
            and voice_mgr.depends_on_global_stt()):
        try:
            await voice_mgr.notify_stt_disabled()
        except Exception:
            logger.debug("voice stt 门控 notify 失败", exc_info=True)


async def _dispatch_wake_toggle(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    enabled = msg.enabled if msg is not None else data.get("enabled", False)
    if audio_buf:
        audio_buf.set_wake_enabled(enabled)
        logger.debug("唤醒词开关: %s", "开启" if enabled else "关闭")

async def _dispatch_text(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    # 9abc P1-3：v2 文本完整帧 V2TextMessage 校验——ValidationError → ACK
    # rejected CONTRACT_VALIDATION（保留 v2 ACK rejected 通道，spec §4.2），
    # 非 ErrorMessage；合法帧 → 校验后模型交给 _route_v2_text（attachments 等
    # 为已校验字段，消除对未校验 data 加法/索引的 TypeError 断连源）。
    if channel is not None and channel.protocol_version >= 2:
        try:
            text_model = V2TextMessage.model_validate(data)
        except ValidationError as e:
            # clientRequestId 缺失/非 str 也是 ValidationError——错误 ACK 只能回显
            # 合法 str（非 str 原值直传会导致 UserAckMessage 构造失败、ACK 被吞）。
            raw_crid = data.get("clientRequestId", "")
            if not isinstance(raw_crid, str):
                raw_crid = ""
            # 9abc R4：clientMessageId/conversationId 同收口——非 str → None，
            # 否则 UserAckMessage 的 Optional[str] 校验抛 → bare except 吞 ACK，
            # 客户端拿到已拒绝的校验错误却无响应。
            raw_cmid = data.get("clientMessageId")
            raw_cid = data.get("conversationId")
            await _send_ack(ws, "", raw_crid,
                            raw_cmid if isinstance(raw_cmid, str) else None,
                            raw_cid if isinstance(raw_cid, str) else None,
                            "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": f"消息校验失败: {e}",
                                   "retryable": False})
            return
        # spec §3.8：v2 空文本/空媒体 → rejected CONTRACT_VALIDATION（不再静默丢弃）
        if not (text_model.content.strip() or text_model.stickerId
                or text_model.attachments):
            await _send_ack(ws, "", text_model.clientRequestId,
                            text_model.clientMessageId, text_model.conversationId,
                            "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": "消息内容为空", "retryable": False})
            return
        await _route_v2_text(agent, ws, text_model,
                             sent_message_ids=sent_message_ids)
        # BE-081 敏感纪律：只记文本长度 + 客户端消息 ID，不记正文
        logger.debug("WS 收到文本 (len=%d, clientMessageId=%s)",
                     len(text_model.content.strip()),
                     text_model.clientMessageId or "")
        return
    # v1 老路径（data.get + 多模态 content-as-parts，v1 不受 v2 收紧影响）
    text_content = data.get("content", "")
    if isinstance(text_content, str):
        if text_content.strip() or data.get("stickerId") or data.get("attachments"):
            await agent.process_message(text=text_content.strip())
            # BE-081 敏感纪律：只记文本长度 + 客户端消息 ID，不记正文
            logger.debug("WS 收到文本 (len=%d, clientMessageId=%s)",
                         len(text_content.strip()),
                         data.get("clientMessageId", ""))
        elif _is_v2(agent, ws):
            # 批次 8b（§3.8）：v2 空文本 → rejected CONTRACT_VALIDATION（不再静默丢弃）
            await _send_ack(ws, "", data.get("clientRequestId", ""),
                            data.get("clientMessageId"), data.get("conversationId"),
                            "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": "消息内容为空", "retryable": False})
    elif isinstance(text_content, list):
        # 多模态 content-as-parts 分支原样保留（不动）
        from core.schema.adapters.message_adapter import content_text, parts_from_dicts
        await agent.process_message(text=content_text(parts_from_dicts(text_content)), user_name=agent.user_name, content=text_content)
        logger.debug("WS 收到多模态消息")

async def _dispatch_interrupt(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    logger.debug("WS 收到打断指令")
    await do_interrupt(agent)

async def _dispatch_switch_model(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    service = msg.service if msg is not None else data.get("service", "")
    model = msg.model if msg is not None else data.get("model", "")
    if service == "llm":
        result = await agent.switch_llm(model)
    elif service == "stt":
        result = await agent.switch_stt(model)
    elif service == "tts":
        result = await agent.switch_tts(model)
    else:
        result = {"ok": False, "error": f"未知 service: {service}"}
    from Module.output import WebSocketChannel
    if ws is not None:
        for ch in agent.output._channels:  # noqa: SLF001
            if isinstance(ch, WebSocketChannel) and ch._ws is ws:  # noqa: SLF001
                try:
                    # 契约：error 仅失败带（成功缺省省略，非 null）
                    await ws.send_json(SwitchModelResultMessage(
                        service=service,
                        model=model,
                        ok=result.get("ok", False),
                        error=result.get("error"),
                    ).wire_dict())
                except Exception:
                    logger.debug("Failed to send switch_model_result", exc_info=True)
                break
    logger.info("WS 切换模型: service=%s model=%s ok=%s", service, model, result.get("ok"))

async def _dispatch_set_tts_voice(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    # 批次 5（Task 11，BE-051）：设置活动会话 TTS 声音覆盖。
    # 经活动会话激活 TTS 后端 list_voices() 校验 voice 存在 → 写活动会话
    # service_state.tts_voice + save → 回 set_tts_voice_result；未知 voice/
    # 后端不可用 → {ok: False, error}，不改 state（失败保留原选择）。
    voice = msg.voice if msg is not None else data.get("voice", "")
    result: dict = {"ok": False, "error": "TTS 未启用"}
    # review 修正：空串/缺省 voice = 清除覆盖，回退 character 默认音色——
    # 否则 tts_voice 只进不出（list_voices 只含系统音色，角色默认不在列），无恢复路径。
    if not voice:
        agent.session.service_state.tts_voice = None
        try:
            await agent.session.save()
        except Exception as e:
            logger.warning("TTS voice 已清除但持久化失败: %s", e)
        result = {"ok": True, "voice": None}
    backend = _active_tts_profile(agent)
    if backend is not None and voice:
        try:
            voices = await backend.list_voices()
            if not any(v.get("id") == voice for v in voices):
                result = {"ok": False, "error": f"未知音色: {voice}"}
            else:
                agent.session.service_state.tts_voice = voice
                try:
                    await agent.session.save()
                except Exception as e:
                    # 持久化失败不回滚已生效的写入——运行中 state 是权威，仅告警
                    logger.warning("TTS voice 已设置但持久化失败: %s", e)
                result = {"ok": True, "voice": voice}
        except Exception as e:
            logger.warning("set_tts_voice 失败: %s", e)
            # 信封不携带异常文本（review Minor）——只出通用消息
            result = {"ok": False, "error": "设置声音失败"}
    from Module.output import WebSocketChannel
    if ws is not None:
        for ch in agent.output._channels:  # noqa: SLF001
            if isinstance(ch, WebSocketChannel) and ch._ws is ws:  # noqa: SLF001
                try:
                    await ws.send_json(SetTtsVoiceResultMessage(
                        ok=result.get("ok", False),
                        voice=result.get("voice"),
                        error=result.get("error"),
                    ).wire_dict())
                except Exception:
                    logger.debug("Failed to send set_tts_voice_result", exc_info=True)
                break
    logger.info("WS 设置 TTS 声音: voice=%s ok=%s", voice, result.get("ok"))

async def _dispatch_set_service(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    service = msg.service if msg is not None else data.get("service", "")
    enabled = msg.enabled if msg is not None else data.get("enabled", False)
    result = await agent.set_service(service, enabled)
    # 广播给所有 WebSocket 客户端（而非仅发送者）——含失败时的 enabled 字段。
    # 不复制 switch_model_result 模式（那只回给请求方）。capabilities 重播在 Task 7 接入。
    from Module.output import WebSocketChannel
    try:
        for ch in agent.output._channels:  # noqa: SLF001
            if isinstance(ch, WebSocketChannel):
                # 契约：error 键恒存在（成功为 null，ts_null 标记）
                await ch._send_json(ServiceModeResultMessage(  # noqa: SLF001
                    service=service,
                    enabled=result.get("enabled", enabled),
                    ok=result.get("ok", False),
                    error=result.get("error"),
                ).wire_dict())
    except Exception:
        # 并发 connect/disconnect 会改 _channels 列表长度——捕获记录，
        # 不向请求方传播（per-channel 发送失败已由 _send_json 内部记录）
        logger.warning("set_service 广播失败（连接并发变化？）", exc_info=True)
    logger.info("WS 设置服务开关: service=%s enabled=%s ok=%s", service, enabled, result.get("ok"))
    # 批次 6（spec §3.2 NEW-X/XA）：stt 关闭成功 → 通知本连接语音会话（RECORDING 除外）。
    # 9abc P1-2：仅对依赖全局 STT 的会话生效——profile dictation 途中关闭全局 STT
    # 不取消独立会话（depends_on_global_stt() 判定，与 _voice_stt_gate 共用）。
    if (service == "stt" and not enabled and result.get("ok")
            and voice_mgr is not None and voice_mgr.depends_on_global_stt()):
        try:
            await voice_mgr.notify_stt_disabled()
        except Exception:
            logger.debug("notify_stt_disabled 失败", exc_info=True)

async def _dispatch_tool_approval(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    request_id = msg.request_id if msg is not None else data.get("request_id", "")
    approved = msg.approved if msg is not None else data.get("approved", False)
    if request_id:
        ok = agent.tool_adapter._manager.submit_approval(request_id, approved)  # noqa: SLF001
        if ok:
            logger.info("WS 审批结果: request=%s approved=%s", request_id, approved)
        else:
            logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)

async def _dispatch_activate_session(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    # 批次 3：切 active + 重发新活动会话回放；未知 conversationId → SESSION_NOT_FOUND
    # review 修复（兼容#5）：activate_session 仅 v2 连接处理——v1 恒 default 无
    # 切会话语义，处理会让 v1 连接意外切走 active 影响 v2 客户端。
    if ws is None:
        logger.debug("WS activate_session 无发送连接，忽略")
    else:
        channel = _find_channel(agent, ws)
        if channel is None or channel.protocol_version < 2:
            logger.debug("WS activate_session: v1 连接忽略（v1 恒 default）")
        else:
            await _handle_activate_session(agent, ws, data)

async def _dispatch_tool_status(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    # 批次 4（spec §5.3）：桌面工具状态回传 → bridge.handle_status。
    # 仅 v2 连接处理——v1 无 desktop 概念，忽略（含 v1 伪冒 platform 头隔离）。
    # 批次 8a（D2）：发送连接校验——channel.connection_id 作为 sender 传入
    # handle_status（发送连接必须是投递目标）；v2 但无 conn_id（异常）→ 拒绝。
    if ws is None:
        logger.debug("WS tool.status 无发送连接，忽略")
    else:
        channel = _find_channel(agent, ws)
        if channel is None or channel.protocol_version < 2:
            logger.debug("WS tool.status: v1 连接忽略")
        elif channel.connection_id is None:
            logger.warning("WS tool.status: channel 无 connection_id，拒绝")
        else:
            # data 已由判别联合校验为合法 ToolStatusMessage——bridge.handle_status
            # 契约接受 wire 帧（camelCase dict），原样透传（既有行为保持）。
            await agent.desktop_bridge.handle_status(
                data, sender_connection_id=channel.connection_id)

async def _dispatch_voice_record_start(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice.record.start: 非 v2 或无 mgr，忽略")
    else:
        try:
            await voice_mgr.start_recording(
                msg.recordingId if msg is not None else data.get("recordingId", ""),
                audio_descriptor=_voice_descriptor(msg, data))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_voice_record_stop(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice.record.stop: 非 v2 或无 mgr，忽略")
    else:
        try:
            await voice_mgr.stop_recording(
                msg.recordingId if msg is not None else data.get("recordingId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_voice_commit(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice.commit: 非 v2 或无 mgr，忽略")
    else:
        await _handle_voice_commit(agent, ws, voice_mgr, data,
                                   sent_message_ids=sent_message_ids)

async def _dispatch_voice_discard(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice.discard: 非 v2 或无 mgr，忽略")
    else:
        try:
            await voice_mgr.discard_recording(
                msg.recordingId if msg is not None else data.get("recordingId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_voice_session_start(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice_session.start: 非 v2，忽略")
    else:
        await _voice_stt_gate(agent, voice_mgr)
        try:
            await voice_mgr.start_realtime(
                msg.sessionId if msg is not None else data.get("sessionId", ""),
                msg.conversationId if msg is not None else data.get("conversationId"),
                audio_descriptor=_voice_descriptor(msg, data))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_voice_session_pause(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice_session.pause: 非 v2，忽略")
    else:
        await _voice_stt_gate(agent, voice_mgr)
        try:
            await voice_mgr.pause_session(
                msg.sessionId if msg is not None else data.get("sessionId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_voice_session_resume(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice_session.resume: 非 v2，忽略")
    else:
        await _voice_stt_gate(agent, voice_mgr)
        try:
            await voice_mgr.resume_session(
                msg.sessionId if msg is not None else data.get("sessionId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_voice_session_stop(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice_session.stop: 非 v2，忽略")
    else:
        await _voice_stt_gate(agent, voice_mgr)
        try:
            await voice_mgr.stop_session(
                msg.sessionId if msg is not None else data.get("sessionId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_voice_session_cancel(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("voice_session.cancel: 非 v2，忽略")
    else:
        await _voice_stt_gate(agent, voice_mgr)
        try:
            await voice_mgr.cancel_session(
                msg.sessionId if msg is not None else data.get("sessionId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_dictation_start(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("dictation.start: 非 v2，忽略")
    else:
        await _voice_stt_gate(agent, voice_mgr)
        try:
            await voice_mgr.start_dictation(
                msg.clientRequestId if msg is not None else data.get("clientRequestId", ""),
                data.get("sttProfileId"),
                audio_descriptor=_voice_descriptor(msg, data))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_dictation_stop(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("dictation.stop: 非 v2，忽略")
    else:
        await _voice_stt_gate(agent, voice_mgr)
        try:
            await voice_mgr.stop_dictation(
                msg.clientRequestId if msg is not None else data.get("clientRequestId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_dictation_cancel(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("dictation.cancel: 非 v2，忽略")
    else:
        await _voice_stt_gate(agent, voice_mgr)
        try:
            await voice_mgr.cancel_dictation(
                msg.clientRequestId if msg is not None else data.get("clientRequestId", ""))
        except VoiceSessionError as e:
            await _send_voice_error(ws, e)

async def _dispatch_audio_cancel(data: dict, agent: "Agent", msg: Any, channel: WebSocketChannel | None, audio_buf: AudioBuffer | None = None, ws: web.WebSocketResponse | None = None,
                        voice_mgr: VoiceSessionManager | None = None, sent_message_ids: set | None = None, limiter: RateLimiter | None = None, rate_key: str | None = None) -> None:
    # 批次 6（BE-063）：取消在途 TTS 播放实例（全局播放操作，v2-only）。
    if voice_mgr is None or not _is_v2(agent, ws):
        logger.debug("audio.cancel: 非 v2，忽略")
    else:
        sid = msg.stream_id if msg is not None else data.get("stream_id")
        if sid is None:
            await _send_voice_error(ws, VoiceSessionError(
                "CONTRACT_VALIDATION", "缺少 stream_id"))
            return
        try:
            sid_int = int(sid)  # 非数字 → CONTRACT_VALIDATION（review #17）
        except (TypeError, ValueError):
            await _send_voice_error(ws, VoiceSessionError(
                "CONTRACT_VALIDATION", "stream_id 非法"))
            return
        tts_mgr = getattr(agent, "tts_manager", None)
        if tts_mgr is None or not tts_mgr.cancel_stream(sid_int):
            await _send_voice_error(ws, VoiceSessionError(
                "PLAYBACK_NOT_FOUND", "播放实例不存在或已结束"))

_WS_HANDLERS: dict[str, Callable[..., Awaitable[None]]] = {
    "wake_toggle": _dispatch_wake_toggle,
    "text": _dispatch_text,
    "interrupt": _dispatch_interrupt,
    "switch_model": _dispatch_switch_model,
    "set_tts_voice": _dispatch_set_tts_voice,
    "set_service": _dispatch_set_service,
    "tool_approval": _dispatch_tool_approval,
    "activate_session": _dispatch_activate_session,
    "tool.status": _dispatch_tool_status,
    "voice.record.start": _dispatch_voice_record_start,
    "voice.record.stop": _dispatch_voice_record_stop,
    "voice.commit": _dispatch_voice_commit,
    "voice.discard": _dispatch_voice_discard,
    "voice_session.start": _dispatch_voice_session_start,
    "voice_session.pause": _dispatch_voice_session_pause,
    "voice_session.resume": _dispatch_voice_session_resume,
    "voice_session.stop": _dispatch_voice_session_stop,
    "voice_session.cancel": _dispatch_voice_session_cancel,
    "dictation.start": _dispatch_dictation_start,
    "dictation.stop": _dispatch_dictation_stop,
    "dictation.cancel": _dispatch_dictation_cancel,
    "audio.cancel": _dispatch_audio_cancel,
}

async def handle_json_message(data: dict, agent: "Agent", audio_buf: "AudioBuffer | None" = None, ws: "web.WebSocketResponse | None" = None, voice_mgr: "VoiceSessionManager | None" = None, sent_message_ids: set | None = None, limiter: "RateLimiter | None" = None, rate_key: str | None = None) -> None:
    """将来自 WebSocket 客户端的 JSON 消息路由到对应 handler。

    Args:
        data: 已解析的 JSON 消息 dict，含 "type" 键。
        agent: 处理消息与工具审批的 Agent 实例。
        audio_buf: 用于音频/唤醒词处理的 AudioBuffer 实例，可为 None。
        ws: 用于发送响应的 WebSocket 连接，可为 None。
        voice_mgr: 批次 6 每连接 VoiceSessionManager（三模式语音路由），可为 None。
        sent_message_ids: 批次 8b（§3.7）D1 接线——v2 text 消息 accepted 时登记
            messageId 的可选集合（透传给 _route_v2_text，Task 9 消费）。
        limiter: D6b 限流器（create_app 挂载时传入，None 不检查）。
        rate_key: 本连接限流 key（认证主体/IP，handle_ws 解析），None 不检查。
    """
    msg_type = data.get("type", "")
    # D6b：WS 消息级限流——高成本类别超限拒绝（连接存活，可重试）。
    # 置于判别联合校验前：畸形帧洪泛同样消耗 token（防解析滥用）。
    # Task 7（BE-P1-03）：按消息类别分通道关联——text/voice.commit（合法
    # clientRequestId）走 rejected ACK（RATE_LIMITED + retryAfterMs）直接 settle
    # 前端对应 promise；其余命令 → generic ErrorMessage（+可选 clientRequestId +
    # 顶层 retryAfterMs）；clientRequestId 缺失/非法 → 退化 generic ErrorMessage。
    if limiter is not None and rate_key is not None:
        category = _WS_MESSAGE_RATE_CATEGORY.get(msg_type)
        if category is not None and not limiter.check(category, rate_key):
            # ws 可能为 None（音频流回调直调）；无发送目标则静默丢弃（限流即拒绝）
            if ws is not None:
                retry_after_ms = limiter.retry_after_ms(category, rate_key)
                client_request_id = _extract_client_request_id(data)
                if msg_type in ("text", "voice.commit") and client_request_id is not None:
                    # 9abc R4：clientMessageId/conversationId 非 str → None，否则
                    # UserAckMessage 的 Optional[str] 校验抛 → bare except 吞 ACK，
                    # 前端 promise 不 settle（与校验失败 ACK 同收口）。
                    raw_cmid = data.get("clientMessageId")
                    raw_cid = data.get("conversationId")
                    await _send_ack(
                        ws, "", client_request_id,
                        raw_cmid if isinstance(raw_cmid, str) else None,
                        raw_cid if isinstance(raw_cid, str) else None,
                        "rejected",
                        error=AckError(code="RATE_LIMITED", retryable=True,
                                       retryAfterMs=retry_after_ms,
                                       message="请求过于频繁，请稍后重试"))
                else:
                    await ws.send_json(ErrorMessage(
                        message="请求过于频繁，请稍后重试",
                        code="RATE_LIMITED",
                        requestId=_current_request_id(),
                        clientRequestId=client_request_id,
                        retryable=True,
                        retryAfterMs=retry_after_ms,
                        details={"retryHint": "请稍后重试"},
                    ).wire_dict())
            return
    # 9B-2（Task 5）：v2 连接入站统一走 ClientMessage 判别联合校验——未知 type /
    # 错误枚举 / 错误字段类型 / 缺必填 → ErrorMessage CONTRACT_VALIDATION，连接存活。
    # type="text" 特判跳过判别联合：text 由 V2TextMessage 独占（TextMessage 不入联合），
    # 但保留既有 v2 ACK rejected 通道（缺 clientRequestId → ACK rejected 而非
    # ErrorMessage，spec §4.2）；content 类型收紧由 text 分支内部完成（Q6）。
    # v1 连接保持 data.get 老路径（不破坏 v1 多模态 text）。
    msg: Any = None
    channel = _find_channel(agent, ws)
    if channel is not None and channel.protocol_version >= 2:
        if data.get("type") == "text":
            msg = None  # text 不走判别联合——保留既有 v2 text 错误通道
        else:
            try:
                msg = TypeAdapter(ClientMessage).validate_python(data)
            except ValidationError as e:
                if ws is not None:  # _find_channel 命中即 ws 非空，此处仅满足 pyright 收紧
                    await ws.send_json(ErrorMessage(
                        message=f"Invalid message: {e}", code="CONTRACT_VALIDATION",
                        requestId=_current_request_id()).wire_dict())
                return
    handler = _WS_HANDLERS.get(msg_type)
    if handler is not None:
        await handler(data, agent, msg, channel, audio_buf=audio_buf, ws=ws,
                       voice_mgr=voice_mgr, sent_message_ids=sent_message_ids,
                       limiter=limiter, rate_key=rate_key)
    else:
        logger.debug("WS 未知消息类型: %s", msg_type)


async def _handle_activate_session(agent: "Agent", ws: "web.WebSocketResponse", data: dict) -> None:
    """处理 activate_session：切 active + 重发目标会话回放；未知会话 → 404。

    spec §6.1：`await agent.switch_session(conversationId)` → 重发 SessionMessage
    （新活动会话回放，复用建连回放路径）。未知 conversationId → 不发回放，
    发 `ErrorMessage{code: SESSION_NOT_FOUND}`（批次 1 错误信封）。

    批次 3 review 修复（F1-a）：切换前 FSM 门禁——agent 忙（生成/TTS 中，
    is_accepting_input=False）→ 拒绝 AGENT_BUSY（retryable 语义由码名表达），
    不执行切换。防 engine 全局单例 attach_session 换绑与一次 v2 生成流式/TTS
    并发 → mood/L3 跨会话污染。
    """
    sid = data.get("conversationId", "")
    if not sid:
        await ws.send_json(ErrorMessage(
            message="缺少 conversationId",
            code="CONTRACT_VALIDATION",
            requestId=_current_request_id(),
        ).wire_dict())
        return
    try:
        if not getattr(agent.status, "is_accepting_input", True):
            logger.info("activate_session: agent 忙（is_accepting_input=False），拒绝切换 %s", sid)
            await ws.send_json(ErrorMessage(
                message="Agent 忙（生成/TTS 中），无法切换会话",
                code="AGENT_BUSY",
                requestId=_current_request_id(),
            ).wire_dict())
            return
        if not await agent.session_manager.has_session(sid):
            logger.debug("activate_session: 未知会话 %s → SESSION_NOT_FOUND", sid)
            await ws.send_json(ErrorMessage(
                message=f"会话不存在: {sid}",
                code="SESSION_NOT_FOUND",
                requestId=_current_request_id(),
            ).wire_dict())
            return
        target = await agent.switch_session(SessionId(sid))
        await _send_session_replay(ws, agent, target)
        logger.info("WS activate_session: 已切到会话 %s", sid)
    except SessionBusyError:
        # review 修复（批次 C）：switch() 双保险 busy check 抛 SessionBusyError——
        # 此前被通用 except Exception 吞成 INTERNAL。单独捕获 → AGENT_BUSY（可重试）。
        logger.info("activate_session: 双保险 busy check 命中（SessionBusyError），拒绝切换 %s", sid)
        await ws.send_json(ErrorMessage(
            message="Agent 忙（生成/TTS 中），无法切换会话",
            code="AGENT_BUSY",
            requestId=_current_request_id(),
        ).wire_dict())
    except Exception:
        logger.warning("activate_session 处理失败 sid=%s", sid, exc_info=True)
        await ws.send_json(ErrorMessage(
            message="激活会话失败",
            code="INTERNAL",  # review 修复（兼容#4）：INTERNAL_ERROR → 错误目录标准码 INTERNAL
            requestId=_current_request_id(),
        ).wire_dict())


async def do_interrupt(agent: "Agent", skip_vad: bool = False) -> None:
    """发布 INTERRUPT 事件。

    Args:
        agent: Agent 实例。
        skip_vad: 为 True 时，THINKING/EXECUTING 阶段抑制打断（VAD 误报防护）。
                  按钮打断应传 skip_vad=False（默认值）。
    """
    if skip_vad and agent.status.should_suppress_vad_interrupt:
        return
    from core.monitoring.event_bus import EventPriority
    agent.bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)


async def do_transcription(agent: "Agent", ws: "web.WebSocketResponse", text: str,
                           audio_buf: "AudioBuffer | None" = None,
                           stt_emotion_state: dict | None = None) -> None:
    """处理 STT 转写结果：回显给客户端、deafen 麦克风、发布事件。

    Args:
        agent: 用于输出与事件发布的 Agent 实例。
        ws: 用于将转写回显给客户端的 WebSocket 连接。
        text: STT 转写的文本。
        audio_buf: TTS 播放期间 deafen 的 AudioBuffer，可为 None。
        stt_emotion_state: 可选的 dict，含 STT 的 'emotion' 与 'events'。
    """
    emotion = ""
    events: list[str] = []
    if stt_emotion_state:
        emotion = stt_emotion_state.get("emotion", "")
        events = stt_emotion_state.get("events", [])

    # 将转写结果发回客户端，供其显示用户语音
    try:
        # 契约：emotion/events 空时缺省省略（X?），非空才带
        msg = TranscriptionMessage(
            text=text,
            emotion=emotion or None,
            events=events or None,
        ).wire_dict()
        await ws.send_json(msg)
    except Exception:
        logger.debug("Failed to send transcription echo", exc_info=True)

    if audio_buf:
        audio_buf.deafen(2.0)
    rid = agent.next_request_id()
    # BE-081 敏感纪律：只记 request_id + 文本长度，不记正文
    logger.debug("STT 识别: rid=%s len=%d%s", rid, len(text),
                 f" [{emotion}]" if emotion and emotion != "NEUTRAL" else "")
    # 语音路径绕过 process_message，需手动 touch——否则纯语音对话
    # 会被误判为"用户沉默"，触发 silence_timeout 主动搭话
    agent.idle.touch()
    agent.bus.post(UserInputData(
        text=text, user_name=agent.user_name,
        emotion=emotion, events=events,
        request_id=rid,
    ))


async def send_wake(ws: "web.WebSocketResponse") -> None:
    """通知前端已检测到唤醒词。

    Args:
        ws: 用于发送通知的 WebSocket 连接。
    """
    try:
        await ws.send_json(WakeMessage().wire_dict())
    except Exception:
        logger.debug("Failed to send wake notification", exc_info=True)
