# Module/input/server.py 拆分设计

> 将 1063 行的 `server.py` 按职责拆为 4 个文件，每个文件导出一个 `register_xxx(app)` 函数，由 `create_app()` 依次调用。

## 背景

`server.py` 当前结构：`create_app()` 工厂函数（L32-828）内部定义了 30+ 个闭包 handler，外加 5 个模块级 async helper（`_handle_ws` / `_handle_json` / `_do_interrupt` / `_do_transcription` / `_send_wake`）。

所有 handler 已经使用 `request.app["agent"]` 取依赖，天然可提取为独立函数。

## 拆分方案

```
Module/input/
├── server.py          (~150 行)  胶水组装 + 基础路由
├── debug_routes.py    (~400 行)  /debug/api/*  全部 handler
├── api_routes.py      (~300 行)  业务 API + 模型切换 + 图片上传 + Chat
├── ws_handler.py      (~230 行)  WebSocket 处理 + 打断/转录/wake 辅助
├── __init__.py        (不变)
├── custom_providers.py (不变)
├── audio_buffer.py    (不变)
└── wakeword.py        (不变)
```

### 文件职责

#### `server.py` — 胶水组装 (~150 行)

- 常量：`_STATIC_DIR`, `_LIVE2D_DIR`
- `create_app(agent, stt_transcribe, ...)` — 工厂入口
- 基础路由（简单到不需要独立文件）：
  - `/` → index.html
  - `/debug` → debug.html
  - `/health` → 健康检查（依赖 `RunState`）
  - `/shutdown` → 优雅关闭
  - `/restart` → 热重启
  - `/tools` → 工具列表
  - `/tools/reload` → 热重载插件
- 调用 3 个 `register_xxx(app)` 完成路由挂载
- `on_startup` / `on_shutdown`（TaskDebugAPI 生命周期）

#### `debug_routes.py` — Debug API (~400 行)

- `register_debug_routes(app)` — 注册所有 `/debug/api/*` 路由
- Handler（18 个）：
  - DB 管理：`db_status`, `ltm_list`, `ltm_insert`, `ltm_delete_single`, `ltm_update`, `ltm_clear`
  - 消息归档：`archive_list`, `archive_delete`, `archive_update`, `archive_clear`
  - Session 管理：`sessions_list`, `session_detail`, `session_delete`, `sessions_clear`
  - LLM 录制：`prompts_list`, `prompt_detail`, `responses_list`, `response_detail`

#### `api_routes.py` — 业务 API (~300 行)

- `register_api_routes(app)` — 注册所有业务 API 路由
- Handler（7 个）：
  - Provider 管理：`providers_list`, `providers_save`, `providers_delete`, `providers_test`
  - 图片上传：`image_upload_handler`
  - Chat REST：`chat_api_handler`
  - 消息归档：`messages_handler`
  - 用户上传文件目录注册（`serve_uploaded_dir`）

#### `ws_handler.py` — WebSocket 处理 (~230 行)

- 模块级函数（5 个），不再嵌套在 `create_app()` 内：
  - `handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled)` — WS 生命周期
  - `handle_json_message(data, agent, audio_buf, ws)` — 消息路由
  - `do_interrupt(agent, skip_if)` — 打断逻辑
  - `do_transcription(agent, ws, text, audio_buf, stt_emotion_state)` — STT 结果处理
  - `send_wake(ws)` — 唤醒通知

### 函数命名

提取后的函数去掉前导下划线（`_handle_ws` → `handle_ws`），因为它们是模块的公开 API。

同名 handler 缩到各自模块内，命名保持 `xxx_handler` 惯例（如 `db_status_handler`），不导出。

### 导入依赖

每个新文件只 import 自己需要的：

| 文件 | core/ 依赖 | Module/ 依赖 | 第三方 |
|------|-----------|-------------|--------|
| `server.py` | `RunState` | — | `aiohttp` |
| `debug_routes.py` | `Message`, `MessageRole`, `SessionId`, `MemoryType` | — | `aiohttp`, `pydantic` |
| `api_routes.py` | — | — | `aiohttp` |
| `ws_handler.py` | `RunState`, `InterruptData`, `UserInputData` | `WebSocketChannel`, `AudioBuffer` | `aiohttp` |

### 路由注册结构

```python
# server.py
def create_app(agent, stt_transcribe=None, serve_static=True,
               stt_emotion_state=None, wake_word_enabled=False,
               task_system=None) -> web.Application:
    app = web.Application()
    app["agent"] = agent
    if task_system is not None:
        app["task_system"] = task_system

    # 基础路由（本文件）
    app.router.add_get("/ws", ws_route_handler)
    app.router.add_get("/", index_handler)
    app.router.add_get("/debug", debug_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/shutdown", shutdown_handler)
    app.router.add_post("/restart", restart_handler)
    app.router.add_get("/tools", tools_handler)
    app.router.add_post("/tools/reload", tool_reload_handler)

    # 子模块路由
    register_debug_routes(app)    # 来自 debug_routes.py
    register_api_routes(app)      # 来自 api_routes.py

    if serve_static:
        app.router.add_static("/static/", _STATIC_DIR, show_index=True)
    if _LIVE2D_DIR.is_dir():
        app.router.add_static("/live2d/", _LIVE2D_DIR, show_index=False)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app
```

### 测试影响

`Module/input/tests/test_server.py` 中 import 变更：

- `from Module.input.server import _do_interrupt` → `from Module.input.ws_handler import do_interrupt`
- `from Module.input.server import _do_transcription` → `from Module.input.ws_handler import do_transcription`
- `from Module.input.server import _handle_json` → `from Module.input.ws_handler import handle_json_message`
- `from Module.input.server import create_app` → 不变

`core/tests/test_phase3.py` 同理（`create_app` import 不变）。

## 不变与约束

- 不修改任何业务逻辑——纯文件搬迁
- 不修改 handler 签名（除了 WS handler 去前导下划线）
- `Module/input/__init__.py` 重导出 `create_app` 不变
- `core/agent/server.py` 的 `from Module.input.server import create_app` 不变
