"""WebSocket handler — WS lifecycle, message routing, interrupt, transcription."""

import asyncio
import json
import logging

from aiohttp import web, WSMsgType
from pydantic import ValidationError

from core.agent import RunState
from core.schema.event import InterruptData, UserInputData
from Module.output import WebSocketChannel
from .audio_buffer import AudioBuffer

logger = logging.getLogger(__name__)


async def handle_ws(
    request: web.Request,
    agent,
    stt_transcribe,
    stt_emotion_state: dict | None = None,
    wake_word_enabled: bool = False,
) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    from core.message_id import new_message_id
    client_id = new_message_id()
    channel = WebSocketChannel(ws)
    agent._output.add_channel(channel)

    # 发送会话历史给新连接的客户端
    try:
        session_msgs = agent._session.messages_for_client()
        token_stats = {}
        if hasattr(agent, '_token_tracker') and agent._token_tracker is not None:
            token_stats = await agent._token_tracker.get_session_stats()
        await ws.send_json({"type": "session", "messages": session_msgs, "token_stats": token_stats})
    except Exception:
        logger.warning("Failed to send session on connect", exc_info=True)

    # 发送服务能力状态（STT / TTS 是否可用）
    try:
        await ws.send_json({
            "type": "capabilities",
            "stt": stt_transcribe is not None,
            "tts": agent._tts is not None,
            "llm_model": agent._llm.model,
            "live2d_static_image": agent._config.live2d.static_image,
        })
    except Exception:
        pass

    # 重新广播挂起的 HITL 审批请求（前端重连后恢复审批弹窗）
    try:
        if hasattr(agent, '_tool_manager') and agent._tool_manager is not None:
            pending = agent._tool_manager.get_pending_approvals()
            for request_id, tool_name, params in pending:
                await ws.send_json({
                    "command": "tool_approval",
                    "params": {
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "params": params,
                    },
                })
                logger.debug("Re-broadcast pending approval to new client: %s (%s)",
                             request_id, tool_name)
    except Exception:
        logger.debug("Failed to re-send pending approvals", exc_info=True)

    audio_buf: AudioBuffer | None = None
    monitor_task: asyncio.Task | None = None
    if stt_transcribe:
        wake_words = None
        if wake_word_enabled:
            try:
                wake_words = agent.get_wake_words()
            except Exception:
                logger.debug("Could not read wake_words from agent", exc_info=True)

        audio_buf = AudioBuffer(
            stt_transcribe=stt_transcribe,
            aggressiveness=3,
            on_interrupt=lambda: do_interrupt(agent, skip_if={"thinking", "executing"}),
            on_transcription=lambda text: do_transcription(
                agent, ws, text, audio_buf, stt_emotion_state,
            ),
            wake_words=wake_words,
            on_wake_notify=lambda: send_wake(ws),
        )

        # Monitor FSM state to deafen mic when assistant is speaking (echo prevention)
        async def _monitor_state():
            prev_state = None
            while not ws.closed:
                try:
                    await asyncio.sleep(0.1)
                    current = agent._fsm.state
                    if current != prev_state and current == "speaking" and audio_buf:
                        audio_buf.deafen(2.0)
                    prev_state = current
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
                    await handle_json_message(data, agent, audio_buf, ws)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
                except ValidationError as e:
                    logger.warning("WS 消息内容校验失败: %s", e)
                    await ws.send_json({"type": "error", "message": f"Invalid message content: {e}"})

            elif msg.type == WSMsgType.BINARY:
                if audio_buf:
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
        agent._output.remove_channel(channel)
        logger.info("WS client disconnected: %s", client_id)

    return ws


async def handle_json_message(data: dict, agent, audio_buf=None, ws=None) -> None:
    msg_type = data.get("type", "")
    if msg_type == "wake_toggle":
        enabled = data.get("enabled", False)
        if audio_buf:
            audio_buf.set_wake_enabled(enabled)
            logger.info("唤醒词开关: %s", "开启" if enabled else "关闭")
    elif msg_type == "text":
        text_content = data.get("content", "")
        if isinstance(text_content, list):
            from core.schema.adapters.message_adapter import content_text, parts_from_dicts
            await agent.process_message(text=content_text(parts_from_dicts(text_content)), user_name=agent._user_name, content=text_content)
            logger.info("WS 收到多模态消息")
        elif isinstance(text_content, str) and text_content.strip():
            await agent.process_message(text=text_content.strip())
            logger.info("WS 收到文本: %s", text_content.strip()[:100])
    elif msg_type == "interrupt":
        logger.info("WS 收到打断指令")
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
            for ch in agent._output._channels:
                if isinstance(ch, WebSocketChannel) and ch._ws is ws:
                    try:
                        await ws.send_json({
                            "type": "switch_model_result",
                            "service": service,
                            "model": model,
                            **result,
                        })
                    except Exception:
                        pass
                    break
        logger.info("WS 切换模型: service=%s model=%s ok=%s", service, model, result.get("ok"))
    elif msg_type == "tool_approval":
        request_id = data.get("request_id", "")
        approved = data.get("approved", False)
        if request_id:
            ok = agent._tool_manager.submit_approval(request_id, approved)
            if ok:
                logger.info("WS 审批结果: request=%s approved=%s", request_id, approved)
            else:
                logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)
    else:
        logger.debug("WS 未知消息类型: %s", msg_type)


async def do_interrupt(agent, skip_if: set | None = None) -> None:
    """Post INTERRUPT event. skip_if 可阻止特定状态下的 VAD 误触发。"""
    if skip_if and agent._fsm.state in skip_if:
        return
    from core.event_bus import EventPriority
    agent._bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)


async def do_transcription(agent, ws, text: str, audio_buf=None,
                           stt_emotion_state: dict | None = None) -> None:
    # 从 STT emotion state 读取情绪和事件
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
        pass

    if audio_buf:
        audio_buf.deafen(2.0)
    rid = agent.next_request_id()
    logger.info("STT 识别: %s%s", text[:100], f" [{emotion}]" if emotion and emotion != "NEUTRAL" else "")
    agent._bus.post(UserInputData(
        text=text, user_name=agent._user_name,
        emotion=emotion, events=events,
        request_id=rid,
    ))


async def send_wake(ws) -> None:
    """Notify frontend that a wake word was detected."""
    try:
        await ws.send_json({"type": "wake"})
    except Exception:
        pass
