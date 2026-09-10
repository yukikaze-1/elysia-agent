# 批次 8a — 安全加固 + 附件生命周期 + 搜索合规 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 版本：** v0.2（v0.1 基础上吸收 review：T2 补离线重连例外 + T3 改用真枚举向量 + T5/T9 测试接真实夹具 + T6 公网地址修正 + T12 守卫移 LLMFactory + T13 字段/离线/并发修正 + T14 kind read(65) + caplog 级别 + 双 token 回归项）

**Goal:** 修复批次 0-7 全量审查的 A 安全 / B 附件 / C 搜索 三簇 + 探活共 20 条发现，闭合 BE-030/BE-031/BE-070 三项合规缺口，且 `make test-normal` 全绿。

**Architecture:** 全部为服务端既有模块的行为收紧/正确性修复——不改 wire 契约（`frontend_contract` 零变动，`contract.generated.ts` 无漂移）、不加新错误码、不加新依赖。跨 4 个输入层模块（server.py / ws_handler.py / attachment_routes.py / api_routes.py / session_routes.py）+ 2 个存储模块（attachment_repo.py / attachment_gc.py）+ 3 个支撑模块（ssrf.py / secret_store.py / llm_adapter.py / loop.py）+ 1 个新模块（attachment_release.py）。

**Tech Stack:** Python 3.11+/aiohttp/pydantic/MongoDB（motor）。测试 conda env `Elysia`，运行前缀 `PATH="/home/yomu/miniconda3/envs/Elysia/bin:$PATH"`（或 `make test-normal` / `conda run -n Elysia python -m pytest ...`）。

## Global Constraints

- **绝对导入**（禁相对导入，ruff TID252）：`from core.storage.attachment_release import ...`，不用 `from .xxx`。
- **代码注释中文**；**新函数 docstring 中文**。
- **不触碰 `frontend_contract/`**、**不改 wire 契约模型**（本批次全部为服务端内部行为）。
- **不加新依赖**、**不加新错误码**（复用既有 code）。
- 测试 marker：service 为主（I/O mock），纯函数（GC/repo/ssrf）可 unit。
- **每 Task 必须 TDD**：先写失败测试 → 跑验证失败 → 实现 → 跑验证通过 → commit。
- 测试运行：`cd /home/yomu/elysia-agent && conda run -n Elysia python -m pytest <target> -q`。
- 本批次 baseline：`c2d6e04`（8a spec commit 之后）。
- **不修改 `docs/`**（文档在批次末尾统一更新，Task 15 之后）。

---

### Task 1: 管理面认证 fallback（D1）

**Files:**
- Modify: `core/input/server.py:286-299`（`_check_auth`）、`:43-65`（`warn_if_unsecured` 签名加 auth_token）
- Modify: `core/agent/server.py:371`（warn_if_unsecured 调用点）
- Modify: `core/input/tests/test_auth.py:140-158`（TestStartupGuard 更新签名）
- Test: `core/input/tests/test_auth.py`（新增 fallback 测试）

**Interfaces:**
- Produces: `_check_auth(request) -> bool` 闭包捕获 `auth_token` + `access_token`；`warn_if_unsecured(host, access_token, auth_token) -> None`。

- [ ] **Step 1: 写失败测试**（`core/input/tests/test_auth.py` 新增类；先给 `make_auth_app` 夹具加 `auth_token` 参数，M10）

```python
@pytest.fixture
def make_auth_app():
    def _make(access_token: str = "", auth_token: str = ""):
        ...  # 既有 stub 不变
        return create_app(agent, serve_static=False,
                          access_token=access_token, auth_token=auth_token)
    return _make


@pytest.mark.service
class TestAdminAuthFallback:
    """批次 8a D1：auth_token 空 + access_token 非空 → 管理端点 fallback 用 access_token。"""

    async def test_admin_anonymous_401_when_only_access_token(self, make_auth_app, aiohttp_client):
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/debug/api")
        assert resp.status == 401

    async def test_admin_with_access_token_ok(self, make_auth_app, aiohttp_client):
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/debug/api", headers={"Authorization": "Bearer secret"})
        # 断言 200 而非 != 401（MagicMock agent 无 task_system 可能 500，M5）
        assert resp.status == 200

    async def test_admin_wrong_token_401(self, make_auth_app, aiohttp_client):
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/debug/api", headers={"Authorization": "Bearer wrong"})
        assert resp.status == 401

    async def test_shutdown_with_access_token_not_401(self, make_auth_app, aiohttp_client):
        # /shutdown 由 _check_auth 护——仅验认证通过后可到达 handler（handler 本身 mock）
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.post("/shutdown", headers={"Authorization": "Bearer secret"})
        assert resp.status != 401

    async def test_two_tokens_strict_separation(self, make_auth_app, aiohttp_client):
        # 双 token：product token 不能访问管理端点；admin token 不能访问产品 API
        app = make_auth_app(access_token="prod", auth_token="admin")
        client = await aiohttp_client(app)
        assert (await client.get("/debug/api", headers={"Authorization": "Bearer prod"})).status == 401
        assert (await client.get("/debug/api", headers={"Authorization": "Bearer admin"})).status != 401
        assert (await client.get("/api/providers", headers={"Authorization": "Bearer admin"})).status == 401
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_auth.py::TestAdminAuthFallback -q`
Expected: FAIL——当前 `_check_auth` 在 auth_token 空时恒 True，匿名访问 /debug/api 不 401；双 token 时 product token 访问 /debug/api 也不 401（夹具已加 auth_token 参数）。

- [ ] **Step 3: 实现**

`core/input/server.py`：

```python
    def _check_auth(request: web.Request) -> bool:
        """管理端点认证（/shutdown /restart /tools /debug/api/*）。

        批次 8a（D1）fallback：auth_token 空 + access_token 非空 → 用 access_token 校验
        （Demo 单 token 语义，docs/14 §6）；两者都空 → 匿名本地开发。
        """
        if auth_token:
            expected = f"Bearer {auth_token}"
        elif access_token:
            expected = f"Bearer {access_token}"
        else:
            return True
        return request.headers.get("Authorization", "") == expected
```

`warn_if_unsecured` 签名改为 `(host, access_token, auth_token)`，加逻辑：非回环 + `auth_token` 空且 `access_token` 非空（或反之）→ `logger.warning("远程部署建议同时配置 AGENT_ACCESS_TOKEN 与 SERVER_AUTH_TOKEN…")`；两者都空 → 原 ERROR 显著告警；两者都配 → 不告警。调用点 `core/agent/server.py:371` 同步传 `auth_token`。

**更新既有 TestStartupGuard**（I4，语义防反转）：`test_non_loopback_with_token_no_warning`（test_auth.py:153-159）改传 `warn_if_unsecured("0.0.0.0", "secret", "admin")`（双 token 姿势，保持「不告警」断言）；新增 `test_non_loopback_one_token_warns` → `warn_if_unsecured("0.0.0.0", "secret", "")` 断言出现「建议同时配置」warning。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_auth.py -q`
Expected: PASS（含新 fallback 测试 + 既有 TestStartupGuard 更新后回归）。

- [ ] **Step 5: Commit**

```bash
git add core/input/server.py core/agent/server.py core/input/tests/test_auth.py
git commit -m "feat(batch8a): 管理面认证 fallback——auth_token 空回退 access_token（D1）+ 启动告警补 auth_token"
```

---

### Task 2: tool.status 发送连接校验（D2）

**Files:**
- Modify: `Module/output/websocket.py:39-59`（WebSocketChannel 加 `connection_id` 属性）
- Modify: `core/input/ws_handler.py:124-165`（每 v2 连接生成 conn_id 存 channel；server_info.connectionId 沿用）、`:835-847`（tool.status 路由传 sender_connection_id）
- Modify: `core/desktop/bridge.py:255-283`（`handle_status` 加 sender_connection_id 参数 + target 校验）
- Test: `core/input/tests/test_server.py` 或新 `core/desktop/tests/test_bridge_sender.py`

**Interfaces:**
- Consumes: `WebSocketChannel` 现有 `protocol_version` / `client_platform` 属性。
- Produces: `WebSocketChannel.connection_id: str`；`DesktopToolBridge.handle_status(status, *, sender_connection_id: str | None = None) -> bool`。
- **注意**：`desktop_conn_id`（ws_handler.py:138）仅 electron 生成——改为**每 v2 连接**生成 `conn_<hex>` 存 `channel.connection_id`；electron 的该值即原 `desktop_conn_id` 语义（`on_client_connect`、server_info.connectionId 沿用）。

- [ ] **Step 1: 写失败测试**

```python
"""core/desktop/tests/test_bridge_sender.py（新文件）"""
@pytest.mark.service
async def test_handle_status_rejects_wrong_sender():
    from core.desktop.bridge import DesktopToolBridge
    bridge = DesktopToolBridge(repo=repo_stub_with_call(call_id="c1", target_connection="conn_real"))
    ok = await bridge.handle_status(valid_status(call_id="c1"), sender_connection_id="conn_other")
    assert ok is False  # 错连接 → 拒绝
    # future 不 resolve（request_and_wait 仍挂起）

@pytest.mark.service
async def test_handle_status_allows_offline_resend_after_reconnect():
    # 断连重连后：调用 status=offline，target 是旧 id；当前已连接 electron 重传 → 放行
    bridge = DesktopToolBridge(repo=repo_stub_with_call(call_id="c1",
                                target_connection="conn_old", status="offline"))
    await bridge.on_client_connect("conn_new")   # 重连新 id
    ok = await bridge.handle_status(valid_status(call_id="c1", status="succeeded"),
                                    sender_connection_id="conn_new")
    assert ok is True
    doc = await bridge._repo.get("c1")
    assert doc["target_connection"] == "conn_new"  # 归属迁移到新连接

@pytest.mark.service
async def test_handle_status_rejects_offline_resend_from_other():
    # offline 调用 + 非当前连接提交 → 仍拒绝（不能借离线状态伪装）
    bridge = DesktopToolBridge(repo=repo_stub_with_call(call_id="c1",
                                target_connection="conn_old", status="offline"))
    await bridge.on_client_connect("conn_new")
    ok = await bridge.handle_status(valid_status(call_id="c1"), sender_connection_id="conn_evil")
    assert ok is False
```

（`repo_stub_with_call` / `valid_status` 用既有 test_bridge 的构造方式——`DesktopCallRepository` mock 或进程内 stub。）

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/desktop/tests/test_bridge_sender.py -q`
Expected: FAIL——当前 `handle_status` 无 sender 参数，签名不符（TypeError）。

- [ ] **Step 3: 实现**

`core/desktop/bridge.py` `handle_status`：

```python
    async def handle_status(self, status: dict, *,
                            sender_connection_id: str | None = None) -> bool:
        msg = status if isinstance(status, ToolStatusMessage) else self._parse_status(status)
        if msg is None:
            return False
        call_id = msg.callId
        doc = await self._repo.get(call_id)
        if doc is None:
            logger.warning("DesktopToolBridge tool.status 未知 callId=%s", call_id)
            return False
        # 批次 8a（D2）：发送连接必须是投递目标（防伪造结果）。
        # 例外（I2）：断连后重连的 Electron 换新 conn_id，在途 offline 调用的
        # target_connection 仍是旧 id——重连重传结果是既有 offline→终态闭环
        # （spec §4.2/D-o），须放行「当前已连接 electron 提交的 offline 调用重传」。
        target = doc.get("target_connection")
        doc_status = doc.get("status")
        resend_after_reconnect = (
            doc_status == STATUS_OFFLINE
            and sender_connection_id is not None
            and sender_connection_id == self._connection_id
        )
        if target and not resend_after_reconnect and sender_connection_id != target:
            logger.warning("DesktopToolBridge tool.status 发送连接非投递目标: call=%s", call_id)
            return False
        if resend_after_reconnect and target != sender_connection_id:
            # 重传通过：把调用归属迁移到新连接，后续校验按新 id 对齐
            await self._repo.mark(call_id, target_connection=sender_connection_id)
        ...  # 原有篡改校验 + 状态机
```

（`self._connection_id` 为当前已连接的 electron conn id——`on_client_connect` 设置。`resend_after_reconnect` 仅放行「offline 调用 + 当前已连接 electron」的重传；非 offline 调用仍严格校验 target 一致，伪造路径依旧被拒。）

`core/input/ws_handler.py:124-145`：每 v2 连接生成 conn id：

```python
    channel = WebSocketChannel(ws, protocol_version=protocol_version,
                               client_platform=client_platform)
    conn_id = f"conn_{uuid.uuid4().hex[:12]}" if protocol_version >= 2 else None
    channel.connection_id = conn_id
    agent.output.add_channel(channel)

    desktop_conn_id = conn_id if client_platform == "electron" else None
    if desktop_conn_id is not None:
        await agent.desktop_bridge.on_client_connect(desktop_conn_id)
        ...
```

`core/input/ws_handler.py:835-847` tool.status 路由：`channel.connection_id` 传入 `handle_status`；channel 无 conn_id（v1 或异常）→ 拒绝。

`Module/output/websocket.py` `__init__` 加 `self.connection_id: str | None = None`。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/desktop/tests/test_bridge_sender.py core/desktop/tests/ core/input/tests/test_server.py -q`
Expected: PASS（新测试 + 既有 bridge/server 回归）。

- [ ] **Step 5: Commit**

```bash
git add Module/output/websocket.py core/input/ws_handler.py core/desktop/bridge.py core/desktop/tests/test_bridge_sender.py
git commit -m "feat(batch8a): tool.status 发送连接校验——每 v2 连接 conn_id + target_connection 拒绝伪造（D2）"
```

---

### Task 3: 下载签名校验前置（D3）

**Files:**
- Modify: `core/input/attachment_routes.py:114-152`（`_download_handler` 校验序）
- Test: `core/input/tests/test_attachment_download.py`

**Interfaces:**
- Produces: 签名模式校验序改为 ① sig hex → ③ exp → ④ HMAC → ② repo.get；fail-open 路径不变（仅 ②）。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.service
async def test_download_wrong_hmac_sig_403_before_existence(make_app, aiohttp_client):
    # 合法 hex 但 HMAC 错误的 sig + 不存在的 id → 403 INVALID_SIGNATURE（而非 404）
    # 这是 D3 的枚举向量：修复前走到 ② repo.get → 404，修复后 ③④ 前置 → 403
    app = make_app()
    client = await aiohttp_client(app)
    exp = int(time.time()) + 3600
    resp = await client.get(f"/api/attachments/att_不存在?sig={'0'*64}&exp={exp}")
    assert resp.status == 403
    body = json.loads((await resp.read()).decode())
    assert body["code"] == "INVALID_SIGNATURE"
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_attachment_download.py::test_download_wrong_hmac_sig_403_before_existence -q`
Expected: FAIL——当前校验序 ① sig hex 通过（合法 hex）→ ② repo.get → 不存在 → 404（非 403）。

- [ ] **Step 3: 实现**

`_download_handler` 重排：`verify_sig` 分支先做 sig hex + exp 窗口 + HMAC 全部校验，通过后 `repo.get` + active + 文件检查。fail-open（`verify_sig=False`）路径保持「仅 repo.get + active」。

```python
    if verify_sig:
        sig = request.query.get("sig", "") or ""
        if not _SIG_RE.fullmatch(sig):
            return _error_response("INVALID_SIGNATURE", "缺少或非法签名", 403)
        try:
            exp = int(request.query.get("exp", "") or "")
        except ValueError:
            exp = -1
        now = int(time.time())
        if exp < now or exp > now + _SIG_TTL:
            return _error_response("SIGNATURE_EXPIRED", "签名已过期", 403)
        expected = hmac.new(sig_secret.encode(),
                            f"{attachment_id}:{exp}".encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return _error_response("INVALID_SIGNATURE", "签名校验失败", 403)
    # 之后才 repo.get + active + 文件存在
```

（fail-open 分支的 ② repo.get 保留；fail-closed 早退语义不变。**注意**：重排后非法 sig 请求不再触达 repo.get——Mongo 离线 + 非法 sig 由 503 变 403（既有 offline 测试用合法 sig 不受影响），这是存在性不泄露的预期代价，spec §6 已记录。）

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_attachment_download.py -q`
Expected: PASS（新测试 + 既有 206/404/expired/fail-open/offline 全回归）。

- [ ] **Step 5: Commit**

```bash
git add core/input/attachment_routes.py core/input/tests/test_attachment_download.py
git commit -m "feat(batch8a): 附件下载签名校验前置——无效 sig 恒 403 防存在性枚举（D3）"
```

---

### Task 4: 搜索分类组合过滤（D4）

**Files:**
- Modify: `core/storage/message_repo.py:21`（`_CATEGORY_PART_TYPES` 语义注释）、`:390-405`（`query_search` filter 构建）
- Modify: `core/input/session_routes.py:576-626`（`search_messages_handler` category 白名单校验）
- Test: `core/storage/tests/test_message_repo.py`（新增组合/link 测试）、`core/input/tests/test_session_routes.py`（未知 category 400）

**Interfaces:**
- Consumes: `_CATEGORY_PART_TYPES = {"text", "image", "link", "video", "audio", "file"}`。
- Produces: `query_search(..., category: str | None, ...)` 行为变化——query 与 category 拆 `$and` 两条 `$elemMatch`；category=link 走 URL 内容正则。

- [ ] **Step 1: 写失败测试**（`core/storage/tests/test_message_repo.py`，沿用既有 `FakeDocumentDBBackend()` + `_seed(fake, docs)` 模式，M2）

```python
    def test_query_search_category_link_matches_url_text(self):
        # 含 https 链接的消息 → category=link 命中（链接在 TextPart 内）
        fake = FakeDocumentDBBackend()
        _seed(fake, [{...含 text part content="看 https://example.com" 的消息...}])
        repo = self._make_repo(fake)
        messages, *_ = asyncio.run(repo.query_search(category="link"))
        assert len(messages) == 1

    def test_query_search_keyword_plus_category_and(self):
        # query + category 组合（不同元素）：关键词命中 text part、分类命中 image part
        fake = FakeDocumentDBBackend()
        _seed(fake, [{...含 text part "配图" + image part 的消息...}])
        repo = self._make_repo(fake)
        messages, *_ = asyncio.run(repo.query_search(query="配图", category="image"))
        assert len(messages) == 1
```

（`_seed` / `_make_repo` / `FakeDocumentDBBackend` 用 test_message_repo.py:165-171 既有模式。）

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_message_repo.py -q`
Expected: FAIL——category=link 恒空（`$elemMatch {type:link}` 无元素）；keyword+image 组合恒空（同元素要求）。

- [ ] **Step 3: 实现**

`query_search` filter 构建：

```python
        # 内容匹配：query 与 category 拆两条 $elemMatch 用 $and（不同 part 元素可分别命中）
        elem_text: dict = {}   # 关键词只命中 text part 的 content
        elem_type: dict = {}   # category 命中对应 part 的 type
        if query:
            elem_text = {"type": "text",
                         "content": {"$regex": query, "$options": "i"}}
        if category == "link":
            elem_type = {"type": "text",
                         "content": {"$regex": r"(https?://|www\.)", "$options": "i"}}
        elif category in _CATEGORY_PART_TYPES - {"text", "link"}:
            elem_type = {"type": category}
        elif category == "text" and not query:
            elem_type = {"type": "text"}
        elem = []
        if elem_text:
            elem.append({"$elemMatch": elem_text})
        if elem_type:
            elem.append({"$elemMatch": elem_type})
        if len(elem) == 1:
            filter_expr["content"] = elem[0]
        elif len(elem) == 2:
            filter_expr["$and"] = [{"content": e} for e in elem]
        if category == "tool":
            filter_expr["role"] = "tool"
```

`session_routes.py` search handler：category 非空且不在 `{"text","image","video","audio","file","link","tool"}` → `_error_response("CONTRACT_VALIDATION", ..., 400)`。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_message_repo.py core/input/tests/test_session_routes.py -q`
Expected: PASS（新测试 + 既有 text/image/tool/分页/时区全回归）。

- [ ] **Step 5: Commit**

```bash
git add core/storage/message_repo.py core/input/session_routes.py core/storage/tests/test_message_repo.py core/input/tests/test_session_routes.py
git commit -m "feat(batch8a): 搜索分类组合过滤——\$and 双 elemMatch + link URL 特判 + 未知 category 400（D4/BE-031）"
```

---

### Task 5: 会话删除附件引用释放（D5）

**Files:**
- Create: `core/storage/attachment_release.py`（提取 debug_routes/archive.py 的 release helpers）
- Modify: `core/input/debug_routes/archive.py`（改为 import 共享 helper）
- Modify: `core/input/session_routes.py:471-493`（`sessions_delete_handler` 先 release）
- Test: `core/input/tests/test_attachment_message.py`（REST 删会话 ref 归零）

**Interfaces:**
- Consumes: `agent.message_repo.query_page`、`agent.attachment_repo.remove_ref`。
- Produces: `release_session_attachments(agent, sid) -> None`、`release_messages_attachments(agent, msgs) -> None`、`release_message_attachments(agent, msg) -> None`、`_ATTACHMENT_RELEASE_BATCH`。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.service
async def test_rest_delete_session_releases_attachments():
    # 会话含引用 att_x 的消息 → DELETE /api/sessions/{sid} → remove_ref 被调
    # 复用本文件既有 _make_agent_app() 夹具；先经 WS 发送含 ImagePart(attachment_id="att_x")
    # 的消息落库（参照本文件既有 test 的消息流构造，确保 message_archive 有记录）
    agent, app, client = await _make_agent_app(...)
    ...  # 发送带 attachment_id="att_x" 的 v2 文本消息 → 消息落 archive + add_ref
    removed = []
    original = agent.attachment_repo.remove_ref
    async def spy(aid, mid):
        removed.append((aid, mid)); await original(aid, mid)
    agent.attachment_repo.remove_ref = spy
    resp = await client.delete("/api/sessions/s1", headers=auth_headers)
    assert resp.status == 200
    assert any(aid == "att_x" for aid, _ in removed)
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_attachment_message.py::test_rest_delete_session_releases_attachments -q`
Expected: FAIL——REST 删会话未调 remove_ref（removed 为空）。

- [ ] **Step 3: 实现**

创建 `core/storage/attachment_release.py`：把 archive.py:29-105 的 `_attachment_ids_from_message` / `_release_message_attachments` / `_release_messages_attachments` / `_release_session_attachments` / `_release_all_attachments` + `_ATTACHMENT_RELEASE_BATCH` 搬入，去掉私有下划线改公开名（保留 `_` 也行，plan 用公开名 `release_*`）。archive.py 改 import。`session_routes.py` `sessions_delete_handler` 在 `session_manager.delete` 前调用 `release_session_attachments(agent, sid)`。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_attachment_message.py core/input/tests/test_server.py core/input/debug_routes -q`
Expected: PASS（REST 释放 + debug 归档回归）。

- [ ] **Step 5: Commit**

```bash
git add core/storage/attachment_release.py core/input/debug_routes/archive.py core/input/session_routes.py core/input/tests/test_attachment_message.py
git commit -m "feat(batch8a): 会话删除附件引用释放——共享 helper + REST 路径接线（D5/BE-030+BE-070）"
```

---

### Task 6: SSRF site-local IPv6（A 簇 minor）

**Files:**
- Modify: `core/input/ssrf.py:71-79`（`_is_blocked_ip` 补 `is_site_local`）
- Test: `core/input/tests/test_ssrf.py`

- [ ] **Step 1: 写失败测试**

```python
def test_is_blocked_ip_covers_site_local_ipv6():
    from core.input.ssrf import _is_blocked_ip
    assert _is_blocked_ip("fec0::1") is True           # site-local（当前漏判 → 放行）
    assert _is_blocked_ip("fe80::1") is True           # link-local（已有）
    assert _is_blocked_ip("2001:4860:4860::8888") is False  # 公网（既有 test_ssrf.py 用址）
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_ssrf.py::test_is_blocked_ip_covers_site_local_ipv6 -q`
Expected: FAIL——`fec0::1` 当前 `is_site_local` 未判 → False（放行）。

- [ ] **Step 3: 实现**

```python
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_site_local   # 批次 8a：site-local IPv6 fec0::/10
        or addr.is_private
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or addr in _CGNAT_NETWORK
    )
```

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_ssrf.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/ssrf.py core/input/tests/test_ssrf.py
git commit -m "feat(batch8a): SSRF 拦截 site-local IPv6 fec0::/10"
```

---

### Task 7: 认证失败日志（A 簇 minor）

**Files:**
- Modify: `core/input/server.py:127-129`（访问中间件 401 补日志）
- Modify: `core/input/debug_routes/__init__.py:27-34`（管理域中间件 401 补日志）
- Test: `core/input/tests/test_auth.py`（新增 caplog 断言）

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.service
async def test_auth_failure_logged_without_token(self, make_auth_app, aiohttp_client, caplog):
    app = make_auth_app(access_token="secret")
    client = await aiohttp_client(app)
    await client.get("/api/providers")
    assert any("AUTH_REQUIRED" in r.message or "AUTH_INVALID" in r.message
               for r in caplog.records)
    # 日志不含 token 原文
    assert not any("secret" in r.message for r in caplog.records)
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_auth.py::test_auth_failure_logged_without_token -q`
Expected: FAIL——中间件无日志。

- [ ] **Step 3: 实现**

两处 401 分支前加 `logger.warning("认证失败 code=%s path=%s", code, request.path)`（`logger` 为模块级 `logging.getLogger(__name__)`；server.py 已有，debug_routes/__init__.py:11 也已有 `logger`，无需新增，M1）。不记 Authorization 头。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_auth.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/server.py core/input/debug_routes/__init__.py core/input/tests/test_auth.py
git commit -m "feat(batch8a): 认证失败记录日志（不含凭据）"
```

---

### Task 8: GC 日志收敛 + unlink 失败处理（A+B 簇 minor）

**Files:**
- Modify: `core/storage/attachment_gc.py:130/152/186`（全路径 → attachment_id/kind）
- Modify: `core/storage/attachment_gc.py:127-135`（规则1 unlink 抛 OSError 保留记录）
- Test: `core/storage/tests/test_attachment_gc.py`

- [ ] **Step 1: 写失败测试**

```python
def test_gc_log_no_absolute_path(caplog):
    import logging
    # GC 规则1 日志是 INFO 级——须显式 at_level(INFO) 才会被捕获（I5）
    with caplog.at_level(logging.INFO):
        run_attachment_gc(...)  # 用既有夹具构造有软删超期记录
    assert not any("/home/" in r.message or "data/uploads" in r.message
                   for r in caplog.records)

def test_gc_rule1_keeps_record_on_unlink_error(tmp_path, monkeypatch, caplog):
    # 文件存在但 unlink 抛 OSError → 记录保留（不 delete_record）
    monkeypatch.setattr(Path, "unlink", raise_oserror)
    run_attachment_gc(...)
    assert repo_get(aid) is not None  # 记录仍在
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_attachment_gc.py -q`
Expected: `test_gc_rule1_keeps_record_on_unlink_error` FAIL——当前 unlink 抛错仍删记录（删除断言）。`test_gc_log_no_absolute_path` 当前可能通过（INFO 捕获后才成立），Step 3 后保持绿。

- [ ] **Step 3: 实现**

规则1：

```python
            path = _file_path(doc)
            if path is not None and path.is_file():
                try:
                    path.unlink()
                    logger.info("附件 GC：删除软删超期文件 aid=%s kind=%s", aid, kind)
                except OSError:
                    logger.warning("附件 GC：删文件失败 aid=%s，保留记录待重试", aid)
                    continue   # 不删记录，下次 GC 重试
            await repo.delete_record(aid)
            attachment_resolver.unregister(aid)
            stats["deleted_files"] += 1
```

其余日志点改 aid/kind。文件缺失（path None / not is_file()）→ 走删记录（原逻辑不变）。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_attachment_gc.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/storage/attachment_gc.py core/storage/tests/test_attachment_gc.py
git commit -m "feat(batch8a): GC 日志收敛（无绝对路径）+ 规则1 unlink 失败保留记录"
```

---

### Task 9: 错误信封异常文本泄漏（A 簇 minor）

**Files:**
- Modify: `core/input/api_routes.py:299/330/584/960`（信封 `str(e)` → 通用消息 + logger）
- Test: `core/input/tests/test_server.py` 或 `test_upload.py`

- [ ] **Step 1: 写失败测试**（逐个触发 4 个信封点；用 monkeypatch 让 handler 抛 OSError）

```python
@pytest.mark.service
async def test_provider_save_error_no_exception_text(make_auth_app, aiohttp_client, monkeypatch):
    # api_routes.py:299/330 provider save/delete：add_provider 抛 OSError("EACCES: /home/x")
    from core.input import api_routes
    async def boom(*a, **k): raise OSError("EACCES: /home/user/secret.txt")
    monkeypatch.setattr(api_routes, "save_provider_to_store", boom)  # 或对应入口
    app = make_auth_app(access_token="secret")
    client = await aiohttp_client(app)
    resp = await client.post("/api/providers", json={...}, headers=auth)
    body = (await resp.read()).decode()
    assert "/home/" not in body and "EACCES" not in body
    assert "error" in json.loads(body)  # 信封仍在，code 不变
```

（image 上传 :584 与 desktop 上传 :960 各补同型测试：触发对应 handler 抛 OSError → body 无路径。四个点逐个断言。）

- [ ] **Step 2: 运行验证失败**

Expected: FAIL——当前信封含 `str(e)`（含绝对路径）。

- [ ] **Step 3: 实现**

四处 `except Exception as e:` 的响应构造改为通用 message（如「处理失败，请稍后重试」/「保存失败」），`str(e)` 进 `logger.warning("... %s", e)`。保留错误码（如 `ApiFail`/`INVALID_REQUEST` 语义不变）。四个点信封形状不同（`ApiFail`/`ImageUploadError`/`_tool_error`），各点按其现有信封改 message 字段即可。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py core/input/tests/test_upload.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/api_routes.py <test>
git commit -m "feat(batch8a): 错误信封去除异常文本——str(e) 只进日志"
```

---

### Task 10: Inner Voice 应用日志降级（A 簇 minor）

**Files:**
- Modify: `core/cognitive/loop.py:949`（`logger.info` → `logger.debug` 或摘要）
- Test: `core/input/tests/test_log_sensitivity.py` 或既有 loop 日志测试

- [ ] **Step 1: 写失败测试**

```python
def test_inner_voice_not_in_app_log(caplog, loop_with_turn):
    import logging
    # loop.py:949 是 logger.info——须显式 at_level(INFO) 才捕获（I5）
    with caplog.at_level(logging.INFO):
        run_turn(...)   # 触发一次带 inner_voice 的回合
    assert not any("<inner_voice>" in r.message for r in caplog.records)
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL——当前 INFO 记 inner_voice 正文（caplog at_level(INFO) 下能捕获到）。

- [ ] **Step 3: 实现**

`loop.py:949` 改为 `logger.debug`（保留正文，仅 debug 可见）或只记摘要（长度/是否含 inner_voice）。选 `logger.debug`（最小改动，审计要求「不写应用日志」在 INFO 下满足；conversation.log 审计通道不受影响）。

- [ ] **Step 4: 运行验证通过**

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/cognitive/loop.py <test>
git commit -m "feat(batch8a): Inner Voice 应用日志降级为 debug"
```

---

### Task 11: Secret Store 并发锁（A 簇 minor，PLAUSIBLE）

**Files:**
- Modify: `core/storage/secret_store.py`（模块级 `threading.Lock` 包 set/delete）
- Test: `core/storage/tests/test_secret_store.py`（若存在）或新建

- [ ] **Step 1: 写失败测试**

```python
def test_concurrent_set_no_lost_update(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from core.storage import secret_store
    path = tmp_path / "secrets.json"
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda i: secret_store.set_secret("svc", f"k{i}", f"v{i}", path=path), range(8)))
    data = secret_store.all_configured("svc", path=path)  # 或 _load
    assert len(data) == 8  # 无丢更新
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（并发丢更新，或当前无锁下确实会丢——若测试通过说明实际无并发路径，记录并在实现中仍加锁防御）。

- [ ] **Step 3: 实现**

```python
_LOCK = threading.Lock()

def set_secret(...):
    with _LOCK:
        ...  # 原 load → 改 → save
```

`delete_secret` 同理。

- [ ] **Step 4: 运行验证通过**

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/storage/secret_store.py <test>
git commit -m "feat(batch8a): Secret Store 并发锁——threading.Lock 防丢更新"
```

---

### Task 12: LLM 初始配置 SSRF 守卫（A 簇 minor）

**Files:**
- Modify: `Module/llm/registry.py`（`LLMFactory.create` custom 分支先 `validate_endpoint`）
- Test: `Module/llm/tests/test_registry.py` 或新建 `core/adapter/tests/test_llm_adapter_ssrf.py`

**Interfaces:**
- Consumes: `core.input.ssrf.validate_endpoint`（SSRFError）。
- Produces: `LLMFactory.create("custom/x", base_url=...)` 在 custom + base_url 非空时先 validate。

- [ ] **Step 1: 写失败测试**

```python
def test_initial_custom_endpoint_ssrf_guarded():
    from core.input.ssrf import SSRFError
    from Module.llm.registry import LLMFactory
    # 初始配置 custom + 内网 base_url → SSRFError（而非裸创建）
    with pytest.raises(SSRFError):
        LLMFactory.create("custom/x", base_url="http://169.254.169.254/",
                          api_key="k", **minimal_kwargs)
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL——当前 `LLMFactory.create` custom 分支无守卫（初始配置路径 base_url 在 registry.py:53 `resolve_base_url` 后才得到，但 validate 未接线）。

- [ ] **Step 3: 实现**

`Module/llm/registry.py` `LLMFactory.create`：custom backend 且 base_url 非空 → `from core.input.ssrf import validate_endpoint; validate_endpoint(base_url)`（抛 SSRFError 传播）。内置 provider（openai/ollama 等）跳过。

- [ ] **Step 4: 运行验证通过**

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/adapter/llm_adapter.py <test>
git commit -m "feat(batch8a): LLM 初始 custom 配置复用 SSRF 守卫"
```

---

### Task 13: add_ref/remove_ref 原子化（B 簇 minor）

**Files:**
- Modify: `core/storage/attachment_repo.py:180-229`（`add_ref` / `remove_ref`）
- Test: `core/storage/tests/test_attachment_repo.py`

**Interfaces:**
- Consumes: `self._db.update_one`（motor），现有 `STATUS_ACTIVE` 常量。
- Produces: `add_ref(attachment_id, message_id) -> None`（条件 update）；`remove_ref(attachment_id, message_id) -> None`（$pull+$inc + 条件软删）。

- [ ] **Step 1: 写失败测试**

```python
def test_add_ref_concurrent_no_lost_update():
    # 并发多次 add_ref 不同 message_id → 当前读改写丢更新（get → $set 全量）
    import asyncio
    async def run():
        await repo.add_ref("att_x", "m1")
        await asyncio.gather(*[repo.add_ref("att_x", f"m{i}") for i in range(2, 8)])
        return repo.get("att_x")
    doc = asyncio.run(run())
    assert doc["ref_count"] == len(doc["refs"]) == 7  # 不变量

def test_add_ref_idempotent_still_holds():
    # 幂等保持：重复 add 同 message_id → 不双计（既有语义回归）
    repo.add_ref("att_x", "m1"); repo.add_ref("att_x", "m1")
    doc = repo.get("att_x")
    assert doc["ref_count"] == 1 and doc["refs"] == ["m1"]

def test_remove_ref_soft_delete_on_zero():
    repo.add_ref("att_x", "m1")
    repo.remove_ref("att_x", "m1")
    doc = repo.get("att_x")
    assert doc["status"] == "deleted" and doc["ref_count"] == 0
```

- [ ] **Step 2: 运行验证失败**

Expected: `test_add_ref_concurrent_no_lost_update` FAIL——当前读改写（get→$set 全量 refs），并发 gather 下丢更新（ref_count 与 len(refs) 不一致）。幂等/软删两个测试当前已绿（既有语义），Step 3 后回归保持。

- [ ] **Step 3: 实现**

```python
    async def add_ref(self, attachment_id: str, message_id: str) -> None:
        """幂等原子 add：仅当 refs 不含该 message_id 才 addToSet + inc。"""
        db = self._require_db()
        await db.update_one(
            COLLECTION,
            {"attachment_id": attachment_id, "refs": {"$ne": message_id}},
            {"$addToSet": {"refs": message_id}, "$inc": {"ref_count": 1}},
        )

    async def remove_ref(self, attachment_id: str, message_id: str) -> None:
        """原子 remove：pull + dec（仅当 refs 确实含该 message_id）；归零且 active → 软删。"""
        db = self._require_db()
        await db.update_one(
            COLLECTION,
            {"attachment_id": attachment_id, "refs": message_id},
            {"$pull": {"refs": message_id}, "$inc": {"ref_count": -1}},
        )
        await db.update_one(
            COLLECTION,
            {"attachment_id": attachment_id, "ref_count": 0, "status": STATUS_ACTIVE},
            {"$set": {"status": "deleted", "deleted_at": now_iso()}},
        )
```

（注意：附件文档以 **`attachment_id` 字段**为键，不是 `_id`；`_require_db()` 保持离线 `StorageUnavailableError` 语义。`now_iso` 从 core.schema.utils 导入，与 mark_deleted 一致。）

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_attachment_repo.py -q`
Expected: PASS（新测试 + 既有 create/get/mark_deleted 回归）。

- [ ] **Step 5: Commit**

```bash
git add core/storage/attachment_repo.py core/storage/tests/test_attachment_repo.py
git commit -m "feat(batch8a): add_ref/remove_ref 原子化——幂等 add + 归零条件软删"
```

---

### Task 14: 上传加固——kind size cap + 多文件孤儿清理（B 簇 minor）

**Files:**
- Modify: `core/input/api_routes.py:639`（kind 字段 read 限长）、`:650-720`（先全部校验再写盘 + 失败清理）
- Test: `core/input/tests/test_upload.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.service
async def test_upload_kind_field_size_capped(make_upload_app, aiohttp_client):
    # kind 字段塞超长 → 拒绝（注：超长 kind 会先挂 step0 未知 kind 400——此测试
    # 验证的是「不读入超长字段内存」的限长分支，用合法 kind 值但超长后缀断言快速 4xx，
    # 或直接对 read(65) 实现后的 400 分支断言，M8）
    ...

@pytest.mark.service
async def test_upload_second_file_invalid_no_orphan(make_upload_app, aiohttp_client, tmp_path):
    # 第 2 个文件魔数非法 → 第 1 个文件不残留（目录为空 + repo 无记录）
    ...
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL——当前 kind 无上限；第 1 个文件已写盘残留。

- [ ] **Step 3: 实现**

- kind 字段：`raw = await field.read(65)`，`if len(raw) > 64: return _error_response("INVALID_REQUEST", "kind 字段超长", 400)`（read(65) 才能检出 >64 超长；read(64) 恒返回 ≤64 字节）。
- 多文件：把「校验 + 写盘 + 建记录」拆成两阶段——先全部 `_validate_upload`（魔数/尺寸/kind），通过后再逐文件 `_persist_upload`（写盘 + repo.create + resolver.register）；`_persist_upload` 失败 → try/except 清理本轮已写文件 + 已建记录（unlink + repo 软删/删除）。失败清理用 `attachment_resolver.unregister` 同步。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_upload.py -q`
Expected: PASS（新测试 + 既有上传成功/超限/魔数拒绝全回归）。

- [ ] **Step 5: Commit**

```bash
git add core/input/api_routes.py core/input/tests/test_upload.py
git commit -m "feat(batch8a): 上传加固——kind 限长 + 多文件失败清理"
```

---

### Task 15: 探活 request_id 副作用（A 簇 minor）

**Files:**
- Modify: `core/input/server.py:184`（health/ready handler）
- Test: `core/input/tests/test_health.py`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.service
async def test_health_does_not_advance_request_counter(make_health_app, aiohttp_client, monkeypatch):
    # 注入 next_request_id 计数 → 多次 /health 后计数不变
    calls = {"n": 0}
    orig = agent.next_request_id
    def spy(): calls["n"] += 1; return orig()
    monkeypatch.setattr(agent, "next_request_id", spy)
    for _ in range(3):
        await client.get("/health")
    assert calls["n"] == 0
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL——当前 health/ready 调用 next_request_id。

- [ ] **Step 3: 实现**

`/health` 与 `/ready` handler 去掉 `agent.next_request_id()` 调用；返回的 `request_id` 用独立 uuid 或省略（确认 `ServerInfoMessage`/health 响应结构是否带该字段——若带，改用 `req_<uuid>` 不触碰计数器）。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_health.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/server.py core/input/tests/test_health.py
git commit -m "feat(batch8a): /health //ready 探活不再扰动 request_id 计数器"
```

---

## Self-Review

**1. Spec coverage（对照 spec §3.1-3.17）**：D1→T1、D2→T2、D3→T3、D4→T4、D5→T5、SSRF fec0→T6、认证日志→T7、GC 日志+unlink→T8、异常文本→T9、Inner Voice→T10、Secret Store 锁→T11、LLM 初始 SSRF→T12、add/remove_ref 原子→T13、上传加固（kind cap + 孤儿）→T14、探活副作用→T15。**共 15 tasks，覆盖 20 条审计发现 + 3 项合规缺口**。spec §1「不做」清单（8b/8c 项）未触碰。find_context 残余、抢跑投递目标均在 spec §6 记录移交，plan 不处理（符合 spec）。

**2. 类型/接口一致性**：
- T2 `handle_status(..., *, sender_connection_id)`——T2 内部与调用点 ws_handler 同一 Task 实现，无跨 Task 断链。
- T5 `release_session_attachments` 公开名——archive.py 与 session_routes.py 同 Task 同步改。
- T13 `add_ref/remove_ref` 签名不变（只改实现）——既有调用方（ws_handler/archive/debug）零改动。
- `warn_if_unsecured` 签名变更（T1）——调用点 core/agent/server.py:371 同 Task 改，无其他调用点（已验证 grep）。

**3. 跨 Task 依赖**：T5（attachment_release 新模块）被 archive.py 与 session_routes 引用，均在 T5 内部完成；无 Task 依赖先前未实现的接口。执行顺序 T1→T15 串行即可（每 Task 独立提交，subagent 可并行 2-3 个无冲突 Task：T6/T7/T10/T11/T12 均单文件独立）。

**4. 风险**：T2 涉及 ws_handler 握手路径改动（每 v2 连接 conn_id），回归面最大——测试已列 `core/input/tests/test_server.py`（建连/断连/v1 回归）+ bridge 测试（含离线重连例外）；T5 提取 helper 涉及 debug 归档全链路，回归已列 archive 测试。T4 搜索 filter 改动 Mongo 查询，FakeDocumentDB 已支持 `$and`/`$elemMatch`/`$regex`（实测），测试用真实语义。

## Self-Review（v0.2 review 收口）

**Review 发现全部修复：**
- **T13**：filter 用 `attachment_id` 字段（非 `_id`）+ `_require_db()` 保离线语义 + remove 首跳 `refs` 条件防负数 + 并发测试替代重复测试（B1/I3）。
- **T14**：kind 限长改 `read(65)` 才能检出超长（B2）。
- **T12**：守卫移 `LLMFactory.create`（真实入口），删除不存在的 `create_initial_backend`（B3）。
- **T6**：公网地址改 `2001:4860:4860::8888`（`2001:db8::1` 在本环境 is_private=True）（B4）。
- **T2**：补 offline 重连重传例外（`resend_after_reconnect` 仅放行当前已连接 electron 提交的 offline 调用，且归属迁移到新 id），防破坏既有 offline→终态闭环（I2）。
- **T3**：测试改真枚举向量（合法 hex 错 HMAC + 不存在 id → 403），修前为 404；标注 STORAGE_UNAVAILABLE 语义变化（I1/M9）。
- **T1**：夹具加 auth_token 参数；补双 token 严格分离回归；`!= 401` 改 `== 200`；warn_if_unsecured 更新既有测试防语义反转（I4/M5/M10）。
- **T5**：测试接真实 `_make_agent_app()` 夹具 + 先播种消息再断言（I6）。
- **T8/T10**：caplog 显式 `at_level(INFO)`（I5）。
- **T4/T9/T7/T14**：`_seed` 既有模式、四信封点逐个触发、logger 已有、kind 限长测试注明（M1/M2/M4/M8）。

