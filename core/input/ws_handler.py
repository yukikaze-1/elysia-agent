"""WebSocket handler —— WS 生命周期、消息路由、打断、转写。"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from aiohttp import web, WSMsgType
from pydantic import ValidationError

from core.memory.session_manager import SessionBusyError
from core.schema.event import InterruptData, UserInputData
from core.schema.part import AudioPart, FilePart, ImagePart, StickerPart, TextPart, VideoPart
from core.schema.utils import now_iso
from core.storage.attachment_repo import StorageUnavailableError
from Module.output import WebSocketChannel
from core.input.audio_buffer import AudioBuffer
from core.input.capabilities import build_capabilities
from core.input.idempotency import IdempotencyEntry, get_store
from core.input.sticker_registry import get as get_sticker
from core.input.voice_sessions import VoiceSessionError, VoiceSessionManager
from core.utils import get_request_id
from frontend_contract import (
    CommandMessage,
    ErrorMessage,
    ExpressionMessage,
    SessionMessage,
    SessionMessageItem,
    ServiceModeResultMessage,
    SetTtsVoiceResultMessage,
    SwitchModelResultMessage,
    TokenStats,
    TranscriptionMessage,
    UserAckMessage,
    WakeMessage,
)
from core.input.tts_profile import _active_tts_profile

if TYPE_CHECKING:
    from core.agent import Agent
    from core.memory.session import SessionMemory

logger = logging.getLogger(__name__)


def _current_request_id() -> str | None:
    """当前 ContextVar 请求 ID（批次 7 关联 ID；"-" 视为无 → None）。

    WS 错误发送点构造 ``ErrorMessage.requestId`` 的数据源——handle_ws 同任务内
    ``get_request_id()`` 反映最近一次 ``process_message``/``next_request_id`` 设置的
    请求 ID（来源 UserInputData.request_id）。有则带、无则 None（缺省省略）。
    """
    rid = get_request_id()
    return rid if rid and rid != "-" else None


def _build_session_items(session_msgs: list[dict]) -> list[SessionMessageItem]:
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
    await ws.send_json(SessionMessage(messages=items, token_stats=token_stats).wire_dict())


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
    # heartbeat=30s 定期发 ping 检测半开连接；receive_timeout=60s 关闭僵尸连接，
    # 防止休眠/断 Wi-Fi 客户端产生永久僵尸 channel（_send_json 每次广播打 warning、
    # STT monitor task 每 100ms 空转、连接无限累积）
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=30, receive_timeout=60)
    await ws.prepare(request)

    # 版本协商（批次 1）：握手头 X-Protocol-Version。缺省/1 → v1 路径（现状）；2 → v2 路径。
    proto_header = request.headers.get("X-Protocol-Version", "")
    protocol_version = 2 if proto_header == "2" else 1

    # 批次 4（spec §5.1 D-l）：X-Client-Platform 头 → WebSocketChannel.client_platform。
    # 仅 v2 连接解析——v1 无 desktop 概念，伪冒 platform 头在 v1 上不生效（负面隔离）。
    client_platform = ""
    if protocol_version >= 2:
        client_platform = request.headers.get("X-Client-Platform", "")

    from core.utils import new_message_id
    client_id = new_message_id()
    channel = WebSocketChannel(ws, protocol_version=protocol_version,
                               client_platform=client_platform)
    # 批次 8a（D2）：每 v2 连接分配稳定连接身份 conn_<hex> 存 channel.connection_id
    # （tool.status 路由据其校验发送连接是投递目标）；v1 无 desktop 概念恒 None。
    # electron 的该值即原 desktop_conn_id 语义（on_client_connect / server_info 沿用）。
    conn_id = f"conn_{uuid.uuid4().hex[:12]}" if protocol_version >= 2 else None
    channel.connection_id = conn_id
    agent.output.add_channel(channel)

    # 批次 4（spec §5.1）：v2 + electron 握手 → 桥标记 connected + 同步 desktop 可用性
    # （Task 6 集成：agent._sync_desktop_available 为组合根同步入口，跨模块私有访问豁免）。
    desktop_conn_id: str | None = conn_id if client_platform == "electron" else None
    if desktop_conn_id is not None:
        try:
            await agent.desktop_bridge.on_client_connect(desktop_conn_id)
            agent._sync_desktop_available()  # noqa: SLF001 — 组合根私有同步入口（Task 6）
            logger.info("WS desktop 客户端已连接: %s (%s)", client_id, desktop_conn_id)
        except Exception:
            logger.debug("Desktop bridge on_client_connect 失败", exc_info=True)

    # 发送会话历史给新连接的客户端
    # 批次 3：建连回放按 protocol_version resolve——v1 恒 default（active 切换不影响 v1），
    # v2 用当前活动会话。token_stats 也取目标会话（v1/v2 各自独立统计）。
    try:
        target = await agent.resolve_session(None, protocol_version)
        await _send_session_replay(ws, agent, target)
    except Exception:
        logger.warning("Failed to send session on connect", exc_info=True)

    # 发送服务能力状态（嵌套 per-service 形状：available/enabled/model/default）。
    # v2 客户端用 server_info（含 protocol_version/features）取代 capabilities，避免重复帧。
    # 批次 4（BE-042 附件持有者校验闭环）：v2 + electron 建连时把握手生成的 conn_id
    # 下发为 server_info.connectionId（Electron 据此构造附件上传 X-Client-Connection 头）；
    # 非 electron 的 desktop_conn_id 恒 None → wire 缺省省略（v1 类型字节不变）。
    try:
        if protocol_version >= 2:
            from core.input.capabilities import build_server_info
            await ws.send_json(
                build_server_info(agent, connection_id=desktop_conn_id).wire_dict())
        else:
            await ws.send_json(build_capabilities(agent).wire_dict())
    except Exception:
        logger.debug("Failed to send capabilities on connect", exc_info=True)

    # 发送当前心情（expression 事件）。心情持久化在 session.character_state，
    # 重启后服务端已恢复，但前端 `chat.mood` 只由 expression 事件更新——
    # 连接时不推送，心情标签要等下一轮 <mood> 才出现。复用前端现有 handler，零改动。
    # review 修复（兼容#5）：v1 恒读 default 会话的 mood（active 切走不影响 v1 心情），
    # v2 才读 active（character_engine.state 跟随 active）。
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

    # 重新广播挂起的 HITL 审批请求（前端重连后恢复审批弹窗）
    try:
        if hasattr(agent, 'tool_adapter') and agent.tool_adapter is not None:
            pending = agent.tool_adapter._manager.get_pending_approvals()  # noqa: SLF001
            for request_id, tool_name, params in pending:
                # 与正常路径（ToolManager._broadcast → websocket.on_command）格式一致。
                # 批次 4 重放版本门控（spec §7）：v2 连接收 enrich 版（+ summary 结构化
                # 参数摘要 + runtime/version/riskLevel/permissionRequired/approvalAuthority，
                # 复用 Task 5 ToolManager 的元数据 + _build_param_summary 逻辑，保持一致）；
                # v1 连接收原样 base——逐字节不变（兼容硬约束 §10.1）。
                base = {
                    "tool": tool_name,
                    "params": str(params)[:200],
                    "request_id": request_id,
                }
                if protocol_version >= 2:
                    manager = agent.tool_adapter._manager  # noqa: SLF001
                    plugin = manager._plugins.get(tool_name)  # noqa: SLF001
                    if plugin is not None:
                        enriched = dict(base)
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

    # 批次 6（BE-060~062）：每连接一个 VoiceSessionManager——无条件创建（放在
    # `if agent.stt is not None:` 块之外，否则 STT 为 None 时 voice_mgr 未定义，
    # BINARY 分支 NameError，review #10）。emit 直接走本连接 send_json。
    from core.input.voice_sessions import VoiceSessionManager
    voice_mgr = VoiceSessionManager(
        agent=agent, emit=lambda data: ws.send_json(data),
        on_vad_interrupt=lambda: do_interrupt(agent, skip_vad=True),
    )
    # 批次 8b（D1）：本连接已发消息 ID 集合（文本/附件 accepted + voice.commit +
    # REALTIME 段）——断连归属判定的数据源：仅当在途 generation 由本连接发起才取消，
    # 多标签/多设备下 Tab B 断开不误杀 Tab A 的生成。
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

        # 监视 FSM 状态：assistant 说话时 deafen 麦克风（回声抑制）
        # 只在上升沿 deafen 一次 2s 不够——长回复（5-30s 多句 TTS）后半段回声会越过
        # deafen 窗口触发 VAD interrupt，自我打断剩余回复。改为 SPEAKING 期间持续刷新。
        async def _monitor_state():
            while not ws.closed:
                try:
                    await asyncio.sleep(0.1)
                    if agent.status.is_speaking and audio_buf:
                        # 持续刷新 deafen——TTS 播放期间麦克风保持静音（回声被抑制）；
                        # is_speaking 变 False 后 deafen 自然过期，用户语音可正常触发打断
                        audio_buf.deafen(2.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("State monitor error", exc_info=True)
        monitor_task = asyncio.create_task(_monitor_state(), name=f"state-monitor-{client_id}")

    logger.info("WS client connected: %s (STT=%s)", client_id, "yes" if audio_buf else "no")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if not isinstance(data, dict):
                        raise ValidationError("WS 消息必须是 JSON 对象")
                    await handle_json_message(data, agent, audio_buf, ws, voice_mgr=voice_mgr,
                                              sent_message_ids=sent_message_ids)
                except json.JSONDecodeError:
                    await ws.send_json(ErrorMessage(
                        message="Invalid JSON",
                        code="CONTRACT_VALIDATION",
                        requestId=_current_request_id(),
                    ).wire_dict())
                except ValidationError as e:
                    logger.warning("WS 消息内容校验失败: %s", e)
                    await ws.send_json(ErrorMessage(
                        message=f"Invalid message content: {e}",
                        code="CONTRACT_VALIDATION",
                        requestId=_current_request_id(),
                    ).wire_dict())

            elif msg.type == WSMsgType.BINARY:
                # 批次 6：有活动语音会话 → 喂给会话管理器（RECORDING/REALTIME/DICTATION 分流）；
                # 批次 8b（D2）：v2 连接 BINARY 只在 voice_mgr 会话内有效——无会话 → 丢弃，
                # 不落 legacy audio_buf（否则 RECORDING 复位残留帧绕过 commit 自动成轮次）；
                # legacy VAD 自动提交仅 v1（现状逐字节不变）。voice_mgr 恒非 None，判空仅兜底。
                if voice_mgr is not None and voice_mgr.has_active_session():
                    await voice_mgr.feed_pcm(msg.data)
                elif protocol_version < 2 and audio_buf and agent.stt_enabled:
                    await audio_buf.feed(msg.data)

            elif msg.type == WSMsgType.ERROR:
                logger.warning("WS error: %s", ws.exception())

    except asyncio.CancelledError:
        pass
    finally:
        if monitor_task and not monitor_task.done():
            monitor_task.cancel()
        if audio_buf:
            await audio_buf.flush()
        if voice_mgr is not None:
            # D1 语音归属：REALTIME 段/commit 的 message_id 并入本连接已发集合
            # （_on_segment 已登记到 voice_mgr._emitted_message_ids，Task 6 覆盖）。
            sent_message_ids.update(voice_mgr.emitted_message_ids)
            await voice_mgr.close()
        agent.output.remove_channel(channel)
        # 批次 4（spec §5.1 D-o）：v2 + electron 连接断开 → 桥标 offline + 未接手 call
        # 立即 resolve UNAVAILABLE + 同步 desktop 可用性（ToolManager available 字段）。
        if desktop_conn_id is not None:
            try:
                await agent.desktop_bridge.on_client_disconnect(desktop_conn_id)
                agent._sync_desktop_available()  # noqa: SLF001 — 组合根私有同步入口（Task 6）
                logger.info("WS desktop 客户端已断开: %s (%s)", client_id, desktop_conn_id)
            except Exception:
                logger.debug("Desktop bridge on_client_disconnect 失败", exc_info=True)
        logger.info("WS client disconnected: %s", client_id)
        # 批次 2 重连语义（最小）+ 批次 8b（D1）：仅当在途 generation 由本连接发起才取消——
        # 多标签/多设备下 Tab B 断开不再误杀 Tab A 的生成。归属 = 当前 generation 的
        # user_message_id ∈ 本连接已发消息集合（文本/附件/语音 commit/REALTIME 段）。
        if protocol_version >= 2:
            try:
                gen_uid = getattr(getattr(agent, "cognitive", None),
                                  "current_generation_user_message_id", None)
                if gen_uid and gen_uid in sent_message_ids:
                    from core.monitoring.event_bus import EventPriority
                    agent.bus.post(InterruptData(source="disconnect"),
                                   priority=EventPriority.INTERRUPT)
            except Exception:
                logger.debug("v2 断线取消 generation 失败", exc_info=True)

    return ws


def _find_channel(agent: "Agent", ws: "web.WebSocketResponse | None") -> "WebSocketChannel | None":
    """按 ws 找到对应 WebSocketChannel（用于读取 protocol_version）。

    仿 switch_model 遍历 agent.output._channels；ws 为 None 或通道不可枚举时返回 None。
    """
    if ws is None:
        return None
    channels = getattr(agent.output, "_channels", None)
    if not isinstance(channels, (list, tuple)):
        return None
    for ch in channels:
        if isinstance(ch, WebSocketChannel) and getattr(ch, "_ws", None) is ws:
            return ch
    return None


def _is_v2(agent: "Agent", ws: "web.WebSocketResponse | None") -> bool:
    """三种语音模式消息 v2-only 门禁（仿 activate_session/tool.status）。"""
    ch = _find_channel(agent, ws)
    return ch is not None and ch.protocol_version >= 2


async def _send_voice_error(ws: "web.WebSocketResponse",
                            error: VoiceSessionError) -> None:
    """VoiceSessionError → error 信封。"""
    from frontend_contract import ErrorMessage
    try:
        await ws.send_json(ErrorMessage(
            message=error.message, code=error.code,
            requestId=_current_request_id(),
            retryable=getattr(error, "retryable", None),
        ).wire_dict())
    except Exception:
        logger.debug("发送 voice error 失败", exc_info=True)


async def _voice_stt_gate(agent: "Agent",
                          voice_mgr: "VoiceSessionManager | None") -> None:
    """会话 JSON 消息入口检查 stt_enabled（spec §5.3 NEW-D/NEW-W）。

    stt 关闭 → 结束活动 REALTIME/DICTATION 会话——暂停中会话无帧流入、idle 计时
    挂起，靠此入口检测避免静默悬挂至连接关闭；RECORDING 由 notify_stt_disabled
    内部忽略（spec §3.2 NEW-XA）。"""
    if (voice_mgr is not None and not getattr(agent, "stt_enabled", False)
            and voice_mgr.has_active_session()):
        try:
            await voice_mgr.notify_stt_disabled()
        except Exception:
            logger.debug("voice stt 门控 notify 失败", exc_info=True)


async def _send_ack(ws: "web.WebSocketResponse", message_id: str, client_request_id: str,
                    client_message_id: str | None, conversation_id: str | None,
                    status: str, error: dict | None = None) -> None:
    """发送 v2 用户消息确认。"""
    try:
        await ws.send_json(UserAckMessage(
            messageId=message_id, clientRequestId=client_request_id,
            clientMessageId=client_message_id, conversationId=conversation_id,
            receivedAt=now_iso(), status=status, error=error,
        ).wire_dict())
    except Exception:
        logger.debug("发送 user_message.ack 失败", exc_info=True)


# 附件 kind → Part 子类映射（spec §3.2 附件四类；未知 kind 跳过不构造）
_ATTACHMENT_PART_TYPES = {
    "image": ImagePart, "file": FilePart, "audio": AudioPart, "video": VideoPart,
}


def _attachment_part(kind: str, attachment_id: str, doc: dict):
    """附件记录 → 对应 Part（src=/api/attachments/{id} + attachment_id + 记录真值字段）。

    mime 以 attachments 记录为准（客户端提示值仅展示用，spec §3.1 Minor 10）；
    记录 mime 不在 Part 枚举时回退默认值（避免 pydantic 校验炸整条消息）。
    """
    cls = _ATTACHMENT_PART_TYPES.get(kind)
    if cls is None:
        logger.warning("未知附件 kind=%s，跳过构造 Part", kind)
        return None
    data: dict = {"src": f"/api/attachments/{attachment_id}", "attachment_id": attachment_id}
    mime = doc.get("mime") or ""
    if mime:
        try:
            cls(**{**data, "mime": mime})
            data["mime"] = mime
        except Exception:
            pass  # mime 非枚举值 → 用 Part 默认 mime
    if kind == "file":
        filename = doc.get("filename")
        if filename:
            data["filename"] = filename
        size = doc.get("size")
        if size is not None:
            try:
                data["size_bytes"] = int(size)
            except (TypeError, ValueError):
                pass
    return cls(**data)


def _build_media_parts(text: str, attachment_records: dict[str, dict],
                       sticker_id: str) -> list:
    """附件/表情消息的 Part 列表：TextPart + 附件 Part（按 kind）+ StickerPart。

    顺序：文本在前，附件按发送顺序，表情最后——喂给 LLM 前 content_to_openai
    会把 image 块移到 text 块之前（MIMO 要求），故此处仅保证 Part 齐全。
    """
    parts: list = []
    if text:
        parts.append(TextPart(content=text))
    for aid, doc in attachment_records.items():
        part = _attachment_part(doc.get("kind", ""), aid, doc)
        if part is not None:
            parts.append(part)
    if sticker_id:
        parts.append(StickerPart(sticker_id=sticker_id,
                                 src=f"/static/stickers/{sticker_id}.png"))
    return parts


async def _route_v2_text(agent: "Agent", ws: "web.WebSocketResponse | None",
                         text: str, data: dict,
                         sent_message_ids: set | None = None) -> bool:
    """v2 文本消息路由：ACK + 幂等 + 预分配 messageId（批次 7：附件/表情）。

    纯文本/附件：校验先于幂等 lookup、ACK 延后到 process 后——accepted 才 ACK +
    refs+1（media），队列满 rejected（QUEUE_FULL）并
    删幂等 entry 允许重试重新进入。批次 8b（§3.7）：纯文本/附件 process_message
    抛异常 → 删幂等 entry + rejected INTERNAL（不 crash 连接）；accepted 时
    ``sent_message_ids.add(message_id)``（D1 归属，Task 9 消费）。

    返回 True 表示已按 v2 处理（含 rejected），False 表示走 v1 路径（非 v2 连接）。

    Args:
        agent: Agent 实例。
        ws: 发送方连接（用于回 ACK 与协议版本判定）。
        text: 文本内容（已 strip）。
        data: 原始消息 dict（含 clientRequestId/clientMessageId/conversationId/
            attachments/stickerId）。
        sent_message_ids: 可选接收集合——accepted 时登记 messageId（D1 接线）。
    """
    if ws is None:
        return False
    channel = _find_channel(agent, ws)
    if channel is None or channel.protocol_version < 2:
        return False
    client_request_id = data.get("clientRequestId", "")
    client_message_id = data.get("clientMessageId")
    conversation_id = data.get("conversationId")

    if not client_request_id:
        # 缺 clientRequestId → 契约错误拒绝（spec §4.2）。拒绝无目标会话，conversationId 回显原始值。
        await _send_ack(ws, "", "", client_message_id, conversation_id, "rejected",
                        error={"code": "CONTRACT_VALIDATION",
                               "message": "缺少 clientRequestId", "retryable": False})
        return True

    # 批次 3：解析实际目标会话——ACK 的 conversationId 填目标会话
    # （v2 缺省 → 当前活动会话，显式 → 该会话），非仅回显原始字段。
    try:
        target = await agent.resolve_session(conversation_id, 2)
        ack_conversation_id = str(target.session_id)
    except Exception:
        logger.warning("_route_v2_text: resolve_session 失败，回退回显 conversationId", exc_info=True)
        ack_conversation_id = conversation_id

    attachments = data.get("attachments") or []
    sticker_id = data.get("stickerId") or ""
    has_media = bool(attachments) or bool(sticker_id)

    # 附件/表情消息：校验先于幂等 lookup（spec §4.2 扩展，批次 7）。
    # 纯文本保持原 digest 语义 sha256(text)；附件/表情摘要含附件 ID 排序 + stickerId。
    attachment_records: dict[str, dict] = {}
    if has_media:
        try:
            for att in attachments:
                aid = att.get("attachmentId") if isinstance(att, dict) \
                    else getattr(att, "attachmentId", "")
                if not aid:
                    await _send_ack(ws, "", client_request_id, client_message_id,
                                    ack_conversation_id, "rejected",
                                    error={"code": "CONTRACT_VALIDATION",
                                           "message": "attachments 缺 attachmentId",
                                           "retryable": False})
                    return True
                doc = await agent.attachment_repo.get(aid)
                if doc is None or (doc.get("status") or "") != "active":
                    await _send_ack(ws, "", client_request_id, client_message_id,
                                    ack_conversation_id, "rejected",
                                    error={"code": "ATTACHMENT_NOT_FOUND",
                                           "message": f"附件不存在或已删除: {aid}",
                                           "retryable": False})
                    return True
                attachment_records[aid] = doc
        except StorageUnavailableError:
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conversation_id, "rejected",
                            error={"code": "STORAGE_UNAVAILABLE",
                                   "message": "附件存储不可用（MongoDB 离线）",
                                   "retryable": True})
            return True
        if sticker_id and get_sticker(sticker_id) is None:
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conversation_id, "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": f"未知 stickerId: {sticker_id}",
                                   "retryable": False})
            return True
        ids = ",".join(sorted(attachment_records))
        digest = hashlib.sha256(
            text.encode("utf-8") + b"\x00" + ids.encode("utf-8")
            + b"\x00" + sticker_id.encode("utf-8")).hexdigest()
    else:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    store = get_store()
    entry = store.lookup(client_request_id)
    if entry is not None:
        if entry.content_digest == digest:
            # 幂等重放：重发原 ACK（同 messageId），不重复生成（spec §4.2）
            await _send_ack(ws, entry.user_message_id, client_request_id,
                            client_message_id, ack_conversation_id, "accepted")
        else:
            # 同 ID 不同内容 → 协议错误拒绝
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conversation_id, "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": "clientRequestId 冲突（内容摘要不同）",
                                   "retryable": False})
        return True

    # 新请求：预分配用户消息 ID → store → ACK → 触发生成
    from core.utils import new_message_id
    message_id = new_message_id()
    store.store(client_request_id,
                IdempotencyEntry(user_message_id=message_id,
                                 content_digest=digest, created_at=time.monotonic()))

    if has_media:
        # 附件/表情消息：ACK 延后到 process 后（需 process 结果判定 accepted / QUEUE_FULL）
        parts = _build_media_parts(text, attachment_records, sticker_id)
        content = [p.model_dump() for p in parts]
        try:
            accepted = await agent.process_message(
                text=text, content=content, message_id=message_id,
                protocol_version=2, conversation_id=conversation_id)
        except Exception:
            logger.warning("附件消息 process_message 抛异常——删幂等 entry", exc_info=True)
            store.delete(client_request_id)
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conversation_id, "rejected",
                            error={"code": "INTERNAL", "message": "消息处理失败",
                                   "retryable": False})
            return True
        if accepted:
            await _send_ack(ws, message_id, client_request_id, client_message_id,
                            ack_conversation_id, "accepted")
            if sent_message_ids is not None:
                sent_message_ids.add(message_id)   # D1 归属（media 路径同样登记）
            for aid in attachment_records:
                try:
                    await agent.attachment_repo.add_ref(aid, message_id)
                except Exception as e:
                    logger.warning("附件 refs 写盘失败 aid=%s message=%s: %s",
                                   aid, message_id, e)
        else:
            # 队列满 → 删幂等 entry（重试允许重新进入）+ rejected QUEUE_FULL；refs 不递增
            store.delete(client_request_id)
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conversation_id, "rejected",
                            error={"code": "QUEUE_FULL",
                                   "message": "Agent 忙，消息队列已满", "retryable": True})
        return True

    try:
        accepted = await agent.process_message(
            text=text, message_id=message_id, protocol_version=2,
            conversation_id=conversation_id)
    except Exception:
        logger.warning("process_message 抛异常——删幂等 entry，rejected", exc_info=True)
        store.delete(client_request_id)
        await _send_ack(ws, "", client_request_id, client_message_id,
                        ack_conversation_id, "rejected",
                        error={"code": "INTERNAL", "message": "消息处理失败",
                               "retryable": False})
        return True
    if accepted:
        await _send_ack(ws, message_id, client_request_id, client_message_id,
                        ack_conversation_id, "accepted")
        if sent_message_ids is not None:
            sent_message_ids.add(message_id)   # D1 归属
    else:
        # 队列满 → 删幂等 entry（重试允许重新进入）+ rejected QUEUE_FULL
        store.delete(client_request_id)
        await _send_ack(ws, "", client_request_id, client_message_id,
                        ack_conversation_id, "rejected",
                        error={"code": "QUEUE_FULL",
                               "message": "Agent 忙，消息队列已满", "retryable": True})
    return True


async def _handle_voice_commit(agent: "Agent", ws: "web.WebSocketResponse | None",
                               voice_mgr: "VoiceSessionManager | None", data: dict,
                               sent_message_ids: set | None = None) -> None:
    """voice.commit 处理顺序（spec §3.3 NEW-A/NEW-M/NEW-T）：
    ① 解析目标会话 ack_conv（幂等重放分支也用，对齐 _route_v2_text，review #15）
    ② 缺 clientRequestId → 直接 ack rejected（置于幂等与门禁之前）
    ③ 幂等表查重（digest = commit 携带 text）→ 命中重发原 ACK / 异摘要 rejected
    ④ 未命中 → 状态机 PENDING 门禁（commit_recording）→ ACK + post UserInputData。

    Args:
        sent_message_ids: D1 归属接收集合——accepted 时登记 messageId（Task 9 消费）。
    """
    recording_id = data.get("recordingId", "")
    text = data.get("text", "")
    client_request_id = data.get("clientRequestId", "")
    client_message_id = data.get("clientMessageId")
    conversation_id = data.get("conversationId")

    try:
        target = await agent.resolve_session(conversation_id, 2)
        ack_conv = str(target.session_id)
    except Exception:
        ack_conv = conversation_id

    if not client_request_id:
        await _send_ack(ws, "", "", client_message_id, ack_conv, "rejected",
                        error={"code": "CONTRACT_VALIDATION",
                               "message": "缺少 clientRequestId", "retryable": False})
        return

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    store = get_store()
    entry = store.lookup(client_request_id)
    if entry is not None:
        if entry.content_digest == digest:
            await _send_ack(ws, entry.user_message_id, client_request_id,
                            client_message_id, ack_conv, "accepted")
        else:
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conv, "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": "clientRequestId 冲突（内容摘要不同）",
                                   "retryable": False})
        return

    try:
        pending_text, emotion, events = voice_mgr.commit_recording(recording_id, text)
    except VoiceSessionError as e:
        await _send_ack(ws, "", client_request_id, client_message_id, ack_conv,
                        "rejected", error={"code": e.code, "message": e.message,
                                           "retryable": e.retryable})
        return

    from core.utils import new_message_id
    message_id = new_message_id()
    store.store(client_request_id, IdempotencyEntry(
        user_message_id=message_id, content_digest=digest, created_at=time.monotonic()))
    await _send_ack(ws, message_id, client_request_id, client_message_id, ack_conv, "accepted")
    if sent_message_ids is not None:
        sent_message_ids.add(message_id)   # D1 归属（voice.commit 同样登记）
    agent.idle.touch()
    # conversation_id 传原始值（None=活动会话），与 _route_v2_text 传原始给 process_message 一致；
    # ACK 侧已用解析后 ack_conv（review NEW-5）
    agent.bus.post(UserInputData(
        text=pending_text, user_name=agent.user_name, emotion=emotion, events=events,
        request_id=agent.next_request_id(), message_id=message_id,
        protocol_version=2, conversation_id=conversation_id))
    # BE-081 敏感纪律：只记 recordingId + 文本长度，不记正文
    logger.debug("voice.commit 提交成功: %s (len=%d)", recording_id, len(pending_text))


async def handle_json_message(data: dict, agent: "Agent", audio_buf: "AudioBuffer | None" = None, ws: "web.WebSocketResponse | None" = None, voice_mgr: "VoiceSessionManager | None" = None, sent_message_ids: set | None = None) -> None:
    """将来自 WebSocket 客户端的 JSON 消息路由到对应 handler。

    Args:
        data: 已解析的 JSON 消息 dict，含 "type" 键。
        agent: 处理消息与工具审批的 Agent 实例。
        audio_buf: 用于音频/唤醒词处理的 AudioBuffer 实例，可为 None。
        ws: 用于发送响应的 WebSocket 连接，可为 None。
        voice_mgr: 批次 6 每连接 VoiceSessionManager（三模式语音路由），可为 None。
        sent_message_ids: 批次 8b（§3.7）D1 接线——v2 text 消息 accepted 时登记
            messageId 的可选集合（透传给 _route_v2_text，Task 9 消费）。
    """
    msg_type = data.get("type", "")
    if msg_type == "wake_toggle":
        enabled = data.get("enabled", False)
        if audio_buf:
            audio_buf.set_wake_enabled(enabled)
            logger.debug("唤醒词开关: %s", "开启" if enabled else "关闭")
    elif msg_type == "text":
        text_content = data.get("content", "")
        if isinstance(text_content, str):
            if text_content.strip() or data.get("stickerId") or data.get("attachments"):
                # 批次 2 v2 路由（握手 protocol_version=2）：ACK + 幂等 + 预分配 messageId。
                # 批次 7：入口门放宽——sticker/附件-only 消息（content 可空）也进 v2 路由。
                if await _route_v2_text(agent, ws, text_content.strip(), data,
                                        sent_message_ids=sent_message_ids):
                    return
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
    elif msg_type == "interrupt":
        logger.debug("WS 收到打断指令")
        await do_interrupt(agent)
    elif msg_type == "switch_model":
        service = data.get("service", "")
        model = data.get("model", "")
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
    elif msg_type == "set_tts_voice":
        # 批次 5（Task 11，BE-051）：设置活动会话 TTS 声音覆盖。
        # 经活动会话激活 TTS 后端 list_voices() 校验 voice 存在 → 写活动会话
        # service_state.tts_voice + save → 回 set_tts_voice_result；未知 voice/
        # 后端不可用 → {ok: False, error}，不改 state（失败保留原选择）。
        voice = data.get("voice", "")
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
    elif msg_type == "set_service":
        service = data.get("service", "")
        enabled = data.get("enabled", False)
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
        # 批次 6（spec §3.2 NEW-X/XA）：stt 关闭成功 → 通知本连接语音会话（RECORDING 除外）
        if (service == "stt" and not enabled and result.get("ok")
                and voice_mgr is not None):
            try:
                await voice_mgr.notify_stt_disabled()
            except Exception:
                logger.debug("notify_stt_disabled 失败", exc_info=True)
    elif msg_type == "tool_approval":
        request_id = data.get("request_id", "")
        approved = data.get("approved", False)
        if request_id:
            ok = agent.tool_adapter._manager.submit_approval(request_id, approved)  # noqa: SLF001
            if ok:
                logger.info("WS 审批结果: request=%s approved=%s", request_id, approved)
            else:
                logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)
    elif msg_type == "activate_session":
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
    elif msg_type == "tool.status":
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
                await agent.desktop_bridge.handle_status(
                    data, sender_connection_id=channel.connection_id)
    elif msg_type == "voice.record.start":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice.record.start: 非 v2 或无 mgr，忽略")
        else:
            try:
                await voice_mgr.start_recording(data.get("recordingId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice.record.stop":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice.record.stop: 非 v2 或无 mgr，忽略")
        else:
            try:
                await voice_mgr.stop_recording(data.get("recordingId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice.commit":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice.commit: 非 v2 或无 mgr，忽略")
        else:
            await _handle_voice_commit(agent, ws, voice_mgr, data,
                                       sent_message_ids=sent_message_ids)
    elif msg_type == "voice.discard":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice.discard: 非 v2 或无 mgr，忽略")
        else:
            try:
                await voice_mgr.discard_recording(data.get("recordingId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.start":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.start: 非 v2，忽略")
        else:
            await _voice_stt_gate(agent, voice_mgr)
            try:
                await voice_mgr.start_realtime(
                    data.get("sessionId", ""), data.get("conversationId"))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.pause":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.pause: 非 v2，忽略")
        else:
            await _voice_stt_gate(agent, voice_mgr)
            try:
                await voice_mgr.pause_session(data.get("sessionId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.resume":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.resume: 非 v2，忽略")
        else:
            await _voice_stt_gate(agent, voice_mgr)
            try:
                await voice_mgr.resume_session(data.get("sessionId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.stop":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.stop: 非 v2，忽略")
        else:
            await _voice_stt_gate(agent, voice_mgr)
            try:
                await voice_mgr.stop_session(data.get("sessionId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.cancel":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.cancel: 非 v2，忽略")
        else:
            await _voice_stt_gate(agent, voice_mgr)
            try:
                await voice_mgr.cancel_session(data.get("sessionId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "dictation.start":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("dictation.start: 非 v2，忽略")
        else:
            await _voice_stt_gate(agent, voice_mgr)
            try:
                await voice_mgr.start_dictation(data.get("clientRequestId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "dictation.stop":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("dictation.stop: 非 v2，忽略")
        else:
            await _voice_stt_gate(agent, voice_mgr)
            try:
                await voice_mgr.stop_dictation(data.get("clientRequestId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "dictation.cancel":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("dictation.cancel: 非 v2，忽略")
        else:
            await _voice_stt_gate(agent, voice_mgr)
            try:
                await voice_mgr.cancel_dictation(data.get("clientRequestId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "audio.cancel":
        # 批次 6（BE-063）：取消在途 TTS 播放实例（全局播放操作，v2-only）。
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("audio.cancel: 非 v2，忽略")
        else:
            sid = data.get("stream_id")
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
        target = await agent.switch_session(sid)
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
