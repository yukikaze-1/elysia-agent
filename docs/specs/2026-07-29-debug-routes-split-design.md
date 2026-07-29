# Debug Routes 拆分 — 设计规格

**日期**: 2026-07-29
**类型**: 重构（结构优化，不变更业务逻辑）

## 动机

`core/input/debug_routes.py`（802 行）是 `core/` 下仅次于 `cognitive/loop.py`（986 行）的第二大单文件。包含 23 个 route handler + 3 个 helper（`_sessions_to_client`、`_list_character_versions`、`_character_rollback`）+ `session_detail_handler` 内部嵌套的 `_get_character_state` + TaskSystem SSE 接线 + startup/shutdown 生命周期，全部嵌套在 `register_debug_routes()` 闭包内。按资源自然分为 5 组（Overview/LTM/Archive+Sessions/Prompts+Responses/Character），适合拆分为独立模块。

## 设计

### 文件布局（变更后）

```
core/input/
├── debug_routes.py           → 删除
├── debug_routes/             → 新建 package
│   ├── __init__.py           → register_debug_routes() 入口 + TaskSystem SSE + startup/shutdown
│   ├── _common.py            → sessions_to_client() 共享 helper
│   ├── overview.py           → GET /debug/api + GET /debug/api/db-status
│   ├── ltm.py                → LTM CRUD (5 handlers: GET/POST/PUT/DELETE /debug/api/ltm)
│   ├── archive.py            → Archive CRUD + Sessions CRUD (8 handlers)
│   ├── recording.py          → Prompts + Responses (4 handlers)
│   └── character.py          → Character 版本历史 (4 handlers + 2 private helpers)
└── server.py                 → import 路径不变（from .debug_routes import register_debug_routes）
```

### 接口契约

#### `__init__.py`

```python
def register_debug_routes(app: web.Application) -> None:
    """注册所有 /debug/api/* 路由到 aiohttp Application。

    调用各子模块的 register_* 函数，接线 TaskSystem SSE，注册 app 生命周期钩子。
    """
```

内部按顺序调用：
1. `register_overview_routes(app)`
2. `register_ltm_routes(app)`
3. `register_archive_routes(app, sessions_to_client)`
4. `register_recording_routes(app)`
5. `register_character_routes(app)`
6. TaskSystem SSE 接线（`Module/task_system/debug_api.py`）
7. `app.on_startup` / `app.on_shutdown` 注册

#### `_common.py`

```python
def sessions_to_client(msgs: list[Message] | list[dict]) -> list[dict]:
    """将消息列表转为 session 详情前端兼容格式。接受 Message 或原始 dict。"""
```

这是原 `register_debug_routes()` 内部的 `_sessions_to_client()` 闭包，提取为模块级 public 函数。去掉了前导下划线（现为共享 helper）。

#### `overview.py`

```python
def register_overview_routes(app: web.Application) -> None: ...
async def debug_api_handler(request: web.Request) -> web.Response: ...
async def db_status_handler(request: web.Request) -> web.Response: ...
```

- `debug_api_handler` → `GET /debug/api` → 返回聚合 debug info JSON
- `db_status_handler` → `GET /debug/api/db-status` → 返回 LTM/session/archive 状态

Handler 通过 `request.app["agent"]` 获取 Agent 实例（原闭包捕获 `app`，改为从 request 获取）。

#### `ltm.py`

```python
def register_ltm_routes(app: web.Application) -> None: ...
async def ltm_list_handler(request: web.Request) -> web.Response: ...
async def ltm_insert_handler(request: web.Request) -> web.Response: ...
async def ltm_delete_single_handler(request: web.Request) -> web.Response: ...
async def ltm_update_handler(request: web.Request) -> web.Response: ...
async def ltm_clear_handler(request: web.Request) -> web.Response: ...
```

5 个端点，完整 LTM CRUD（搜索/分页/新增/编辑/单条删除/清空）。

#### `archive.py`

```python
def register_archive_routes(app: web.Application, sessions_to_client) -> None: ...
async def archive_list_handler(request: web.Request) -> web.Response: ...
async def archive_delete_handler(request: web.Request) -> web.Response: ...
async def archive_update_handler(request: web.Request) -> web.Response: ...
async def archive_clear_handler(request: web.Request) -> web.Response: ...
async def sessions_list_handler(request: web.Request) -> web.Response: ...
async def session_detail_handler(request: web.Request) -> web.Response: ...
async def session_delete_handler(request: web.Request) -> web.Response: ...
async def sessions_clear_handler(request: web.Request) -> web.Response: ...
```

8 个端点：消息归档 CRUD（分页/编辑/单条删除/清空）+ Session 管理（列表/详情/删除/清空）。`sessions_to_client` 作为参数传入（`session_detail_handler` 需要它格式化消息）。`session_detail_handler` 内部保留了 `_get_character_state` 嵌套闭包（仅被该 handler 使用，不提升）。

#### `recording.py`

```python
def register_recording_routes(app: web.Application) -> None: ...
async def prompts_list_handler(request: web.Request) -> web.Response: ...
async def prompt_detail_handler(request: web.Request) -> web.Response: ...
async def responses_list_handler(request: web.Request) -> web.Response: ...
async def response_detail_handler(request: web.Request) -> web.Response: ...
```

4 个端点：Prompt 录制列表/详情 + Response 录制列表/详情。

#### `character.py`

```python
def register_character_routes(app: web.Application) -> None: ...
async def l2_versions_handler(request: web.Request) -> web.Response: ...
async def l2_rollback_handler(request: web.Request) -> web.Response: ...
async def character_versions_handler(request: web.Request) -> web.Response: ...
async def character_rollback_handler(request: web.Request) -> web.Response: ...
```

4 个端点 + 2 个私有 helper（`_list_character_versions`, `_character_rollback`）：L2 Self 版本历史/回滚 + C6 SpeakingStyle 版本历史/回滚。

### Handler 变更：闭包 → 顶层函数

**之前**（闭包，捕获外部 `app`）：
```python
def register_debug_routes(app):
    async def db_status_handler(request):
        agent = request.app["agent"]  # 也能用 request.app
        ...
    app.router.add_get("/debug/api/db-status", db_status_handler)
```

**之后**（顶层函数）：
```python
# overview.py
async def db_status_handler(request: web.Request) -> web.Response:
    agent = request.app["agent"]
    ...

def register_overview_routes(app: web.Application) -> None:
    app.router.add_get("/debug/api/db-status", db_status_handler)
```

Handler 通过 `request.app["agent"]` 获取 Agent——aiohttp 的 `Request.app` 指向创建时的 `Application` 实例，所有 handler 均可访问。

### 调用方兼容

`core/input/server.py`（约第 18 行）：
```python
from .debug_routes import register_debug_routes
```

Python 的 import 机制自动解析：当 `debug_routes.py` 变为 `debug_routes/` package 后，`from .debug_routes import register_debug_routes` 从 `debug_routes/__init__.py` 导入同名符号。**调用方零改动。**

## 非目标

- 不新增测试（纯结构重构）
- 不变更任何 handler 的业务逻辑、参数校验、错误处理
- 不改变路由路径或 HTTP 方法
- 不改变 `core/debug/info.py` 或 `Module/task_system/debug_api.py`

## 验证

```bash
# 现有 server 测试（覆盖 debug routes 注册）
pytest core/input/tests/test_server.py -v

# import 路径验证
python -c "from core.input.debug_routes import register_debug_routes; print('OK')"

# 全量 input 测试
pytest core/input/tests/ -v
```

手动冒烟：启动服务端，访问 `/debug` 页面，确认 Overview 16 面板、LTM CRUD、Archive/Sessions 管理、Character 版本历史均正常加载。
