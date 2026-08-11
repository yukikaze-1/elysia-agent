# Disable v1 Chat / Keep Browser Debug 设计

> **For agentic workers:** 实施时用 writing-plans 出 plan，按 plan 执行。

**Goal:** 停用 v1 浏览器聊天界面（`/`），保留浏览器 Debug 仪表盘（`/debug`），可逆（env 开关随时翻回）。

**Architecture:** `create_app` 增加 `serve_chat` 开关（默认关）。聊天路由 `/` 在开关关闭时不注册（404）；`/debug` 路由从「依赖 index.html 存在」的嵌套块中拆出，独立于聊天注册。开关经 `ServerConfig.serve_chat`（env `SERVE_CHAT`）传至生产入口。

**Tech Stack:** Python 3.12 + aiohttp（`core/input/server.py`）+ `Module.config.ConfigField`（env 绑定）。

---

## 背景

- v1 聊天 UI（`frontend/` Vue + Vite 多页：`index.html`=聊天、`debug.html`=debug）是 legacy，**不再需要**浏览器聊天界面。
- Debug 仪表盘（`/debug`）**保留为浏览器访问**（运维方便，之后可能搬进 Electron）。它走 REST `/debug/api/*` + SSE `/debug/api/tasks/stream`，**无 WS**。
- Electron 前端（elysia-frontend）用 Node `ws` 带 `Authorization` header 鉴权，**不依赖 `/`**。
- WS query-param token **不做**：v1 聊天 ban 掉后无浏览器 WS 客户端（Electron 走 header、debug 走 REST/SSE），无消费方。

## 现状缺陷（触发本设计）

`core/input/server.py` 的 `debug_handler` **嵌套在 `if index_path.is_file():` 块内**——Debug 页面是否被服务取决于 `static/index.html` 是否存在。删掉聊天入口（index.html）会连带 `/debug` 一起 404，无法独立保留 debug。

```python
# 现状（server.py:501-529）——/ 与 /debug 均在顶层注册，但 debug 嵌套在 index.html 存在性判断内
index_path = _STATIC_DIR / "index.html"
if index_path.is_file():                      # ← debug 依赖 index.html 存在
    ... index_handler / add_get("/")
    debug_path = _STATIC_DIR / "debug.html"
    if debug_path.is_file():
        ... debug_handler / add_get("/debug")
```

## 设计

### 1. `create_app` 加 `serve_chat` 参数

`core/input/server.py:create_app(..., serve_static: bool = True, serve_chat: bool = False, ...)`：

- `serve_chat=False`（默认）→ 不注册 `/` 聊天路由（`/` 返回 404）。
- `serve_chat=True` → 保持现状（`static/index.html` 存在时 `/` 返回聊天页）。
- **`serve_static` 语义不变**：仍控制 `/static/` 挂载；`/live2d/` 维持顶层（仅受目录存在性约束，现状如此）；`/` 与 `/debug` 也维持在顶层注册（现状即顶层，仅受文件存在性约束）——重构不改变 serve_static=False 调用方（如 `scripts/capture_ws_trace.py`）对 `/`、`/debug`、`/live2d/` 的现状。

### 2. 拆 debug 耦合

把 `debug_handler` 从 `if index_path.is_file()` 块中**拆出到顶层**（与现状一致的同层，但**不再依赖 index.html 存在**）：

```python
# /static/ 归 serve_static 门控（现状不变）；/live2d/ 维持顶层（仅受目录存在性约束，现状不变）
if serve_static and _STATIC_DIR.is_dir():
    app.router.add_static("/static/", ...)

if _LIVE2D_DIR.is_dir():
    app.router.add_static("/live2d/", ...)

# Debug 页面——独立于聊天，index.html 存在与否不影响（拆耦合）
debug_path = _STATIC_DIR / "debug.html"
if debug_path.is_file():
    async def debug_handler(...): ...
    app.router.add_get("/debug", debug_handler)

# 聊天页面——serve_chat 关闭则不注册（/ 404）
if serve_chat:
    index_path = _STATIC_DIR / "index.html"
    if index_path.is_file():
        async def index_handler(...): ...
        app.router.add_get("/", index_handler)
```

### 3. 配置接线

- `core/config/app_config.py` `ServerConfig` 加字段：
  ```python
  serve_chat: bool = ConfigField(default=False, env="SERVE_CHAT")
  ```
- `core/agent/server.py:437` 生产调用传参：`create_app(cast(Any, self), serve_static=True, serve_chat=self._config.server.serve_chat, ...)`。
- `core/agent/server.py:459` 启动 banner：`serve_chat=False` 时不打印「前端: http://{host}:{port}/」（/ 404 死链），改为提示 `SERVE_CHAT=1` 可恢复；`serve_chat=True` 维持现状。

### 4. 前端源码/构建与文档

**不改** `frontend/` 源码与 vite 构建入口（聊天入口保留，ban 可逆）。

**残留暴露（如实记录，接受）**：`serve_static=True` 时 `/static/` 挂载整目录——`/static/index.html` 仍可直接访问（聊天资产与 debug 共享 `/static/assets/*`，无法单独摘掉该挂载）。ban 的效果是移除 `/` **入口**（正常访问路径 404），`/static/index.html` 需知 URL 才能加载，属软 ban 的接受残留，不做额外守卫（用户意图是「暂时 ban 看影响」，可逆优先）。

**文档/脚本同步**：
- `README.md`：`--with-frontend` / 前端开发节——说明构建后访问 `/` 需 `SERVE_CHAT=1`，默认 `/` 404、`/debug` 正常。
- `scripts/start.sh`：输出与 `serve_chat` 一致（`static/index.html` 存在但 `serve_chat=False` 时提示「聊天已停用，`SERVE_CHAT=1` 恢复」而非「访问 / 将无 v1 UI」）。

## 测试

在既有 `core/input/tests/test_server.py` 追加用例（`serve_static=True` + monkeypatch `_STATIC_DIR` 指向临时目录）：

1. `serve_chat=False`（默认）+ `index.html` 存在 + `debug.html` 存在 → `GET /` = 404、`GET /debug` = 200。
2. `serve_chat=True` + 两者存在 → `GET /` = 200、`GET /debug` = 200。
3. **只有 `debug.html`（无 `index.html`）** + `serve_chat=False` → `GET /debug` = 200（debug 独立于聊天）——回归拆耦合。
4. `serve_chat=True` 但无 `index.html` → `GET /` = 404（无聊天页自然 404，不误挂）。
5. 既有测试（多用 `serve_static=False`）不受影响。

## 验收标准

- 生产默认（无 `SERVE_CHAT` env）`/` 返回 404、`/debug` 正常。
- `SERVE_CHAT=1` 翻回后 `/` 恢复聊天页。
- Debug 页面在有聊天（index.html）与无聊天两种 static 布局下都正常。
- `make test-quick` / `make test-normal` / `pyright` / `ruff` / `check-types` 全绿。

## 决策记录（2026-08-11）

1. **serve_chat 默认关闭**（env `SERVE_CHAT=1` 才恢复）——用户显式「暂时 ban 掉 v1 聊天」。
2. **WS query-param token 不做**——v1 聊天（唯一浏览器 WS 客户端）ban 后无消费方；Electron 走 header、debug 走 REST/SSE。
3. **前端源码/构建不动**——ban 可逆；debug 还需前端构建产出 `static/debug.html`。
