# Module/input/server.py 拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 1063 行的 `Module/input/server.py` 拆为 4 个文件（server.py + debug_routes.py + api_routes.py + ws_handler.py），纯提取，不改逻辑。

**Architecture:** 每个新文件导出一个 `register_xxx(app)` 函数，`create_app()` 依次调用。WS helper 去前导下划线为公开函数。handler 保持 `request.app["agent"]` 取依赖模式。

**Tech Stack:** Python 3.11+, aiohttp, Pydantic

## Global Constraints

- 不改任何业务逻辑——纯文件搬迁
- 不改 handler 签名（WS helper 仅去前导下划线）
- `Module/input/__init__.py` 重导出 `create_app` 不变
- `core/agent/server.py` 的 `from Module.input.server import create_app` 不变
- 每个 task 完成后 `Module/input/tests/test_server.py` 30 tests 必须全绿

---

### Task 1: 创建 `ws_handler.py` — 提取 WebSocket handler + 3 个辅助函数

**Files:**
- Create: `Module/input/ws_handler.py`
- Modify: `Module/input/server.py`（删除 L831-1064, 替换为 import + wrapper）

**Interfaces:**
- Consumes: （无依赖——第一个 task）
- Produces:
  - `handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled) -> WebSocketResponse`
  - `handle_json_message(data, agent, audio_buf, ws) -> None`
  - `do_interrupt(agent, skip_if) -> None`
  - `do_transcription(agent, ws, text, audio_buf, stt_emotion_state) -> None`
  - `send_wake(ws) -> None`

- [ ] **Step 1: 创建 `Module/input/ws_handler.py`**

新建文件，内容为 `server.py` 的 L831-L1064 完整复制，做以下修改：

1. 添加文件级 docstring
2. 函数重命名（去前导下划线）：
   - `_handle_ws` → `handle_ws`
   - `_handle_json` → `handle_json_message`
   - `_do_interrupt` → `do_interrupt`
   - `_do_transcription` → `do_transcription`
   - `_send_wake` → `send_wake`
3. 内部调用点更新：
   - `handle_ws` 内：`_do_interrupt` → `do_interrupt`，`_do_transcription` → `do_transcription`，`_send_wake` → `send_wake`
   - `handle_json_message` 内：`_do_interrupt` → `do_interrupt`

文件内容（完整）：

```python
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
```

- [ ] **Step 2: 修改 `Module/input/server.py`**

1. 删除 L831-L1064（`_handle_ws` 到 `_send_wake`）
2. 在文件顶部添加 import：

```python
from .ws_handler import handle_ws, handle_json_message, do_interrupt, do_transcription, send_wake as _send_wake
```

Wait — `_do_interrupt` 和 `_do_transcription` 在 `create_app()` 内部被 `AudioBuffer` 的 `on_interrupt`/`on_transcription` lambda 引用。现在这些 lambda 在 `handle_ws` 里（已提取到 ws_handler.py）。而 `server.py` 的 `ws_handler` wrapper（L57-58）调用 `handle_ws` 即可。

server.py 的 ws_handler 改为：

```python
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    return await handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled)
```

需要 import：
```python
from .ws_handler import handle_ws
```

不再需要在 server.py 中 import `handle_json_message`, `do_interrupt`, `do_transcription`, `send_wake`——它们只在 ws_handler.py 内部使用。

删除 server.py 中的：
- L6-L24 中不再需要的 imports：`asyncio`, `WSMsgType`, `pydantic.ValidationError`, `RunState`, `InterruptData`, `UserInputData`, `WebSocketChannel`, `AudioBuffer`（其中 `RunState` 仍被 health_handler 使用，保留。`WebSocketChannel` 被 switch_model_handler 的 HTTP 路由使用... 等等，switch_model_handler 已经是 HTTP 路由，不使用 WebSocketChannel。`WebSocketChannel` 只在 WS handler 中使用。所以可以删除。）

**重要：那些 core imports 要小心——只删 ws_handler.py 专用的，server.py 和 debug_routes.py 仍然需要的保留。**

当前 server.py 的 imports 和各自的使用者：
- `asyncio` — 只在 `_handle_ws` 使用 → **删除**
- `WSMsgType` — 只在 `_handle_ws` 使用 → **删除**
- `BodyPartReader` — 只在 `image_upload_handler` 使用（在 api_routes.py 范围）→ 本 task 不动
- `pydantic.ValidationError` — 只在 `_handle_ws` 使用 → **删除**
- `cast` — 只在 `image_upload_handler` 使用 → 本 task 不动
- `RunState` — 只在 `health_handler` 使用（在 server.py 保留）→ **保留**
- `Message`, `MessageRole` — debug 区域使用 → 本 task 不动
- `SessionId` — debug 区域 + session_detail_handler 使用 → 本 task 不动
- `MemoryType` — debug 区域使用 → 本 task 不动
- `InterruptData` — 只在 `_do_interrupt` 使用 → **删除**
- `UserInputData` — 只在 `_do_transcription` 使用 → **删除**
- `WebSocketChannel` — 只在 `_handle_ws` 和 `_handle_json` 使用（均在 ws_handler.py）→ **删除**
- `AudioBuffer` — 只在 `_handle_ws` 使用 → **删除**

删除这些 import line：
- `import asyncio`
- `from aiohttp import web, WSMsgType` → `from aiohttp import web`
- `from pydantic import ValidationError`
- `from core.agent import RunState` — 保留
- `from core.schema.event import InterruptData, UserInputData`
- `from Module.output import WebSocketChannel`
- `from .audio_buffer import AudioBuffer`

修改 `from aiohttp import web, WSMsgType` 为 `from aiohttp import web`

3. 删除 L831-L1064 全部内容，替换为 import wrapper：

```python
    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        return await handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled)
```

等等——当前 L57-58 已经有 ws_handler。提取后只需要把 import 从本地函数改为从 ws_handler import，wrapper 保持即可。实际操作：

替换 L57-58：
```python
    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        return await _handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled)
```

为：
```python
    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        return await handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled)
```

实际上最简单的操作：
1. 在文件顶部加 `from .ws_handler import handle_ws`
2. 将 L58 的 `_handle_ws` 改为 `handle_ws`
3. 删除 L831-L1064（`_handle_ws` 到 `_send_wake` 的函数体）
4. 清理不再需要的 imports

**Imports 清理细节**（逐一比对后）：

删除以下 import line：
```
import asyncio         # 仅 _handle_ws 使用
```

修改：
```
from aiohttp import web, WSMsgType  →  from aiohttp import web
```
（WSMsgType 仅 _handle_ws 使用）

删除：
```
from pydantic import ValidationError          # 仅 _handle_ws 使用
from core.schema.event import InterruptData, UserInputData  # 仅 WS helper 使用
from Module.output import WebSocketChannel    # 仅 WS handler 使用
from .audio_buffer import AudioBuffer         # 仅 _handle_ws 使用
```

（`from core.agent import RunState` 保留——health_handler 仍需要）

- [ ] **Step 3: 运行测试验证**

```bash
conda run -n Elysia python -m pytest Module/input/tests/test_server.py -v --tb=short
```

Expected: 30 passed

- [ ] **Step 4: Commit**

```bash
git add Module/input/ws_handler.py Module/input/server.py
git commit -m "refactor: extract ws_handler.py from server.py

Move handle_ws, handle_json_message, do_interrupt, do_transcription, send_wake
to Module/input/ws_handler.py. Rename: drop leading underscore for public API."
```

---

### Task 2: 创建 `api_routes.py` — 提取业务 API handler

**Files:**
- Create: `Module/input/api_routes.py`
- Modify: `Module/input/server.py`（删除 ~L447-L714 handler，替换为 `register_api_routes(app)` 调用）

**Interfaces:**
- Consumes: `app` (aiohttp Application with `app["agent"]` set)
- Produces: `register_api_routes(app) -> None`

- [ ] **Step 1: 创建 `Module/input/api_routes.py`**

包含以下 handler（从 `server.py` 提取，加 `register_api_routes` 包装函数）：

1. `providers_list_handler` + `providers_save_handler` + `providers_delete_handler` + `providers_test_handler`
2. `image_upload_handler`
3. `chat_api_handler`
4. `messages_handler`
5. `_archive_to_client` helper（`messages_handler` 依赖）
6. `image_upload_handler` 依赖 `BodyPartReader` + `cast`

文件内容（完整）：

```python
"""业务 API 路由 — Providers, 图片上传, Chat REST, 消息归档。"""

import logging
from pathlib import Path

from aiohttp import web
from aiohttp.multipart import BodyPartReader
from typing import cast

from core.schema.message import Message

logger = logging.getLogger(__name__)


# ---- 共享 helper（debug_routes.py 也需要） ----

def _archive_to_client(msgs: list[Message]) -> list[dict]:
    """将 Message 列表转为前端兼容格式（补 parts + _id 字段）。"""
    result = []
    for msg in msgs:
        entry = msg.model_dump(mode="json")
        mongo_id = entry.get("metadata", {}).pop("_mongo_id", None)
        if mongo_id:
            entry["_id"] = mongo_id
        content = entry.get("content", [])
        if isinstance(content, list):
            entry["parts"] = [
                {"content": p["content"]}
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
        elif isinstance(content, str):
            entry["parts"] = [{"content": content}]
        else:
            entry["parts"] = [{"content": ""}]
        result.append(entry)
    return result


def register_api_routes(app: web.Application) -> None:
    """注册所有业务 API 路由到 aiohttp Application。"""

    # ---- Provider 管理 ----

    async def providers_list_handler(request: web.Request) -> web.Response:
        """返回可用 provider 列表（注册表 + custom_providers.yaml）。"""
        from Module.input.custom_providers import load as load_custom

        llm_registry = {
            "deepseek": {"models": ["deepseek-v4-flash", "deepseek-v4-pro"]},
            "openai": {"models": ["gpt-4", "gpt-4o", "gpt-5"]},
            "ollama": {"models": ["qwen2.5:3b"]},
            "mimo": {"models": ["mimo-v2.5", "mimo-v2.5-pro"]},
            "gemini": {"models": []},
            "custom": {"models": []},
        }
        stt_registry = {
            "sensevoice": {"models": ["sense-voice"]},
            "custom": {"models": []},
        }
        tts_registry = {
            "gpt-sovits": {"models": ["default"]},
            "custom": {"models": []},
            "minimax": {"models": ["speech-2.8-hd"]},
        }

        custom_data = load_custom()
        for entry in custom_data.get("llm", []):
            llm_registry["custom"]["models"].append(entry["name"])
        for entry in custom_data.get("stt", []):
            stt_registry["custom"]["models"].append(entry["name"])
        for entry in custom_data.get("tts", []):
            tts_registry["custom"]["models"].append(entry["name"])

        return web.json_response({
            "llm": llm_registry,
            "stt": stt_registry,
            "tts": tts_registry,
            "custom": custom_data,
        })

    async def providers_save_handler(request: web.Request) -> web.Response:
        from Module.input.custom_providers import add_provider
        try:
            body = await request.json()
            service = body.get("service", "")
            entry = body.get("entry", {})
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)
        if not entry.get("name") or not entry.get("base_url"):
            return web.json_response({"ok": False, "error": "缺少 name 或 base_url"}, status=400)
        try:
            data = add_provider(service, entry)
            return web.json_response({"ok": True, "data": data})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def providers_delete_handler(request: web.Request) -> web.Response:
        from Module.input.custom_providers import remove_provider
        service = request.match_info.get("service", "")
        name = request.match_info.get("name", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)
        try:
            data = remove_provider(service, name)
            return web.json_response({"ok": True, "data": data})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def providers_test_handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            service = body.get("service", "")
            base_url = body.get("base_url", "")
            api_key = body.get("api_key", "")
            model = body.get("model", "")
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)

        try:
            if service == "llm":
                from Module.llm import LLMFactory
                backend = LLMFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            elif service == "stt":
                from Module.stt import STTFactory
                backend = STTFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            elif service == "tts":
                from Module.tts import TTSFactory
                backend = TTSFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            else:
                return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)

            result = await backend.test()
            try:
                await backend.close()
            except Exception:
                pass
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    app.router.add_get("/api/providers", providers_list_handler)
    app.router.add_post("/api/providers", providers_save_handler)
    app.router.add_delete("/api/providers/{service}/{name}", providers_delete_handler)
    app.router.add_post("/api/providers/test", providers_test_handler)

    # ---- 图片上传 ----

    _IMAGES_DIR = Path(__file__).parent.parent.parent / "data" / "uploads" / "images"

    async def image_upload_handler(request: web.Request) -> web.Response:
        import uuid as _uuid
        import os as _os

        agent = request.app["agent"]
        cfg = getattr(agent._config, "server", None)
        mb = getattr(cfg, "image_max_mb", None)
        max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
        allowed = getattr(cfg, "image_allowed_types", None) or ["image/png", "image/jpeg", "image/gif", "image/webp"]

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return web.json_response({"error": "未找到图片字段"}, status=400)

            data = await cast(BodyPartReader, field).read()
            if len(data) > max_bytes:
                return web.json_response({"error": f"图片过大，上限 {max_bytes // 1024 // 1024}MB"}, status=413)

            filename = getattr(field, "filename", "") or "image.png"
            ext = _os.path.splitext(filename)[1].lower()
            ext_to_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                           ".gif": "image/gif", ".webp": "image/webp"}
            mime = ext_to_mime.get(ext, "image/png")

            if mime not in allowed:
                return web.json_response({"error": f"不支持的图片类型: {mime}"}, status=415)

            _os.makedirs(str(_IMAGES_DIR), exist_ok=True)
            saved_name = f"{_uuid.uuid4().hex[:12]}{ext}"
            saved_path = _IMAGES_DIR / saved_name
            saved_path.write_bytes(data)

            url = f"/uploads/images/{saved_name}"
            return web.json_response({"url": url, "mime": mime, "size": len(data)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    app.router.add_post("/api/images", image_upload_handler)

    # ---- Chat API（curl 测试用） ----

    async def chat_api_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        try:
            body = await request.json()
            content = body.get("content")
            user_name = body.get("user_name", agent._user_name)
            text = body.get("text", "")
        except Exception:
            return web.json_response({"ok": False, "error": "无效的 JSON 请求体"}, status=400)

        if not content and not text:
            return web.json_response({"ok": False, "error": "缺少 content 或 text"}, status=400)

        await agent.process_message(text=text, user_name=user_name, content=content)
        return web.json_response({"ok": True})

    app.router.add_post("/api/chat", chat_api_handler)

    # ---- 消息归档 API ----

    async def messages_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        repo = agent._message_repo

        before = request.query.get("before", "").strip() or None
        try:
            limit = int(request.query.get("limit", "50"))
            limit = max(1, min(limit, 200))
        except ValueError:
            limit = 50

        try:
            messages, has_more = await repo.query(before=before, limit=limit)
            total = await repo.count()
            client_msgs = _archive_to_client(messages)
            return web.json_response({
                "messages": client_msgs,
                "has_more": has_more,
                "total": total,
            })
        except Exception as e:
            logger.warning("Failed to query message archive: %s", e)
            return web.json_response(
                {"messages": [], "has_more": False, "total": 0, "error": str(e)},
                status=500,
            )

    app.router.add_get("/api/messages", messages_handler)
```

- [ ] **Step 2: 修改 `Module/input/server.py`**

1. 在文件顶部添加 import：
```python
from .api_routes import register_api_routes, _archive_to_client
```

（`_archive_to_client` 是过渡期 import——`archive_list_handler` 在 Task 3 移走前仍需要它。Task 3 完成后可从 server.py 移除该 import，因为 debug_routes.py 自己 import。）

2. 在 `create_app()` 中，`app.router.add_get("/ws", ws_handler)` 之后添加：
```python
register_api_routes(app)
```

3. 删除原 L447-L655（从 `# ---- 模型切换 API ----` 到 `_archive_to_client` 函数结束）。**注意**：L657-L684（`_sessions_to_client`）保留在 server.py 中——它将在 Task 3 中移至 debug_routes.py。

4. 清理不再需要的 imports：
   - 删除：`from aiohttp.multipart import BodyPartReader`, `from typing import cast`（仅 image_upload 使用，已移走）
   - 保留：`from core.schema.message import Message, MessageRole`（debug 区域 + `_sessions_to_client` 仍需）

   修改后 import block 为：
   ```python
   import json
   import logging
   from pathlib import Path

   from aiohttp import web

   from core.agent import RunState
   from core.schema.message import Message, MessageRole
   from core.schema.types import SessionId
   from core.schema.memory import MemoryType

   from .ws_handler import handle_ws
   from .api_routes import register_api_routes, _archive_to_client
   ```

- [ ] **Step 3: 运行测试验证**

```bash
conda run -n Elysia python -m pytest Module/input/tests/test_server.py -v --tb=short
```

Expected: 30 passed

- [ ] **Step 4: Commit**

```bash
git add Module/input/api_routes.py Module/input/server.py
git commit -m "refactor: extract api_routes.py from server.py

Move providers, image upload, chat API, message archive handlers
to Module/input/api_routes.py with register_api_routes(app)."
```

---

### Task 3: 创建 `debug_routes.py` — 提取 Debug API handler

**Files:**
- Create: `Module/input/debug_routes.py`
- Modify: `Module/input/server.py`（删除 L172-L446 + L716-L828 handler，替换为 `register_debug_routes(app)` 调用）

**Interfaces:**
- Consumes: `app` (aiohttp Application with `app["agent"]` and optionally `app["task_system"]`)
- Produces: `register_debug_routes(app) -> None`

- [ ] **Step 1: 创建 `Module/input/debug_routes.py`**

**提取方式：从 `server.py` 原样复制以下行范围，包装到 `register_debug_routes(app)` 函数体内。**

文件头部（新建）：

```python
"""Debug API 路由 — 数据库状态, LTM CRUD, Session 管理, 消息归档, LLM 录制。"""

import json
import logging
import os
from pathlib import Path

from aiohttp import web

from core.schema.message import Message, MessageRole
from core.schema.types import SessionId
from core.schema.memory import MemoryType
from .api_routes import _archive_to_client

logger = logging.getLogger(__name__)


def register_debug_routes(app: web.Application) -> None:
    """注册所有 /debug/api/* 路由到 aiohttp Application。"""
```

然后按顺序从 `server.py` 复制以下代码段到 `register_debug_routes()` 函数体内：

| 复制 server.py 行范围 | 内容 | 修改 |
|----------------------|------|------|
| L88-L100 | `debug_api_handler` + TaskDebugAPI 注册 | `from core.debug.info import collect, asdict` 移到文件顶部 |
| L172-L193 | `db_status_handler` + 路由注册 | 不变 |
| L195-L229 | `ltm_list_handler` + 路由注册 | 不变 |
| L231-L245 | `ltm_insert_handler` + 路由注册 | 不变（含 `await`、`entry_id`） |
| L247-L254 | `ltm_delete_single_handler` + 路由注册 | 不变 |
| L256-L267 | `ltm_update_handler` + 路由注册 | 不变 |
| L269-L273 | `ltm_clear_handler` + 路由注册 | 不变 |
| L277-L305 | `archive_list_handler` + 路由注册 | **把 `_archive_to_client(messages)` 的调用保留——函数在 api_routes.py 中已导入** |
| L307-L315 | `archive_delete_handler` + 路由注册 | 不变 |
| L317-L329 | `archive_update_handler` + 路由注册 | 不变 |
| L331-L337 | `archive_clear_handler` + 路由注册 | 不变 |
| L339-L378 | `sessions_list_handler` + 路由注册 | 不变（含 `token_totals` 聚合逻辑） |
| L380-L427 | `session_detail_handler` + 路由注册 | 不变（`_get_character_state` 保持为内部嵌套函数） |
| L429-L444 | `session_delete_handler` + 路由注册 | 不变 |
| L657-L684 | `_sessions_to_client` 函数 | 原样复制到 `register_debug_routes()` 内（在 session handlers 之前） |
| L716-L719 | `_UPLOADS_DIR` + 静态路由注册 | **整个移到 `server.py` 的 `create_app()` 中，不放在这里** |
| L721-L728 | `prompts_list_handler` + `app.router.add_get` | 不变 |
| L730-L765 | `prompt_detail_handler` + `app.router.add_get` | 不变 |
| L767-L779 | `sessions_clear_handler` + `app.router.add_delete` | 不变（L780-781 移到各 handler 旁） |
| L783-L789 | `responses_list_handler` + `app.router.add_get` | 不变 |
| L791-L812 | `response_detail_handler` + 两个 `app.router.add_get` | 不变 |
| L814-L826 | `on_startup` / `on_shutdown` + `app.on_startup.append` / `app.on_shutdown.append` | 不变 |

**关键注意事项：**
- **所有 handler 原样复制，不改参数名、不省 `await`、不改方法名。** 唯一的改动是文件头 import 整理。
- `_archive_to_client` 已经在 Task 2 中提取到 `api_routes.py`，`archive_list_handler` 仍通过 `from .api_routes import _archive_to_client` 使用它。
- `_sessions_to_client`（L633-L655）复制到 `register_debug_routes()` 内部，放在 session handlers 之前。
- `_get_character_state`（L387-L392）保持嵌套在 `session_detail_handler` 内部不变。
- `_UPLOADS_DIR` + `/uploads/` 静态路由（L716-L719）**不放到 debug_routes.py**——它属于 `server.py` 的 `create_app()`。

- [ ] **Step 2: 修改 `Module/input/server.py`**

1. 在文件顶部添加 import：
```python
from .debug_routes import register_debug_routes
```

2. 在 `create_app()` 中，`register_api_routes(app)` 之后添加：
```python
register_debug_routes(app)
```

3. 删除以下代码段：
   - L88-L100（debug_api_handler + TaskDebugAPI 注册）
   - L172-L446（数据库管理 + 消息归档管理）
   - L657-L684（`_sessions_to_client` — 已移到 debug_routes.py）
   - L716-L828（sessions_clear, prompts, responses, on_startup/on_shutdown）

   **保留** `return app`（L828）

4. 清理不再需要的 imports。Task 2 的 `_archive_to_client` 过渡 import 现在可移除（debug_routes.py 自己 import）。server.py 最终 import block 为：

```python
"""WebSocket server — bridges browser clients to the agent pipeline.

Each client gets an independent WebSocketChannel (outbound) and AudioBuffer
(inbound audio). Text and STT results are routed to the agent's EventBus.
"""

import json
import logging
from pathlib import Path

from aiohttp import web

from core.agent import RunState

from .ws_handler import handle_ws
from .api_routes import register_api_routes
from .debug_routes import register_debug_routes

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent.parent / "static"
_LIVE2D_DIR = Path(__file__).parent.parent.parent / "data" / "live2d-assets"
```

5. `create_app()` 剩余内容为：基础路由（index, debug, health, shutdown, restart, tools）+ 三个 register 调用 + static dirs：

```python
def create_app(
    agent,
    stt_transcribe=None,
    serve_static: bool = True,
    stt_emotion_state: dict | None = None,
    wake_word_enabled: bool = False,
    task_system=None,
) -> web.Application:
    app = web.Application()

    app["agent"] = agent
    if task_system is not None:
        app["task_system"] = task_system

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        return await handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled)

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

        debug_path = _STATIC_DIR / "debug.html"
        if debug_path.is_file():
            async def debug_handler(_request: web.Request) -> web.FileResponse:
                return web.FileResponse(debug_path)
            app.router.add_get("/debug", debug_handler)

    # Health check endpoint — 保留原始实现不变
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

    # Shutdown endpoint — 保留原始实现不变
    async def shutdown_handler(request: web.Request) -> web.Response:
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Shutdown requested via HTTP")
        request.app["agent"]._state = RunState.STOPPING
        return web.json_response({"status": "shutting_down"})
    app.router.add_post("/shutdown", shutdown_handler)
    app.router.add_get("/shutdown", shutdown_handler)

    # Restart endpoint — 保留原始实现不变
    async def restart_handler(request: web.Request) -> web.Response:
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Restart requested via HTTP")
        request.app["agent"]._state = RunState.RESTARTING
        return web.json_response({"status": "restarting"})
    app.router.add_post("/restart", restart_handler)

    # Tools endpoint
    async def tools_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        return web.json_response({
            "plugins": agent._tool_manager.plugin_list,
        })
    app.router.add_get("/tools", tools_handler)

    # Tool reload endpoint
    async def tool_reload_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        try:
            body = await request.json()
            name = body.get("name", "")
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)
        if not name:
            return web.json_response({"ok": False, "error": "缺少插件名称"}, status=400)
        ok = await agent._tool_manager.reload_plugin(name)
        if ok:
            return web.json_response({"ok": True, "name": name})
        else:
            return web.json_response({"ok": False, "name": name, "error": f"插件 '{name}' 重载失败"})
    app.router.add_post("/tools/reload", tool_reload_handler)

    # 子模块路由
    register_api_routes(app)
    register_debug_routes(app)

    # 用户上传文件目录
    _UPLOADS_DIR = Path(__file__).parent.parent.parent / "data" / "uploads"
    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/", str(_UPLOADS_DIR), show_index=False)

    return app
```

server.py 最终 import block：
```python
import json
import logging
from pathlib import Path

from aiohttp import web

from core.agent import RunState

from .ws_handler import handle_ws
from .api_routes import register_api_routes
from .debug_routes import register_debug_routes
```

注意：`asyncio` 不再被 server.py 直接使用（已随 `_handle_ws` 移至 ws_handler.py），不需要 import。

- [ ] **Step 3: 运行测试验证**

```bash
conda run -n Elysia python -m pytest Module/input/tests/test_server.py -v --tb=short
```

Expected: 30 passed

- [ ] **Step 4: Commit**

```bash
git add Module/input/debug_routes.py Module/input/server.py
git commit -m "refactor: extract debug_routes.py from server.py

Move all /debug/api/* handlers (18 total) plus on_startup/on_shutdown
to Module/input/debug_routes.py with register_debug_routes(app)."
```

---

### Task 4: 更新测试 import 路径

**Files:**
- Modify: `Module/input/tests/test_server.py`
- Modify: `core/tests/test_phase3.py`（4 处 create_app import）

**Interfaces:**
- Consumes: ws_handler.py, api_routes.py, debug_routes.py 的新函数名
- Produces: 不变——测试断言不变

- [ ] **Step 1: 更新 `Module/input/tests/test_server.py`**

测试中直接从 server 模块 import 内部函数的地方需要改为从新模块 import：

| 旧 import | 新 import |
|-----------|-----------|
| `from Module.input.server import _do_interrupt` | `from Module.input.ws_handler import do_interrupt` |
| `from Module.input.server import _do_transcription` | `from Module.input.ws_handler import do_transcription` |
| `from Module.input.server import _handle_json` | `from Module.input.ws_handler import handle_json_message` |
| `from Module.input.server import create_app` | 不变 |
| `from Module.input.server import create_app, _do_transcription` | `from Module.input.server import create_app` + `from Module.input.ws_handler import do_transcription` |

同时更新测试函数中的调用名：
- `_do_interrupt(agent)` → `do_interrupt(agent)`
- `_do_transcription(agent, ws, text)` → `do_transcription(agent, ws, text)`
- `_handle_json(data, agent, audio_buf, ws)` → `handle_json_message(data, agent, audio_buf, ws)`

- [ ] **Step 2: 检查 `core/tests/test_phase3.py`**

只 import `create_app`，不需要改。

```bash
grep -n "from Module.input.server" core/tests/test_phase3.py
```

Expected: 仅 `from Module.input.server import create_app`（4 处），无需修改。

- [ ] **Step 3: 运行全量 Module/input 测试**

```bash
conda run -n Elysia python -m pytest Module/input/tests/ -v --tb=short
```

Expected: 所有测试通过

- [ ] **Step 4: 运行关联测试**

```bash
conda run -n Elysia python -m pytest core/tests/test_phase3.py -v --tb=short -k "server"
```

- [ ] **Step 5: Commit**

```bash
git add Module/input/tests/test_server.py
git commit -m "test: update import paths after server.py split

_do_interrupt → ws_handler.do_interrupt
_do_transcription → ws_handler.do_transcription
_handle_json → ws_handler.handle_json_message"
```

---

### Task 5: 更新文档

**Files:**
- Modify: `CLAUDE.md`
- Modify: `Module/input/README.md`
- Modify: `Module/README.md`
- Modify: `docs/testing.md`
- Modify: `docs/architecture.md`

- [ ] **Step 1: 更新 `Module/input/README.md`**

文件树从：
```
├── server.py            # WebSocket 服务器
```
改为：
```
├── server.py            # 胶水组装 + 基础路由
├── ws_handler.py        # WebSocket 处理
├── api_routes.py        # 业务 API 路由
├── debug_routes.py      # Debug API 路由
```

测试命令更新（如有）：
```bash
python -m pytest Module/input/tests/ -v
```

- [ ] **Step 2: 更新 `CLAUDE.md` 和 `docs/testing.md`**

确认 `Module/input/tests/` 路径正确（Task 4 应该已更新）。

在 CLAUDE.md 的 Module index 中添加：
```
| `Module.input` | Input server (WebSocket + REST) | `server.py`, `ws_handler.py`, `api_routes.py`, `debug_routes.py` |
```

- [ ] **Step 3: Commit**

```bash
git add Module/input/README.md CLAUDE.md Module/README.md docs/testing.md docs/architecture.md
git commit -m "docs: update references after server.py split"
```

---

### Task 6: 最终全量验证

- [ ] **Step 1: 运行全量 Module 测试**

```bash
conda run -n Elysia python -m pytest Module/ -q --tb=short
```

Expected: 与拆分前一致（db 相关的预存 errors 除外）

- [ ] **Step 2: 运行 make test-quick**

```bash
conda run -n Elysia make test-quick
```

Expected: 全绿

- [ ] **Step 3: 确认文件行数**

```bash
wc -l Module/input/server.py Module/input/ws_handler.py Module/input/api_routes.py Module/input/debug_routes.py
```

Expected: server.py ~150 行, ws_handler.py ~230 行, api_routes.py ~300 行, debug_routes.py ~400 行
