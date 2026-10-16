"""WebSocket handler —— WS 生命周期、消息路由、打断、转写。"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web, WSMsgType
from pydantic import TypeAdapter, ValidationError

from core.schema.event import InterruptData, UserInputData
from core.schema.message import ClientMessageDict
from Module.output import WebSocketChannel
from core.input.audio_buffer import AudioBuffer
from core.input._common import _find_channel, _send_ack
from core.services.capabilities import build_capabilities
from core.input.ws_handlers import _WS_HANDLERS
from core.input.ws_handlers.context import WsRequestContext
# re-export（评估 §5 批次 D 第 4 条「旧 import re-export」）：这些符号已迁入
# core/input/ws_handlers/{chat,session}.py，但既有测试与同包模块仍从本模块导入，
# 故在此保留导出以维持兼容。新增代码请直接从领域模块导入。
from core.input.ws_handlers.chat import do_interrupt
from core.input.ws_handlers.voice import (  # noqa: F401 — re-export 兼容层
    _is_v2,
    _send_voice_error,
    _voice_descriptor,
    _voice_stt_gate,
)
from core.network.rate_limiter import RateLimiter, rate_key_for_request
# 重导出（2026-09-17 Batch 2.3 恢复）：`_handle_activate_session` 由
# `core.input.ws_handlers.session` 拥有，但 `core/input/tests/test_request_id.py`
# 按 `from core.input.ws_handler import _handle_activate_session` 取用。
# F401 清理曾删掉它 → ImportError。显式别名表态「有意重导出」。
from core.input.ws_handlers.session import (
    _handle_activate_session as _handle_activate_session,
)
from core.input.voice_sessions import VoiceSessionManager
from core.utils import get_request_id, set_request_id
from frontend_contract import (
    AckError,
    CommandMessage,
    ErrorMessage,
    ExpressionMessage,
    SessionMessage,
    SessionMessageItem,
    TokenStats,
    TranscriptionMessage,
    WakeMessage,
)
from frontend_contract.ws_message import (
    ClientMessage,
)
from frontend_contract.version import TRANSPORT_VERSION

if TYPE_CHECKING:
    from core.agent import Agent
    from core.memory.session import SessionMemory

logger = logging.getLogger(__name__)

# ClientMessage 判别联合的校验适配器——提为模块级单例（Pydantic 官方建议，同
# core/memory/consolidator.py 的 _compare_actions_adapter 先例）。原先是每条 v2 入站
# 消息现场构造 TypeAdapter(ClientMessage)，判别联合较大，重复构造纯属浪费。
_CLIENT_MESSAGE_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


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


async def _emit_state_snapshot(agent: "Agent", ws: "web.WebSocketResponse") -> None:
    """发当前活动会话 instance 的产品态快照(character.state.changed)。

    取活动会话的 runtime → make_public_state → CharacterStateChangedMessage。
    建连时强制发射(客户端刚连上需要初始态);失败静默(不影响握手)。
    """
    try:
        from core.character.public_state import make_public_state
        from frontend_contract.ws_message import CharacterStateChangedMessage
        registry = getattr(agent, "character_runtime_registry", None)
        if registry is None:
            return
        # 当前活动会话的 binding → instance_id → runtime
        binding = getattr(agent.session_manager.active, "character_binding", None) or {}
        instance_id = binding.get("instance_id") or "chi_builtin_elysia"
        runtime = registry.get_existing(instance_id)
        if runtime is None:
            return
        view = make_public_state(runtime)
        msg = CharacterStateChangedMessage(
            conversationId=str(agent.session.session_id),
            characterId=runtime.definition.characterId,
            characterInstanceId=runtime.instance.instanceId,
            characterRevision=runtime.revision.revision,
            stateVersion=runtime.stateVersion,
            state=view,
        )
        await ws.send_json(msg.wire_dict())
    except Exception:
        logger.debug("emit state snapshot 失败", exc_info=True)


#: 连接收尾时留给 v1 尾段转写的预算（秒）。远小于 `STT_TIMEOUT=30.0`——
#: 那个是**单次调用**上限，不是"连接收尾可接受阻塞时间"（S2-E3）。
_CLOSE_FLUSH_BUDGET_S = 2.0


def _ws_response_kwargs() -> dict:
    """WS 握手参数（提取为函数：测试直接调用它，避免复制参数导致漂移）。

    S2-E4：**显式 `compress=False`**。不传它时 aiohttp 默认 `compress=True`，会与
    请求压缩的客户端协商 `permessage-deflate`；实测该组合在**空闲约 75s 后的第一帧**
    上被判 `Received frame with non-zero reserved bits` 并以关闭码 1002 断连
    （存档：docs/review/archive/2026-08/2026-08-14-Windows-Electron-第三次真实联调结果.md
    的 A/B：默认压缩 → 1002，客户端 `perMessageDeflate:false` → 1000）。

    若那一帧是 `text`，用户消息**从未到达后端**，前端只会看到 ACK 超时；连接被 1002
    关闭重连时，语音会话与 desktop 注册全部重建。此前只在 Electron 侧规避，后端纵深
    防护一直没落地——而**浏览器 WebSocket API 无法关闭该扩展**，所以必须在服务端关。
    不影响 wire 内容（只是不再压缩）。
    """
    return {
        "max_msg_size": 16 * 1024 * 1024,
        "heartbeat": 30,
        "receive_timeout": 60,
        "compress": False,
    }


async def _open_ws_connection(
    request: web.Request,
    agent: "Agent",
    wake_word_enabled: bool,
) -> _WsConnection:
    """握手、初始帧回放、voice/audio 装配——连接级 setup 全部收口在此。"""
    ws = web.WebSocketResponse(**_ws_response_kwargs())
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
            # 阶段 G(§7.1):v2 + character_state_stream=true 时发角色状态快照(强制,不看 last)
            if getattr(agent, "character_state_stream", False):
                await _emit_state_snapshot(agent, ws)
        else:
            await ws.send_json(build_capabilities(agent).wire_dict())
    except Exception:
        # **error 而非 debug**（2026-09-27）：server_info / capabilities 是协议
        # 必须帧（docs/electron-backend-protocol-v2.md「v2 连接建连帧」），发不出去
        # 等于客户端这一整个连接的能力协商失败——不可在本连接内恢复，必须上报。
        # 历史缺陷：此处原为 logger.debug，于是「帧缺失」在日志里完全不可见，
        # 只在别处表现为客户端等帧超时（详见 core/services/capabilities.py 模块 docstring）。
        logger.error("Failed to send %s on connect（协议必须帧缺失）",
                     "server_info" if protocol_version >= 2 else "capabilities",
                     exc_info=True)

    try:
        # Task 12（§6.5 读端迁移）：v1/v2 统一读当前 instance 的 engine.state（实例权威）。
        # 此前 v2 读 agent.character_engine.state、v1 读 default_session.character_state
        # （非权威——M3 后 state 权威在 runtime.state，session.character_state 不再被写）。
        # agent.character_engine 经 registry 解析当前会话绑定 instance 的 engine（Task 11）。
        cs = agent.character_engine.state
        expression_identity = {}
        if protocol_version >= 2:
            registry = getattr(agent, "character_runtime_registry", None)
            binding = getattr(agent.session_manager.active, "character_binding", None) or {}
            instance_id = binding.get("instance_id") or "chi_builtin_elysia"
            runtime = registry.get_existing(instance_id) if registry is not None else None
            if runtime is not None:
                expression_identity = {
                    "conversationId": str(agent.session.session_id),
                    "characterId": runtime.definition.characterId,
                    "characterInstanceId": runtime.instance.instanceId,
                    "characterRevision": runtime.revision.revision,
                }
        await ws.send_json(ExpressionMessage(
            expression=cs.mood, intensity=cs.mood_intensity,
            **expression_identity,
        ).wire_dict())
    except Exception:
        logger.debug("Failed to send mood on connect", exc_info=True)

    try:
        if hasattr(agent, 'tool_adapter') and agent.tool_adapter is not None:
            adapter = agent.tool_adapter
            for request_id, tool_name, params in adapter.get_pending_approvals():
                # payload 组装（含 v1/v2 富化差异）收口在工具子系统门面——
                # 传输层只负责发送（批次 A / ARCH-INC-P2-04）
                payload = adapter.build_approval_payload(
                    request_id, tool_name, params,
                    enrich=protocol_version >= 2,
                )
                await ws.send_json(CommandMessage(
                    command="tool_approval",
                    params=payload,
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
        # S3-06：**每帧入口重置关联 ID**。ContextVar 是任务级、跨帧保留：上一条命令
        # （如 `voice.commit`）设置的 "012" 会一直留着，于是之后某条**与本请求无关**的
        # 错误帧会带上那个 id——客户端按它归因到一条**已成功**的旧请求，比省略更糟。
        # "-" 是既有默认值（无请求上下文时 `_current_request_id()` 本来就返回 None），
        # 故本改动不动 wire 契约。
        set_request_id("-")
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
    """连接清理：monitor/audio/voice 关闭、channel 移除、desktop 下线、断线取消。

    S2-E1（2026-09-12）：**纯注册表清理 `remove_channel` 必须在 `finally` 里**。
    此前它排在 `audio_buf.flush()` / `voice_mgr.close()` 两个可失败 await **之后**
    且全程无 `try/finally`——任一次抛错就让该连接**永久留在**
    `agent.output.channels`；此后每次广播都对这条死连接 `send_json`
    （`Module/output/websocket.py` 每帧一条 warning + 完整 traceback，日志随消息量
    线性爆炸），desktop 状态也永久失真，v2 断线取消 generation 一并失效。
    """
    if conn.monitor_task and not conn.monitor_task.done():
        conn.monitor_task.cancel()
    # 纯内存更新，不可能失败——放在可失败块之前，保证断线取消用的集合完整
    if conn.voice_mgr is not None:
        conn.sent_message_ids.update(conn.voice_mgr.emitted_message_ids)

    try:
        if conn.audio_buf:
            # S2-E3③：**收尾预算远小于单次 STT 上限**。`flush()` 内部是
            # `wait_for(stt_transcribe, 30.0)`——30s 是"单次调用"上限，不是
            # "连接收尾可接受阻塞时间"的预算。收尾路径必须快速让出：超预算即放弃
            # 尾段（与今天超时丢段的结果一致，但不再拖住停止按钮与停机）。
            try:
                await asyncio.wait_for(
                    conn.audio_buf.flush(), timeout=_CLOSE_FLUSH_BUDGET_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "WS 连接关闭：尾段转写超过 %.0fs 预算，放弃该段（不阻塞收尾）",
                    _CLOSE_FLUSH_BUDGET_S)
                # S2-E2：放弃尾段时**显式释放缓冲**——否则那几十 MB PCM 会一直挂在
                # AudioBuffer 上直到对象被 GC（连接对象可能被引用得更久）。
                # **只在放弃路径释放**：放在 `finally` 里会在取消路径上执行额外语句，
                # 一旦对象没有该方法就抛 AttributeError，把最初的 CancelledError
                # **顶掉**（实测把 E1 的"取消也不得泄漏 channel"测试打红）。
                conn.audio_buf.release()
        if conn.voice_mgr is not None:
            await conn.voice_mgr.close()
    except Exception:
        logger.warning("WS 连接关闭：音频/语音清理失败——仍继续注册表清理",
                       exc_info=True)
    finally:
        agent.output.remove_channel(conn.channel)

    # 以下两段各自已有 try/except，互不阻断
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
        # S2-E3：**不再吞掉取消**。原先这里是 `pass`，取消被吃掉后仍继续执行
        # `finally` 里的清理，而清理的第一个 await 就是 `audio_buf.flush()`——
        # 它内部 `wait_for(stt_transcribe, 30.0)`，于是"停止按钮/停机"最长 30s
        # 才走到 `remove_channel`；停机时每个持有音频段的连接把进程退出各拖长 ≤30s，
        # 期间 `_ws_active` 不减 → 新连接按 CONNECTION_LIMITED 503 被拒。
        # 清理仍由 finally 负责（那里另有收尾预算，见 `_close_ws_connection`）。
        logger.info("WS 消息循环被取消，交由 finally 收尾: %s", conn.client_id)
        raise
    finally:
        await _close_ws_connection(conn, agent)
    return conn.ws

































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
                msg = _CLIENT_MESSAGE_ADAPTER.validate_python(data)
            except ValidationError as e:
                if ws is not None:  # _find_channel 命中即 ws 非空，此处仅满足 pyright 收紧
                    await ws.send_json(ErrorMessage(
                        message=f"Invalid message: {e}", code="CONTRACT_VALIDATION",
                        requestId=_current_request_id()).wire_dict())
                return
    handler = _WS_HANDLERS.get(msg_type)
    if handler is not None:
        # ARCH-INC-P2-01（批次 D）：handler 签名收口为 (ctx, data, msg, channel)——
        # 连接级状态打包进 WsRequestContext，新增消息类型不再扩参数面。
        await handler(
            WsRequestContext(
                agent=agent, ws=ws, audio_buf=audio_buf, voice_mgr=voice_mgr,
                sent_message_ids=sent_message_ids, limiter=limiter, rate_key=rate_key,
            ),
            data, msg, channel,
        )
    else:
        logger.debug("WS 未知消息类型: %s", msg_type)






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
    # KB-48 / RO1（Batch 14 的残留缺口）：**准入判定必须覆盖 STT 路径**。
    # 本函数是直接 `bus.post(UserInputData)`，不经过 `process_message`；而它跑在
    # aiohttp 的 WS 请求任务上——`Agent.stop()` **不**取消这些任务（`runner.cleanup()`
    # 在 `agent.stop()` 之后才跑）。于是窗口是：
    #
    #   stop() 最后一步 RO1 清扫 `discard_user_inputs()`  →  stop() 返回
    #     → 在途 `do_transcription`（STT 单段预算 ≤30s）才投递
    #
    # 投进去的事件会**跨 epoch 复活**：同一实例 `start()` 后消费者把它当新输入消费，
    # 进历史。判据与 `process_message` / REALTIME 语音段同源
    # （`status.is_service_running`，生命周期轴），不另设 flag。
    #
    # 这里**拒收而不排队**（与 `process_message` 一致：停机窗口没有"稍后再处理"）。
    # 回显（上面那句）仍然发——客户端需要清掉自己的录音态；被拒的是**入队**这一步。
    if not agent.status.is_service_running:
        logger.warning(
            "STT 转写提交被拒：服务未在运行（停机窗口）——该段不入队，不跨 epoch 复活 "
            "rid=%s len=%d", rid, len(text))
        return
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
