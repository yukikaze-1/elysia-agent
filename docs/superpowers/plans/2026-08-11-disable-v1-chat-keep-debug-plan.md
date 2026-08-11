# Disable v1 Chat / Keep Browser Debug 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 停用 v1 浏览器聊天界面（`/` 返回 404），保留浏览器 Debug 仪表盘（`/debug` 正常），`SERVE_CHAT=1` 可逆恢复。

**Architecture:** `create_app` 加 `serve_chat` 开关（默认关）；`debug_handler` 从 `if index_path.is_file()` 嵌套块拆出到顶层（不再依赖 index.html 存在）；`/`、`/debug`、`/live2d/` 保持顶层注册（`serve_static` 语义不变）；开关经 `ServerConfig.serve_chat`（env `SERVE_CHAT`）传到生产入口 + 启动 banner 条件化。

**Tech Stack:** Python 3.12 + aiohttp（`core/input/server.py`）+ `Module.config.ConfigField` + pytest（`@pytest.mark.asyncio` + `aiohttp_client`）。

## Global Constraints

- 生产代码参数必须类型注解（ruff ANN001/002/003）；测试文件豁免。
- 禁相对导入（TID252）；注释中文。
- pytest strict：async 测试 `async def` + `@pytest.mark.asyncio` + `@pytest.mark.service`。
- 执行环境：pytest 用 `conda run -n Elysia python -m pytest ...`；提交前 `make lint` + `make pyright`（Elysia env）+ `make check-types`。
- 只改 `core/input/server.py`、`core/input/tests/test_server.py`、`core/config/app_config.py`、`core/agent/server.py`、`README.md`、`scripts/start.sh`、`docs/electron-backend-security.md`。**勿碰** `frontend/` 源码与 vite 构建入口（ban 可逆，前端不动）。
- `serve_chat` 默认 `False`；`/`、`/debug`、`/live2d/` 保持顶层注册（现状如此），重构不得把三者移入 `if serve_static` 块。**语义说明**：`serve_static` 只门控 `/static/`；`/live2d/`（目录存在性）、`/debug`（文件存在性）、`/`（serve_chat AND 文件存在性）各自门控不变。serve_chat 默认关会让**任何**调用方（含 `serve_static=False` 的 `scripts/capture_ws_trace.py`）在 static/index.html 存在时 `/` 从 200 变 404——这正是「ban 聊天」的意图，capture_ws_trace 只连 `/ws` 无实际影响，但语义是刻意变更而非「现状不变」。

---

### Task 1: `serve_chat` 参数 + 拆 debug 耦合（测试 + 实现一次完成）

**Files:**
- Modify: `core/input/tests/test_server.py`（追加 5 个用例 + helper）
- Modify: `core/input/server.py`（`create_app` 加 `serve_chat` 参数 + 静态服务块拆耦合）

**Interfaces:**
- Consumes: `create_app(agent, serve_static, serve_chat)`（本 Task Step 3 实现）。
- Produces: 5 个 serve_chat/拆耦合路由测试（断言 `GET /`、`GET /debug` 状态码）+ `create_app(..., serve_chat=...)` 签名 + `serve_chat` 门控 `/` + `debug_handler` 拆出顶层。

- [ ] **Step 1: 写失败测试**

在 `core/input/tests/test_server.py` 顶部加一个模块级 helper（复用 `_make_app` 风格，但 serve_static=True + monkeypatch `_STATIC_DIR`）：

```python
import tempfile
from pathlib import Path
from unittest.mock import patch

def _make_static_app(static_dir: Path, serve_chat: bool, files: tuple[str, ...]):
    """serve_static=True 的 app——_STATIC_DIR 指向 tmp 目录（含指定文件）。"""
    from core.input.server import create_app
    for name in files:
        (static_dir / name).write_text(f"<html>{name}</html>", encoding="utf-8")
    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    with patch("core.input.server._STATIC_DIR", static_dir):
        return create_app(agent, serve_static=True, serve_chat=serve_chat)
```

测试类（追加到文件末尾）：

```python
@pytest.mark.service
class TestServeChat:
    @pytest.mark.asyncio
    async def test_serve_chat_false_root_404_debug_200(self, aiohttp_client, tmp_path):
        """serve_chat=False（默认）：/ 404、/debug 200。"""
        app = _make_static_app(tmp_path, serve_chat=False, files=("index.html", "debug.html"))
        client = await aiohttp_client(app)
        assert (await client.get("/")).status == 404
        assert (await client.get("/debug")).status == 200

    @pytest.mark.asyncio
    async def test_serve_chat_true_root_200_debug_200(self, aiohttp_client, tmp_path):
        """serve_chat=True：/ 200、/debug 200。"""
        app = _make_static_app(tmp_path, serve_chat=True, files=("index.html", "debug.html"))
        client = await aiohttp_client(app)
        assert (await client.get("/")).status == 200
        assert (await client.get("/debug")).status == 200

    @pytest.mark.asyncio
    async def test_debug_served_without_index(self, aiohttp_client, tmp_path):
        """只有 debug.html（无 index.html）+ serve_chat=False：/debug 仍 200（拆耦合回归）。"""
        app = _make_static_app(tmp_path, serve_chat=False, files=("debug.html",))
        client = await aiohttp_client(app)
        assert (await client.get("/debug")).status == 200

    @pytest.mark.asyncio
    async def test_serve_chat_true_no_index_root_404(self, aiohttp_client, tmp_path):
        """serve_chat=True 但无 index.html：/ 404（无聊天页自然 404）。"""
        app = _make_static_app(tmp_path, serve_chat=True, files=("debug.html",))
        client = await aiohttp_client(app)
        assert (await client.get("/")).status == 404

    @pytest.mark.asyncio
    async def test_serve_chat_false_no_files_both_404(self, aiohttp_client, tmp_path):
        """serve_chat=False 且 static 空目录：/ 404、/debug 404（无文件不误挂）。"""
        app = _make_static_app(tmp_path, serve_chat=False, files=())
        client = await aiohttp_client(app)
        assert (await client.get("/")).status == 404
        assert (await client.get("/debug")).status == 404
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py -q -k "ServeChat" -x --tb=short`
Expected: FAIL（`TypeError: create_app() got an unexpected keyword argument 'serve_chat'`）

- [ ] **Step 3: 最小实现（一次完成：参数 + 拆耦合）**

`core/input/server.py:create_app` 签名加 `serve_chat: bool = False`（在 `serve_static` 之后），并把静态服务块重构：

把现状（server.py:495-529）：

```python
if serve_static and _STATIC_DIR.is_dir():
    app.router.add_static("/static/", _STATIC_DIR, show_index=False)

if _LIVE2D_DIR.is_dir():
    app.router.add_static("/live2d/", _LIVE2D_DIR, show_index=False)

index_path = _STATIC_DIR / "index.html"
if index_path.is_file():
    async def index_handler(...): ...
    app.router.add_get("/", index_handler)
    debug_path = _STATIC_DIR / "debug.html"
    if debug_path.is_file():
        async def debug_handler(...): ...
        app.router.add_get("/debug", debug_handler)
```

改为：

```python
if serve_static and _STATIC_DIR.is_dir():
    app.router.add_static("/static/", _STATIC_DIR, show_index=False)

if _LIVE2D_DIR.is_dir():
    app.router.add_static("/live2d/", _LIVE2D_DIR, show_index=False)

# Debug 页面——独立于聊天，index.html 存在与否不影响（拆耦合）
debug_path = _STATIC_DIR / "debug.html"
if debug_path.is_file():
    async def debug_handler(_request: web.Request) -> web.FileResponse:
        """提供 Debug 仪表盘 HTML 页面。"""
        return web.FileResponse(debug_path)
    app.router.add_get("/debug", debug_handler)

# 聊天页面——serve_chat 关闭则不注册（/ 404）
if serve_chat:
    index_path = _STATIC_DIR / "index.html"
    if index_path.is_file():
        async def index_handler(_request: web.Request) -> web.FileResponse:
            """提供主聊天应用 HTML 页面。"""
            return web.FileResponse(index_path)
        app.router.add_get("/", index_handler)
```

> 注：保持 `/static/`（serve_static 门控）、`/live2d/`（目录存在性门控）、`/debug`（文件存在性门控）、`/`（serve_chat AND 文件存在性门控）各自现状层级，**不得**把三者并入 `if serve_static` 块。**Step 4 必须等此完整重构后才绿**（只加参数不重构，`test_serve_chat_false_root_404_debug_200` 与 `test_debug_served_without_index` 仍红）。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py -q -k "ServeChat" -x --tb=short`
Expected: PASS（重构后 5 个测试全绿）

- [ ] **Step 5: 提交**

```bash
git add core/input/server.py core/input/tests/test_server.py
git commit -m "feat(server): serve_chat switch and decouple debug from index.html"
```

---

### Task 2: `ServerConfig.serve_chat` + 生产接线 + banner

**Files:**
- Modify: `core/config/app_config.py`（`ServerConfig`）
- Modify: `core/agent/server.py`（create_app 调用 + banner）

**Interfaces:**
- Consumes: Task 1 `create_app(..., serve_chat=...)`。
- Produces: `ServerConfig.serve_chat`（env `SERVE_CHAT`）；生产传参；banner 条件化。

- [ ] **Step 1: 写失败测试**

`core/config/tests/test_app_config.py` 追加（沿用既有模式——`ServerConfig()` 默认值 + dataclass metadata env 绑定断言，不真的走 `AgentConfig.load()`）：

```python
def test_serve_chat_default_false(self):
    """serve_chat 默认关（ban v1 聊天）。"""
    from core.config.app_config import ServerConfig
    assert ServerConfig().serve_chat is False

def test_serve_chat_env_binding(self):
    """serve_chat 绑定 env SERVE_CHAT。"""
    import dataclasses
    from core.config.app_config import ServerConfig
    f = next(f for f in dataclasses.fields(ServerConfig) if f.name == "serve_chat")
    assert f.metadata["_config_env"] == "SERVE_CHAT"
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/config/tests/test_app_config.py -q -k "serve_chat" -x --tb=short`
Expected: FAIL（`AttributeError: 'ServerConfig' object has no attribute 'serve_chat'`）

- [ ] **Step 3: 最小实现**

`core/config/app_config.py` `ServerConfig` 加字段（在 `access_token` 附近）：

```python
serve_chat: bool = ConfigField(default=False, env="SERVE_CHAT")
```

`core/agent/server.py:437` create_app 调用加参数：

```python
app = create_app(cast(Any, self), serve_static=True,
                 serve_chat=self._config.server.serve_chat, ...)
```

`core/agent/server.py` banner（约 :459）条件化——`run_server` 作用域无 `serve_chat` 局部名，须先取局部再判断：

```python
serve_chat = self._config.server.serve_chat   # banner 局部——与 create_app 传参同源
if serve_chat:
    # banner 前端行（现状：「前端: http://{host}:{port}/」）
    ...
else:
    # banner 补一行：「聊天 UI 已停用（serve_chat=False），/debug 仪表盘可用；SERVE_CHAT=1 可恢复」
```

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/config/tests/ -q -k "serve_chat" -x --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add core/config/app_config.py core/agent/server.py core/config/tests/
git commit -m "feat(config): serve_chat switch and conditional startup banner"
```

---

### Task 3: 文档同步（README / start.sh / security）

**Files:**
- Modify: `README.md`、`scripts/start.sh`、`docs/electron-backend-security.md`

**Interfaces:**
- Consumes: Task 1/2 语义（serve_chat 默认关）。
- Produces: 文档/脚本与 serve_chat 一致。

- [ ] **Step 1: README 同步**

`README.md` 两处：

- **主使用段（约 :67「打开浏览器访问 http://localhost:8765，在聊天界面与 AI 对话」）**——这是用户第一落点，改为注明默认 `/` 404：
  ```markdown
  打开浏览器访问 http://localhost:8765 —— v1 网页聊天默认停用（`/` 404），Debug 仪表盘 `/debug` 可用（需已构建前端，`static/debug.html` 存在）；`SERVE_CHAT=1`（env）可恢复浏览器聊天。
  ```
- **`--with-frontend` / 前端开发节**：构建 v1 UI 后访问 `/` 需 `SERVE_CHAT=1`。注意**不要**写「/debug 始终可用」——backend-only（未构建前端）时 `static/debug.html` 不存在，`/debug` 同样 404。

- [ ] **Step 2: start.sh 同步**

`scripts/start.sh`（:68-80）——start.sh **不读 SERVE_CHAT**（无此判断），只按 `static/index.html` 存在性输出。修两处：

- **static 存在分支**（:72「✓ 前端构建完成」、:78「✓ 跳过前端构建（static/ 已存在）」）：补一行提示，避免误导「/ 可用」——实际默认 404。改文案如：
  ```bash
  echo "  ✓ 前端构建完成（聊天界面默认停用，SERVE_CHAT=1 恢复；/debug 仪表盘可用）"
  ```
- **无 static 分支**（:80）：补「/debug 亦不可用（需先构建前端）」：
  ```bash
  echo "  ⚠ 跳过前端构建（backend-only，无 static/）——/ 与 /debug 均无 UI，仅 WS/REST/API 可用"
  ```

> 注：不要在 start.sh 里按 SERVE_CHAT 分支（脚本不读 env，保持现状按文件存在性判断即可）。

- [ ] **Step 3: security 文档同步**

`docs/electron-backend-security.md` 增加一行，**精确表述**（勿写「/debug 属管理面保护」——HTML 壳页匿名，只有数据 API 鉴权）：

> v1 网页聊天默认停用（`SERVE_CHAT=0`，env `SERVE_CHAT=1` 恢复），`/` 404。Debug 仪表盘：**HTML 页（`/debug`）匿名可加载**，`/debug/api/*` 与 SSE 经 `SERVER_AUTH_TOKEN` 鉴权（页面匿名、数据鉴权）。Electron 走 `Authorization` header 鉴权不受影响。

- [ ] **Step 4: 验证 + 提交**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py core/config/tests/ -q --tb=short`（确认无回归）
Run: `bash -n scripts/start.sh`（脚本语法）
```bash
git add README.md scripts/start.sh docs/electron-backend-security.md
git commit -m "docs: sync serve_chat default-off for README/start.sh/security"
```

---

### Task 4: 全量门禁

- [ ] **Step 1: 全量门禁**

Run: `conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short`
Expected: PASS（记录计数）
Run: `conda run -n Elysia pyright`
Expected: 0 errors
Run: `conda run -n Elysia ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__`
Expected: All checks passed
Run: `conda run -n Elysia make check-types`
Expected: pass

- [ ] **Step 2: 若门禁失败**——修复（注释中文、类型注解、语义保持），重跑直至绿。

---

## Self-Review

- **Spec 覆盖**：serve_chat 开关 + 拆 debug 耦合（Task 1，测试+实现一次完成）、config/banner（Task 2）、README/start.sh/security 文档（Task 3）、全量门禁（Task 4）、残留暴露记录（spec 文档，不在代码）、WS query-param 不做（决策记录，不实现）。全部有 Task。
- **类型一致**：`create_app(..., serve_chat=...)` 在 Task 1 测试 / Task 1 实现 / Task 2 生产调用三处签名一致；`ServerConfig.serve_chat` env `SERVE_CHAT`。
- **无占位符**：Task 1 测试为完整可运行代码；Task 2 config 测试按既有模式（`ServerConfig()` 默认值 + `_config_env` metadata 断言）。
