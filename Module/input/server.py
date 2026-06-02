"""WebSocket server — bridges browser clients to the agent pipeline.

Each client gets an independent WebSocketChannel (outbound) and AudioBuffer
(inbound audio). Text and STT results are routed to the agent's EventBus.
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path

from aiohttp import web, WSMsgType

from Module.output import WebSocketChannel
from .audio_buffer import AudioBuffer

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent.parent / "static"
_LIVE2D_DIR = Path(__file__).parent.parent.parent / "live2d"


def create_app(
    agent,
    stt_transcribe=None,
    serve_static: bool = True,
    stt_emotion_state: dict | None = None,
) -> web.Application:
    """Create an aiohttp Application with WebSocket handler and optional static files.

    Args:
        agent: Agent instance (needs _output, _bus, process_message())
        stt_transcribe: async callable(bytes) -> str for STT (optional)
        serve_static: serve static/index.html at /
        stt_emotion_state: mutable dict with 'emotion' and 'events' keys,
            updated by the STT wrapper after each transcription.
    """
    app = web.Application()

    # Inject agent + stt into app context for handler access
    app["agent"] = agent

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        return await _handle_ws(request, agent, stt_transcribe, stt_emotion_state)

    app.router.add_get("/ws", ws_handler)

    if serve_static and _STATIC_DIR.is_dir():
        app.router.add_static("/static/", _STATIC_DIR, show_index=True)

    if _LIVE2D_DIR.is_dir():
        app.router.add_static("/live2d/", _LIVE2D_DIR, show_index=False)

    # Serve index.html at /
    index_path = _STATIC_DIR / "index.html"
    if index_path.is_file():
        async def index_handler(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(index_path)
        app.router.add_get("/", index_handler)

        # Debug page + API
        debug_path = _STATIC_DIR / "debug.html"
        if debug_path.is_file():
            async def debug_handler(_request: web.Request) -> web.FileResponse:
                return web.FileResponse(debug_path)
            app.router.add_get("/debug", debug_handler)

        async def debug_api_handler(request: web.Request) -> web.Response:
            from core.debug_info import collect, asdict
            info = await collect(request.app["agent"])
            return web.json_response(asdict(info))
        app.router.add_get("/debug/api", debug_api_handler)

    # Health check endpoint — for Docker/Kubernetes probes
    async def health_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        try:
            fsm_state = agent._fsm.state
        except Exception:
            fsm_state = "unknown"
        return web.json_response({
            "status": "ok",
            "fsm": fsm_state,
            "llm": getattr(agent._llm, 'model', 'unknown'),
            "memory": {
                "session_count": len(getattr(agent._session, '_messages', [])),
                "long_term_count": getattr(agent._long_term, 'count', 0),
            },
            "tools": getattr(agent._tool_manager, 'tool_names', ["<loading>"]),
        })
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", health_handler)

    # Shutdown endpoint — triggers graceful agent stop
    async def shutdown_handler(request: web.Request) -> web.Response:
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Shutdown requested via HTTP")
        request.app["agent"]._running = False
        return web.json_response({"status": "shutting_down"})
    app.router.add_post("/shutdown", shutdown_handler)
    app.router.add_get("/shutdown", shutdown_handler)  # 也接受浏览器直接访问

    return app


async def _handle_ws(
    request: web.Request,
    agent,
    stt_transcribe,
    stt_emotion_state: dict | None = None,
) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    client_id = uuid.uuid4().hex[:8]
    channel = WebSocketChannel(ws)
    agent._output.add_channel(channel)

    audio_buf: AudioBuffer | None = None
    monitor_task: asyncio.Task | None = None
    if stt_transcribe:
        audio_buf = AudioBuffer(
            stt_transcribe=stt_transcribe,
            aggressiveness=3,
            on_interrupt=lambda: _do_interrupt(agent, skip_if={"thinking", "executing"}),
            on_transcription=lambda text: _do_transcription(
                agent, ws, text, audio_buf, stt_emotion_state,
            ),
        )

        # Monitor FSM state to deafen mic when assistant is speaking (echo prevention)
        async def _monitor_state():
            prev_state = None
            while not ws.closed:
                try:
                    await asyncio.sleep(0.1)
                    current = agent._fsm.state
                    if current != prev_state and current == "speaking" and audio_buf:
                        audio_buf.deafen(2.0)  # Ignore mic for 2s while TTS plays
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
                    await _handle_json(data, agent)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})

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


async def _handle_json(data: dict, agent) -> None:
    msg_type = data.get("type", "")
    if msg_type == "text":
        content = data.get("content", "").strip()
        if content:
            await agent.process_message(content)
            logger.info("WS 收到文本: %s", content[:100])
    elif msg_type == "interrupt":
        logger.info("WS 收到打断指令")
        await _do_interrupt(agent)
    elif msg_type == "tool_approval":
        request_id = data.get("request_id", "")
        approved = data.get("approved", False)
        if request_id:
            ok = agent._tool_manager.submit_approval(request_id, approved)
            if ok:
                logger.info(
                    "WS 审批结果: request=%s approved=%s", request_id, approved,
                )
            else:
                logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)
    else:
        logger.debug("WS 未知消息类型: %s", msg_type)


async def _do_interrupt(agent, skip_if: set | None = None) -> None:
    """Post INTERRUPT event. skip_if 可阻止特定状态下的 VAD 误触发。"""
    if skip_if and agent._fsm.state in skip_if:
        return
    from core.event_bus import EventType, EventPriority
    agent._bus.post(EventType.INTERRUPT, {"source": "websocket"}, priority=EventPriority.INTERRUPT)


async def _do_transcription(agent, ws, text: str, audio_buf=None,
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
    from core.event_bus import EventType
    agent._bus.post(EventType.USER_INPUT, {
        "text": text, "user_name": "妖梦",
        "emotion": emotion, "events": events,
        "request_id": rid,
    })
