"""WebSocket handler — WS lifecycle, message routing, interrupt, transcription."""

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web, WSMsgType
from pydantic import ValidationError

from core.schema.event import InterruptData, UserInputData
from Module.output import WebSocketChannel
from core.input.audio_buffer import AudioBuffer
from core.input.capabilities import build_capabilities

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


async def handle_ws(
    request: web.Request,
    agent: "Agent",
    wake_word_enabled: bool = False,
) -> web.WebSocketResponse:
    """Handle an incoming WebSocket connection for a browser client.

    Manages the full WS lifecycle: connection, session history replay,
    capability announcement, audio buffer setup, message routing, and cleanup.

    STT 从 create_app 参数注入改为读 ``agent.stt`` 槽位：建连时
    ``agent.stt is not None`` 才建 audio_buf，转写/情绪经 ``agent.stt_transcribe``
    / ``agent.stt_emotion_state`` 读取（支持热切换）。

    Args:
        request: aiohttp Request initiating the WebSocket upgrade.
        agent: Agent instance providing output channels and message processing.
        wake_word_enabled: Enable wake word detection for this connection.

    Returns:
        web.WebSocketResponse: The WebSocket connection object after close.
    """
    # heartbeat=30s 定期发 ping 检测半开连接；receive_timeout=60s 关闭僵尸连接，
    # 防止休眠/断 Wi-Fi 客户端产生永久僵尸 channel（_send_json 每次广播打 warning、
    # STT monitor task 每 100ms 空转、连接无限累积）
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=30, receive_timeout=60)
    await ws.prepare(request)

    from core.utils import new_message_id
    client_id = new_message_id()
    channel = WebSocketChannel(ws)
    agent.output.add_channel(channel)

    # 发送会话历史给新连接的客户端
    try:
        session_msgs = agent.session.messages_for_client()
        token_stats = {}
        if hasattr(agent, 'token_tracker') and agent.token_tracker is not None:
            token_stats = await agent.token_tracker.get_session_stats()
        await ws.send_json({"type": "session", "messages": session_msgs, "token_stats": token_stats})
    except Exception:
        logger.warning("Failed to send session on connect", exc_info=True)

    # 发送服务能力状态（嵌套 per-service 形状：available/enabled/model/default）。
    # model 供前端回填设置面板下拉选中值——否则首次访问（无 localStorage）
    # 下拉框显示"选择模型..."占位符，即使服务端已在运行某个模型。
    try:
        await ws.send_json(build_capabilities(agent))
    except Exception:
        logger.debug("Failed to send capabilities on connect", exc_info=True)

    # 发送当前心情（expression 事件）。心情持久化在 session.character_state，
    # 重启后服务端已恢复，但前端 `chat.mood` 只由 expression 事件更新——
    # 连接时不推送，心情标签要等下一轮 <mood> 才出现。复用前端现有 handler，零改动。
    try:
        cs = agent.character_engine.state
        await ws.send_json({
            "type": "expression",
            "expression": cs.mood,
            "intensity": cs.mood_intensity,
        })
    except Exception:
        logger.debug("Failed to send mood on connect", exc_info=True)

    # 重新广播挂起的 HITL 审批请求（前端重连后恢复审批弹窗）
    try:
        if hasattr(agent, 'tool_adapter') and agent.tool_adapter is not None:
            pending = agent.tool_adapter._manager.get_pending_approvals()  # noqa: SLF001
            for request_id, tool_name, params in pending:
                # 与正常路径（ToolManager._broadcast → websocket.on_command）格式一致
                await ws.send_json({
                    "type": "command",
                    "command": "tool_approval",
                    "params": {
                        "tool": tool_name,
                        "params": str(params)[:200],
                        "request_id": request_id,
                    },
                })
                logger.debug("Re-broadcast pending approval to new client: %s (%s)",
                             request_id, tool_name)
    except Exception:
        logger.debug("Failed to re-send pending approvals", exc_info=True)

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

        # Monitor FSM state to deafen mic when assistant is speaking (echo prevention)
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
                    await handle_json_message(data, agent, audio_buf, ws)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
                except ValidationError as e:
                    logger.warning("WS 消息内容校验失败: %s", e)
                    await ws.send_json({"type": "error", "message": f"Invalid message content: {e}"})

            elif msg.type == WSMsgType.BINARY:
                # 后端防御：STT 被禁用（agent.stt_enabled=False）时不喂音频，
                # 防止关闭开关后仍触发 VAD/转写
                if audio_buf and agent.stt_enabled:
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
        agent.output.remove_channel(channel)
        logger.info("WS client disconnected: %s", client_id)

    return ws


async def handle_json_message(data: dict, agent: "Agent", audio_buf: "AudioBuffer | None" = None, ws: "web.WebSocketResponse | None" = None) -> None:
    """Route a JSON message from the WebSocket client to the appropriate handler.

    Args:
        data: Parsed JSON message dict with a "type" key.
        agent: Agent instance for processing messages and tool approvals.
        audio_buf: AudioBuffer instance for audio/wake-word handling, or None.
        ws: WebSocket connection for sending responses, or None.
    """
    msg_type = data.get("type", "")
    if msg_type == "wake_toggle":
        enabled = data.get("enabled", False)
        if audio_buf:
            audio_buf.set_wake_enabled(enabled)
            logger.debug("唤醒词开关: %s", "开启" if enabled else "关闭")
    elif msg_type == "text":
        text_content = data.get("content", "")
        if isinstance(text_content, list):
            from core.schema.adapters.message_adapter import content_text, parts_from_dicts
            await agent.process_message(text=content_text(parts_from_dicts(text_content)), user_name=agent.user_name, content=text_content)
            logger.debug("WS 收到多模态消息")
        elif isinstance(text_content, str) and text_content.strip():
            await agent.process_message(text=text_content.strip())
            logger.debug("WS 收到文本: %s", text_content.strip()[:100])
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
                        await ws.send_json({
                            "type": "switch_model_result",
                            "service": service,
                            "model": model,
                            **result,
                        })
                    except Exception:
                        logger.debug("Failed to send switch_model_result", exc_info=True)
                    break
        logger.info("WS 切换模型: service=%s model=%s ok=%s", service, model, result.get("ok"))
    elif msg_type == "set_service":
        service = data.get("service", "")
        enabled = data.get("enabled", False)
        result = await agent.set_service(service, enabled)
        # 广播给所有 WebSocket 客户端（而非仅发送者）——含失败时的 enabled 字段。
        # 不复制 switch_model_result 模式（那只回给请求方）。capabilities 重播在 Task 7 接入。
        from Module.output import WebSocketChannel
        for ch in agent.output._channels:  # noqa: SLF001
            if isinstance(ch, WebSocketChannel):
                await ch._send_json({  # noqa: SLF001
                    "type": "service_mode_result",
                    "service": service,
                    "enabled": result.get("enabled", enabled),
                    "ok": result.get("ok", False),
                    "error": result.get("error"),
                })
        logger.info("WS 设置服务开关: service=%s enabled=%s ok=%s", service, enabled, result.get("ok"))
    elif msg_type == "tool_approval":
        request_id = data.get("request_id", "")
        approved = data.get("approved", False)
        if request_id:
            ok = agent.tool_adapter._manager.submit_approval(request_id, approved)  # noqa: SLF001
            if ok:
                logger.info("WS 审批结果: request=%s approved=%s", request_id, approved)
            else:
                logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)
    else:
        logger.debug("WS 未知消息类型: %s", msg_type)


async def do_interrupt(agent: "Agent", skip_vad: bool = False) -> None:
    """Post INTERRUPT event.

    Args:
        agent: Agent instance.
        skip_vad: If True, suppress during THINKING/EXECUTING (VAD false-positive).
                  Button-press interrupts should pass skip_vad=False (default).
    """
    if skip_vad and agent.status.should_suppress_vad_interrupt:
        return
    from core.monitoring.event_bus import EventPriority
    agent.bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)


async def do_transcription(agent: "Agent", ws: "web.WebSocketResponse", text: str,
                           audio_buf: "AudioBuffer | None" = None,
                           stt_emotion_state: dict | None = None) -> None:
    """Handle an STT transcription result: echo to client, deafen mic, post event.

    Args:
        agent: Agent instance for output and event posting.
        ws: WebSocket connection to echo transcription back to client.
        text: Transcribed text from STT.
        audio_buf: AudioBuffer to deafen during TTS playback, or None.
        stt_emotion_state: Optional dict with 'emotion' and 'events' from STT.
    """
    emotion = ""
    events: list[str] = []
    if stt_emotion_state:
        emotion = stt_emotion_state.get("emotion", "")
        events = stt_emotion_state.get("events", [])

    # Send transcription back to client so it can display the user's speech
    try:
        msg: dict = {"type": "transcription", "text": text}
        if emotion:
            msg["emotion"] = emotion
        if events:
            msg["events"] = events
        await ws.send_json(msg)
    except Exception:
        logger.debug("Failed to send transcription echo", exc_info=True)

    if audio_buf:
        audio_buf.deafen(2.0)
    rid = agent.next_request_id()
    logger.debug("STT 识别: %s%s", text[:100], f" [{emotion}]" if emotion and emotion != "NEUTRAL" else "")
    # 语音路径绕过 process_message，需手动 touch——否则纯语音对话
    # 会被误判为"用户沉默"，触发 silence_timeout 主动搭话
    agent.idle.touch()
    agent.bus.post(UserInputData(
        text=text, user_name=agent.user_name,
        emotion=emotion, events=events,
        request_id=rid,
    ))


async def send_wake(ws: "web.WebSocketResponse") -> None:
    """Notify frontend that a wake word was detected.

    Args:
        ws: WebSocket connection to send the notification to.
    """
    try:
        await ws.send_json({"type": "wake"})
    except Exception:
        logger.debug("Failed to send wake notification", exc_info=True)
