# Batch 8c Implementation Plan — E/F 簇契约/信封/ID/杂项（19 条）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `docs/electron-backend-audit-batches0-7.md` 剩余 E/F 簇 19 条（3 important + 16 minor）：WS/REST 错误信封统一、HeartbeatAckMessage 死契约面、ID 回显、会话归属、desktop/tts/服务器 infra/脚本杂项。

**Architecture:** 契约层（frontend_contract）补充可选字段/改名/删死成员并重生成 TS 快照 → 发送点（voice_sessions/ws_handler/manager/websocket）透传已有参数或改模型化发射 → REST 边界（session_routes/api_routes/server）统一 ProtocolError 信封 → 前端消费点（chatStore/CharStatus/useWebSocket）同步 wire 键与可选链 → 服务器 infra（WS 计数、payload 中间件、版本缓存）按语义修正。

**Tech Stack:** Python 3.11+ / aiohttp / Pydantic ContractModel / pytest / Vue3+TS（vue-tsc 类型门禁）/ ruff。

## Global Constraints

- 禁相对导入（ruff TID252）；`__init__.py`/`__main__.py` 豁免。
- 敏感纪律：错误信封/日志不落正文、路径、密钥；`str(e)` 只进日志。
- 契约单源：改 `frontend_contract/` 后必须 `make gen-types`（重生成 TS）+ `make check-types`（漂移门禁）通过。
- 前端类型门禁：改 `frontend/src/` 后必须 `cd frontend && npx vue-tsc --noEmit` 通过。
- 新测试必须加 marker（unit/service），日常用 `make test-quick`（unit+service）。
- **测试 marker 纪律**：仓库无 pytest.ini 配 `asyncio_mode`（strict 默认）——新增 async 测试必须显式 `@pytest.mark.asyncio`（否则直接报「async def functions are not natively supported」）；缺 `unit`/`service` marker 的测试会被 `make test-quick`（`-m "unit or service"`）过滤排除。各文件惯例：test_voice_sessions.py/test_bridge.py/test_desktop_roundtrip.py/test_upload.py/test_output.py 有模块级 pytestmark（只需补 asyncio）；test_server.py/test_version.py/test_session_routes.py/test_limits.py/test_tts_voices.py 为 per-test marker（需显式补 service/unit + asyncio）。
- 错误码统一走协议文档 §9.1 目录（`CONTRACT_VALIDATION`/`AUTH_REQUIRED`/`AUTH_INVALID` 等），禁止裸错误。
- spec 决策：D1 移除 HeartbeatAckMessage；D2 `max_request_bytes=0` = 不限；D3 voices current 缺失追加；F15 v2 跳过 capabilities 不新增 server_info 重播；F19 by_model→byModel；F7 None 归属仅活动会话；F18 用 CONTRACT_VALIDATION。

---

### Task 1: WS 错误信封完整化（F1 + F18）

**Files:**
- Modify: `frontend_contract/ws_message.py:480-491`（ErrorMessage 加字段）
- Modify: `core/input/voice_sessions.py:136-144`（_emit_error 透传 retryable）
- Modify: `core/input/ws_handler.py:384-391`（_send_voice_error 透传）+ `:287-306`（decode/validation 补 code）
- Test: `frontend_contract/tests/test_ws_message.py`、`core/input/tests/test_voice_sessions.py`

**Interfaces:**
- Produces: `ErrorMessage` 新增 `retryable: Optional[bool]` / `details: Optional[dict[str, Any]]`（缺省 None，exclude_none 省略）。

- [ ] **Step 1: 写契约单测（失败）**

在 `frontend_contract/tests/test_ws_message.py` 末尾加：

```python
def test_error_message_retryable_details_wire():
    m = ErrorMessage(message="x", code="STT_UNAVAILABLE", retryable=True, details={"n": 1})
    d = m.wire_dict()
    assert d["retryable"] is True
    assert d["details"] == {"n": 1}


def test_error_message_retryable_omitted_when_none():
    d = ErrorMessage(message="x").wire_dict()
    assert "retryable" not in d
    assert "details" not in d
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest frontend_contract/tests/test_ws_message.py -q`
Expected: FAIL — `ErrorMessage.__init__() got an unexpected keyword argument 'retryable'`。

- [ ] **Step 3: 实现契约字段**

`ErrorMessage`（ws_message.py）加：

```python
    retryable: Optional[bool] = Field(default=None, description="是否可重试，可缺省")
    details: Optional[dict[str, Any]] = Field(default=None, description="公开安全详情，可缺省")
```

- [ ] **Step 4: 运行验证通过 + 既有契约测试不回归**

`python -m pytest frontend_contract/tests/test_ws_message.py -q` → PASS。
`python -m pytest frontend_contract/tests/ -q` → 全绿（新字段缺省省略不破坏既有断言）。

- [ ] **Step 5: 写发射点单测（失败）**

`core/input/tests/test_voice_sessions.py` 加（复用文件内 `make_manager`，其注入 `emit=async_emit(emitted)` 捕获帧；需 `@pytest.mark.asyncio`）：

```python
@pytest.mark.asyncio
async def test_error_frame_carries_retryable():
    """_emit_error(..., True) → error 帧带 retryable=true。"""
    mgr, emitted, _ = make_manager()
    await mgr._emit_error("STT_UNAVAILABLE", "STT 已关闭，录音丢弃", True)
    assert any(e.get("type") == "error" and e.get("code") == "STT_UNAVAILABLE"
               and e.get("retryable") is True for e in emitted)
```

- [ ] **Step 6: 运行验证失败**

`python -m pytest core/input/tests/test_voice_sessions.py -q -k error_frame_carries_retryable`
Expected: FAIL — 帧无 `retryable` 键。

- [ ] **Step 7: 实现 `_emit_error` 透传**

```python
        await self._emit(ErrorMessage(
            message=message, code=code, retryable=retryable,
            requestId=rid if rid and rid != "-" else None,
        ).wire_dict())
```

- [ ] **Step 8: 写 `_send_voice_error` 透传 + decode/validation code 单测（失败）**

`core/input/tests/test_voice_ws.py`（或既有 voice WS 测试文件）加：

```python
class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, d):
        self.sent.append(d)


async def test_send_voice_error_carries_retryable():
    """VoiceSessionError.retryable → _send_voice_error → wire 帧带 retryable。"""
    from core.input.ws_handler import _send_voice_error
    from core.input.voice_sessions import VoiceSessionError
    ws = _FakeWS()
    err = VoiceSessionError("STT_UNAVAILABLE", "转写失败", retryable=True)
    await _send_voice_error(ws, err)
    assert ws.sent[0]["code"] == "STT_UNAVAILABLE"
    assert ws.sent[0]["retryable"] is True
```

`core/input/tests/test_v2_ws.py` 加（复用文件内 `_make_agent_with`/`_UsageLLM`/`_drain`，与 `TestV2AckIdempotency` 同构）：

```python
    @pytest.mark.asyncio
    async def test_invalid_json_error_has_code(self, aiohttp_client):
        """非法 JSON → error 帧 code=CONTRACT_VALIDATION。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_str("{not json")
            # 建连后可能有 expression 心情帧残留——循环读到 error 帧为止
            d = None
            for _ in range(10):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d.get("type") == "error":
                    break
            assert d is not None and d["type"] == "error"
            assert d["code"] == "CONTRACT_VALIDATION"
            await ws.close()
        finally:
            await agent.stop()
```

`core/input/tests/test_voice_ws.py` 的 `test_send_voice_error_carries_retryable` 需 `@pytest.mark.asyncio`（若文件无模块级 marker）；`frontend_contract/tests/test_ws_message.py` 新增测试需 `@pytest.mark.unit`（make test-quick 用 `-m "unit or service"` 过滤，契约测试缺 marker 会被排除）。

- [ ] **Step 9: 运行验证失败**

两个新测试均 FAIL（无 retryable 键 / 无 code 键）。

- [ ] **Step 10: 实现 `_send_voice_error` + decode/validation code**

`_send_voice_error`（ws_handler.py:384-391）：
```python
        await ws.send_json(ErrorMessage(
            message=error.message, code=error.code,
            requestId=_current_request_id(),
            retryable=getattr(error, "retryable", None),
        ).wire_dict())
```

`ws_handler.py` 两处 ErrorMessage 构造补 `code="CONTRACT_VALIDATION"`：**JSONDecodeError 分支**（json.loads 的 except，约 :296-300）与 **ValidationError 内容校验分支**（约 :301-306）——按「except 后构造 ErrorMessage 处」模式枚举，勿依赖精确行号（批次内会漂移）。

- [ ] **Step 11: 运行验证通过**

`python -m pytest core/input/tests/test_voice_sessions.py core/input/tests/test_voice_ws.py core/input/tests/test_v2_ws.py frontend_contract/tests/test_ws_message.py -q` → 全绿。
`make lint` 通过。

- [ ] **Step 12: Commit**

```bash
git add frontend_contract/ws_message.py core/input/voice_sessions.py core/input/ws_handler.py frontend_contract/tests/test_ws_message.py core/input/tests/test_voice_sessions.py core/input/tests/test_voice_ws.py core/input/tests/test_v2_ws.py
git commit -m "fix(batch8c): F1+F18 WS error 信封完整化——ErrorMessage 补 retryable/details，_emit_error/_send_voice_error 透传，decode/validation 补 CONTRACT_VALIDATION code"
```

---

### Task 2: admin/debug 统一错误信封（F2）

**Files:**
- Modify: `core/input/server.py`（4 个 admin 端点认证失败）
- Modify: `core/input/debug_routes/__init__.py`（认证中间件）
- Modify: `core/input/debug_routes/archive.py`、`character.py`、`ltm.py`（全部裸 `{"error":...}`）
- Test: `core/input/tests/test_server.py`（或新 `test_admin_envelope.py`）

**Interfaces:**
- Consumes: `_protocol_error_response(code, message, status, retryable=False)`（server.py:105，ProtocolError 信封，`error`=message 兼容别名）。
- Produces: admin/debug 全部错误响应为 ProtocolError 信封。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_server.py` 加：

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_admin_unauthorized_unified_envelope(aiohttp_client):
    """无 token 调 /shutdown → 401 ProtocolError 信封（code/retryable/requestId/error 别名）。"""
    from core.input.server import create_app
    agent = _make_agent()  # 文件内既有 stub
    app = create_app(agent, serve_static=False, auth_token="secret")
    client = await aiohttp_client(app)
    resp = await client.post("/shutdown")
    assert resp.status == 401
    data = await resp.json()
    assert data["code"] == "AUTH_REQUIRED"
    assert data["retryable"] is False
    assert data["requestId"]
    assert data["error"] == data["message"]  # 兼容别名


@pytest.mark.service
@pytest.mark.asyncio
async def test_debug_ltm_bad_pagination_envelope(aiohttp_client):
    """debug 业务错误（非法 limit 参数）→ 信封含 code。"""
    from core.input.server import create_app
    agent = _make_agent()  # 文件内既有 stub
    app = create_app(agent, serve_static=False, auth_token="secret")
    client = await aiohttp_client(app)
    # debug 路由受 auth_token 保护——带正确 token 才进业务 handler
    headers = {"Authorization": "Bearer secret"}
    resp = await client.get("/debug/api/ltm?limit=abc", headers=headers)
    assert resp.status == 400
    data = await resp.json()
    assert data.get("code") == "CONTRACT_VALIDATION"
    assert data.get("error") == data.get("message")  # 兼容别名
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest core/input/tests/test_server.py -q -k envelope`
Expected: FAIL — 响应为 `{"error":"unauthorized"}` 无 `code` 键。

- [ ] **Step 3: 实现 server.py 四端点**

每个 `if not _check_auth(request):` 分支改为：

```python
        if not _check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
```

四处：`shutdown_handler`、`restart_handler`、`tools_handler`、`tool_reload_handler`。

- [ ] **Step 4: 实现 debug_routes 认证中间件**

`debug_routes/__init__.py` `_debug_auth_middleware` 中（已有 `code` 变量）：
```python
            if request.path.startswith("/debug/api") and not check_auth(request):
                code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
                msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
                logger.warning("认证失败 code=%s path=%s", code, request.path)
                from core.input.server import _protocol_error_response  # 懒导入绕环
                return _protocol_error_response(code, msg, 401)
```

- [ ] **Step 5: 实现 debug 业务错误全量转换**

grep `web.json_response({"error"` in `core/input/debug_routes/`（archive.py/character.py/ltm.py，现约 30 处）。逐处改为 `_protocol_error_response(code, message, status)`（懒导入同 Step 4）。code 映射：分页/参数校验 → `CONTRACT_VALIDATION`；资源缺失 → `NOT_FOUND`；其余按 §9.1 目录。**保留原 HTTP status 与 message**。

- [ ] **Step 6: 运行验证通过 + 回归**

`python -m pytest core/input/tests/test_server.py -q` 全绿；`core/input/tests/test_debug*`（如有）全绿；`make lint` 通过。

- [ ] **Step 7: Commit**

```bash
git add core/input/server.py core/input/debug_routes/ core/input/tests/test_server.py
git commit -m "fix(batch8c): F2 admin/debug 裸 error 统一 ProtocolError 信封——四 admin 端点 + debug 认证中间件 + debug 业务错误全量（懒导入绕环）"
```

---

### Task 3: 移除 HeartbeatAckMessage + 修 on_action/on_token_usage 绕过（F3）★

**Files:**
- Modify: `frontend_contract/ws_message.py`（删 HeartbeatAckMessage、ActionMessage.params→Optional）
- Modify: `frontend_contract/__init__.py`（删导出）
- Modify: `Module/output/websocket.py`（on_action/on_token_usage 模型化）
- Modify: `frontend/src/composables/useWebSocket.ts`（handleAction 可选链）
- Modify: `frontend_contract/tests/test_ws_message.py`（联合数 39→38）
- Test: `frontend_contract/tests/`、`Module/output/tests/`、vue-tsc

**Interfaces:**
- Produces: `ActionMessage.params: Optional[dict[str, Any]]`；`on_token_usage` 发 `TokenUsageMessage`（本轮用 TokenStats 既有 `by_model` 字段名——Task 15 再改 byModel）。

- [ ] **Step 1: 写失败测试（契约）**

`frontend_contract/tests/test_ws_message.py` 加（`_union_members` 从 `test_batch6_contract` 复用）：

```python
def test_heartbeat_ack_removed_from_union():
    from frontend_contract.tests.test_batch6_contract import _union_members
    members = _union_members()
    assert "HeartbeatAckMessage" not in members
    assert len(members) == 38


def test_action_message_params_optional_omitted_when_none():
    d = ActionMessage(action="tool_executing").wire_dict()
    assert "params" not in d
    d2 = ActionMessage(action="tool_executing", params={"tool": "t"}).wire_dict()
    assert d2["params"] == {"tool": "t"}
```

（既有 `test_ws_message.py:20` 的 `len(members) == 39` 同步改 38。）

- [ ] **Step 2: 运行验证失败**

`python -m pytest frontend_contract/tests/ -q`
Expected: FAIL — HeartbeatAck 仍在联合；ActionMessage(params=) 报多余关键字 / 空 params 强制 `{}`。

- [ ] **Step 3: 实现契约层**

`ws_message.py`：删除 `HeartbeatAckMessage` 类 + 从 ServerMessage 联合移除；`ActionMessage.params` → `Optional[dict[str, Any]] = Field(default=None)`。`__init__.py`：删 `HeartbeatAckMessage` import（:104）与 `__all__`（:225）。

- [ ] **Step 4: 运行验证通过（契约）**

`python -m pytest frontend_contract/tests/ -q` → 全绿。
`make gen-types`（TS 重新生成）。

- [ ] **Step 5: 写失败测试（发射点）**

`Module/output/tests/test_output.py` 加（`WebSocketChannel` 以假 ws 构造——`_send_json` 只调 `self._ws.send_json`；`_FakeWS` 在测试文件内定义，文件无既有同名类）：

```python
class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, d):
        self.sent.append(d)


@pytest.mark.asyncio
async def test_on_action_emits_modeled_message():
    from Module.output import WebSocketChannel
    ws = _FakeWS()
    ch = WebSocketChannel(ws, protocol_version=1)
    await ch.on_action("tool_executing", params={"tool": "t", "call_id": "c"})
    assert ws.sent[0]["type"] == "action"
    assert ws.sent[0]["params"] == {"tool": "t", "call_id": "c"}


@pytest.mark.asyncio
async def test_on_action_no_params_omits_key():
    from Module.output import WebSocketChannel
    ws = _FakeWS()
    ch = WebSocketChannel(ws, protocol_version=1)
    await ch.on_action("wave")
    assert ws.sent[0]["type"] == "action"
    assert "params" not in ws.sent[0]


@pytest.mark.asyncio
async def test_on_token_usage_emits_modeled_message():
    from Module.output import WebSocketChannel
    ws = _FakeWS()
    ch = WebSocketChannel(ws, protocol_version=1)
    data = {"turn": {"rounds": [{"model": "m", "input_tokens": 1, "output_tokens": 2,
                                  "total_tokens": 3, "round_num": 0,
                                  "cached_tokens": None, "reasoning_tokens": None}],
                     "turn_total": 3},
            "session": {"total": 3, "by_model": {"m": 3}}}
    await ch.on_token_usage(data)
    assert ws.sent[0]["type"] == "token_usage"
    assert ws.sent[0]["session"]["by_model"] == {"m": 3}   # 本轮保留 by_model（Task 15 改）
    assert "cached_tokens" not in ws.sent[0]["turn"]["rounds"][0]  # null 键省略 → 旧裸 dict 路径红
```

（**红因说明**：旧裸 dict 实现 `{"type":"token_usage", **data}` 直通，`cached_tokens: None` 会以显式 null 上 wire → `"cached_tokens" in ...` 为真 → 测试红；模型化 TokenUsageMessage 经 exclude_none 省略 → 绿。on_action 两测试是行为回归守卫——旧裸 dict 与新模型化发射 wire 行为等价，实现前即绿，真正契约变更由 Step 1 的 ActionMessage.params 单测覆盖。）

- [ ] **Step 6: 运行验证失败**

`python -m pytest Module/output/tests/ -q -k "on_action or on_token_usage"`
Expected: FAIL — 发射仍为裸 dict（无契约形状）。

- [ ] **Step 7: 实现发射点**

`on_action`（websocket.py:113-125）：
```python
        await self._send_json(ActionMessage(action=action, params=params).wire_dict())
```
`on_token_usage`（:186-196）——**注意用 `by_model=`（TokenStats 本轮字段名仍是 by_model，spec §3.3 片段里的 `byModel=` 是 Task 15 之后的样子，勿照抄）**：
```python
        turn = data.get("turn") or {}
        session = data.get("session") or {}
        await self._send_json(TokenUsageMessage(
            turn=TokenTurnStats(
                rounds=[TokenRound.model_validate(r) for r in (turn.get("rounds") or [])],
                turn_total=turn.get("turn_total"),
            ) if turn else None,
            session=TokenStats(
                total=session.get("total", 0),
                by_model=session.get("by_model", {}),
            ) if session else None,
        ).wire_dict())
```
删除两处旧注释（"注：暂不切 ActionMessage..." / "注：暂不切 TokenUsageMessage..."）。

- [ ] **Step 8: 运行验证通过**

`python -m pytest Module/output/tests/ -q -k "on_action or on_token_usage"` → 全绿。
`python -m pytest frontend_contract/tests/ -q` → 全绿。

- [ ] **Step 9: 前端类型同步**

`frontend/src/composables/useWebSocket.ts` `handleAction`（:96-101）改可选链：
```ts
    if (msg.action === 'tool_executing')
      chat.addToolCard(msg.params?.tool as string, msg.params?.call_id as string, msg.params?.args as Record<string, unknown> | undefined)
    else if (msg.action === 'tool_done')
      chat.updateToolCard(msg.params?.call_id as string, msg.params as any)
```
验证：`cd frontend && npx vue-tsc --noEmit` 通过（改前预期 TS2532，改后通过——可先跑一遍确认改前即失败）。

- [ ] **Step 10: 门禁**

`make check-types` 通过；`make lint` 通过。

- [ ] **Step 11: Commit**

```bash
git add frontend_contract/ws_message.py frontend_contract/__init__.py Module/output/websocket.py frontend/src/composables/useWebSocket.ts frontend_contract/tests/ Module/output/tests/ frontend/src/types/contract.generated.ts
git commit -m "fix(batch8c): F3 移除 HeartbeatAckMessage 死契约面 + on_action/on_token_usage 模型化发射（ActionMessage.params 可选，前端 handleAction 可选链）"
```

---

### Task 4: generation.started.requestId 回显（F4）

**Files:**
- Modify: `Module/output/manager.py:346-356`（turn_started）
- Test: `core/cognitive/tests/test_v2_generation.py`

**Interfaces:**
- Consumes: `ctx.request_id`（GenerationContext.request_id，generation.py:41）。
- Produces: `generation.started` 帧 `requestId` = 该 turn 的 request_id。

- [ ] **Step 1: 写失败测试**

`core/cognitive/tests/test_v2_generation.py`（复用文件内 `_make_agent_with`/`_UsageLLM`/`_send_and_collect` 基建）：

```python
    @pytest.mark.asyncio
    async def test_generation_started_echoes_request_id(self, aiohttp_client):
        """v2 generation.started 帧 requestId 非空（回显服务端 request_id，非 clientRequestId）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001 空会话
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            rid = f"req_{time.monotonic_ns()}"
            frames = await _send_and_collect(
                client, {"X-Protocol-Version": "2"}, "你好", client_request_id=rid)
            started = next(f for f in frames if f.get("type") == "generation.started")
            # requestId 数据源 = 服务端 next_request_id()（"001" 递增计数，agent.py:336
            # → UserInputData.request_id → ctx.request_id），与 clientRequestId 无关。
            assert started.get("requestId"), "generation.started 应带非空 requestId"
            assert started["requestId"] != rid  # 不是 clientRequestId 回显
        finally:
            await agent.stop()
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest core/cognitive/tests/test_v2_generation.py -q -k request_id`
Expected: FAIL — `started.get("requestId")` 为 None。

- [ ] **Step 3: 实现**

`turn_started`（manager.py:346-356）广播参数补 `request_id=ctx.request_id`：
```python
        await self._broadcast(
            "on_generation_started",
            generation_id=ctx.generation_id, message_id=ctx.assistant_message_id,
            conversation_id=ctx.conversation_id,
            request_id=ctx.request_id,
            model=ctx.model_id or None,
            channels=v2,
        )
```
（核对 `on_generation_started` 接收方已把 `request_id` 传给 `GenerationStartedMessage.requestId`；若缺则同步补。）

- [ ] **Step 4: 运行验证通过**

`python -m pytest core/cognitive/tests/test_v2_generation.py -q -k request_id` → 全绿。
`python -m pytest core/cognitive/tests/test_v2_generation.py -q` → 回归全绿。

- [ ] **Step 5: Commit**

```bash
git add Module/output/manager.py core/cognitive/tests/test_v2_generation.py
git commit -m "fix(batch8c): F4 generation.started 回显 requestId（ctx.request_id 透传）"
```

---

### Task 5: session 路由——畸形 cursor 400 + pending 归属（F5 + F7）

**Files:**
- Modify: `core/input/session_routes.py`
- Test: `core/input/tests/test_session_routes.py`

**Interfaces:**
- Consumes: `manager.list(cursor=...)`（畸形 cursor 抛 ValueError）、`agent.session_manager.active_id`、`agent.pending_messages`（`_PendingMessage.conversation_id`）。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_session_routes.py` 加（复用文件内 `_FakeAgent`/`_make_app`/`_create_session`/`TestPending._pending`）：

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_sessions_list_malformed_cursor_400(self, aiohttp_client):
    """畸形 cursor → 400 CONTRACT_VALIDATION（非 500）。"""
    agent = _FakeAgent()
    app = _make_app(agent)
    client = await aiohttp_client(app)
    resp = await client.get("/api/sessions?cursor=garbage")
    assert resp.status == 400
    data = await resp.json()
    assert data["code"] == "CONTRACT_VALIDATION"


@pytest.mark.service
@pytest.mark.asyncio
async def test_pending_none_conversation_only_active_session(self, aiohttp_client):
    """conversation_id=None（v1/未归属）pending 仅活动会话展示，非活动 sid 不展示。"""
    agent = _FakeAgent()
    await _create_session(agent, "conv_active")
    await _create_session(agent, "conv_other")
    # 活动会话 = conv_active（_create_session 首个创建默认 active；必要时显式 switch）
    await agent.switch_session("conv_active")
    agent.pending_messages = [
        self._pending("未归属消息", "pm_none", None),
        self._pending("other 会话消息", "pm_other", "conv_other"),
    ]
    app = _make_app(agent)
    client = await aiohttp_client(app)

    resp = await client.get("/api/sessions/conv_active/pending")
    data = await resp.json()
    ids_active = [m["messageId"] for m in data["pendingMessages"]]
    assert "pm_none" in ids_active        # None 归属 → 活动会话可见

    resp2 = await client.get("/api/sessions/conv_other/pending")
    data2 = await resp2.json()
    ids_other = [m["messageId"] for m in data2["pendingMessages"]]
    assert "pm_other" in ids_other
    assert "pm_none" not in ids_other     # None 归属不在非活动会话
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest core/input/tests/test_session_routes.py -q -k "malformed_cursor or none_conversation"`
Expected: FAIL — 畸形 cursor 500（裸异常）；`pm_none` 在非活动会话也出现。

- [ ] **Step 3: 实现 cursor 400**

`sessions_list_handler` 包 try/except：
```python
    try:
        items, has_more, next_cursor = await manager.list(cursor=cursor, limit=limit, search=search)
    except ValueError:
        return _error_response("CONTRACT_VALIDATION", "cursor 格式非法")
```

- [ ] **Step 4: 实现 pending 归属**

`sessions_pending_handler` 过滤循环改为：
```python
    active_id = getattr(getattr(agent, "session_manager", None), "active_id", None)
    for pm in getattr(agent, "pending_messages", None) or []:
        cid = pm.conversation_id
        effective = str(cid) if cid is not None else (str(active_id) if active_id else None)
        if effective != str(sid):
            continue
        ...
```

- [ ] **Step 5: 运行验证通过 + 回归**

`python -m pytest core/input/tests/test_session_routes.py -q` 全绿。

- [ ] **Step 6: Commit**

```bash
git add core/input/session_routes.py core/input/tests/test_session_routes.py
git commit -m "fix(batch8c): F5+F7 session 路由——畸形 cursor → 400 CONTRACT_VALIDATION；None 归属 pending 仅活动会话展示（active_id 空防御）"
```

---

### Task 6: generation 持久化 error 结构化（F6）

**Files:**
- Modify: `core/cognitive/generation_registry.py:96-130`（mark error 参数）
- Modify: `core/cognitive/loop.py:538`（错误路径传 dict）
- Test: `core/cognitive/tests/test_v2_generation.py`（或既有 generation 测试）

**Interfaces:**
- Consumes: loop 错误路径已有 `code`/`retryable` 变量。
- Produces: `mark(..., error={"code","message","retryable"})` → 存储 doc `error` 为 dict；`_generation_status`（session_routes.py:670）已透传 dict。

- [ ] **Step 1: 写失败测试**

`core/cognitive/tests/test_v2_generation.py`（复用 `_make_agent_with`/`_UsageLLM(fault="http_503")`/`_registry_docs`）：

```python
    @pytest.mark.asyncio
    async def test_generation_failed_error_is_structured(self, aiohttp_client):
        """LLM 503 → generation 失败，注册表 doc error 为 {code,message,retryable} dict。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM(fault="http_503"))
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001 空会话
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await _send_and_collect(
                client, {"X-Protocol-Version": "2"}, "你好",
                client_request_id=f"req_{time.monotonic_ns()}")  # 缺 clientRequestId 会被拒绝，无 generation
            docs = await _registry_docs(agent)
            failed = next(d for d in docs if d.get("status") == "failed")
            err = failed["error"]
            assert isinstance(err, dict)
            assert "code" in err and "message" in err and "retryable" in err
        finally:
            await agent.stop()
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest core/cognitive/tests/test_v2_generation.py -q -k failed_error_is_structured`
Expected: FAIL — `failed["error"]` 为字符串（`str(e)[:200]`）。

- [ ] **Step 3: 实现**

`generation_registry.py` `mark` 签名：`error: str | dict | None = None`，`if error is not None: doc["error"] = error`。
`loop.py:538` 附近：
```python
                if self._generation_registry is not None:
                    try:
                        await self._generation_registry.mark(
                            self._generation_ctx.generation_id,
                            status="failed", finish_reason="error",
                            error={"code": code, "message": err_msg, "retryable": retryable})
                    except Exception:
                        ...
```

- [ ] **Step 4: 运行验证通过 + 回归**

`python -m pytest core/cognitive/tests/test_v2_generation.py -q` 全绿；`core/input/tests/test_session_routes.py -q -k generation` 全绿。

- [ ] **Step 5: Commit**

```bash
git add core/cognitive/generation_registry.py core/cognitive/loop.py core/cognitive/tests/
git commit -m "fix(batch8c): F6 generation 持久化 error 结构化 {code,message,retryable}（mark 接受 dict）"
```

---

### Task 7: desktop 簇——llm_summary 回退 + tools/calls camelCase（F8 + F9）

**Files:**
- Modify: `core/desktop/bridge.py:449-458`（_map_terminal content 回退）
- Modify: `core/input/api_routes.py:895-909`（_desktop_call_to_status result 归一）
- Test: `core/desktop/tests/test_bridge.py`、`core/input/tests/test_upload.py`（或 tools/calls 测试文件）

**Interfaces:**
- Consumes: `ToolStatusMessage.output/executionSummary`、repo doc `result`（snake 键）。
- Produces: `ToolResult.content` 空时回退 summary；REST `DesktopCallStatus.result` 键为 camelCase。

- [ ] **Step 1: 写失败测试（F8）**

`core/desktop/tests/test_bridge.py` 加：

```python
def test_map_terminal_empty_output_falls_back_to_summary():
    msg = ToolStatusMessage(toolName="desktop.screen.capture", status=ToolStatusEnum.RESULT,
                            callId="call_1", toolVersion="1", runtime="desktop", paramHash="h",
                            output="", executionSummary="截图已保存")
    _s, result_field, _e, tool_result = DesktopToolBridge._map_terminal(msg)
    assert tool_result.content == "截图已保存"
    assert tool_result.llm_summary == "截图已保存"


def test_map_terminal_nonempty_output_keeps_output():
    msg = ToolStatusMessage(toolName="t", status=ToolStatusEnum.RESULT,
                            callId="call_1", toolVersion="1", runtime="desktop", paramHash="h",
                            output="o", executionSummary="s")
    _s, _rf, _e, tool_result = DesktopToolBridge._map_terminal(msg)
    assert tool_result.content == "o"
```

- [ ] **Step 2: 运行验证失败（F8）**

`python -m pytest core/desktop/tests/test_bridge.py -q -k map_terminal`
Expected: FAIL — content == ""。

- [ ] **Step 3: 实现 F8**

`_map_terminal`：
```python
            content = msg.output or (msg.executionSummary or "")
            llm_summary = msg.executionSummary or content
```

- [ ] **Step 4: 写失败测试（F9）**

`core/input/tests/test_desktop_roundtrip.py` 加（`_make_agent_with` 已注入 fake desktop repo，`upsert` 直接写 snake 键 doc，GET 端点验证归一）：

```python
@pytest.mark.asyncio
async def test_tools_call_result_camel_case(aiohttp_client):
    """GET /api/tools/calls/{id} result 键归 camelCase（executionSummary/mimeType）。"""
    from core.input.server import create_app
    from core.cognitive.tests.test_v2_generation import _make_agent_with, _UsageLLM

    agent = _make_agent_with(_UsageLLM())
    await agent.start()
    await agent._desktop_call_repo.upsert({  # noqa: SLF001
        "call_id": "call_1", "tool_name": "t", "tool_version": "1", "runtime": "desktop",
        "arguments_hash": "h", "status": "succeeded", "created_at": "t", "expires_at": "t2",
        "result": {"output": "o", "execution_summary": "s",
                   "attachments": [{"url": "u", "mime_type": "image/png", "label": "l"}]},
    })
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        resp = await client.get("/api/tools/calls/call_1")
        data = await resp.json()
        assert data["result"]["executionSummary"] == "s"
        assert data["result"]["attachments"][0]["mimeType"] == "image/png"
    finally:
        await agent.stop()
```

- [ ] **Step 5: 运行验证失败（F9）**

Expected: FAIL — result 键为 execution_summary/mime_type。

- [ ] **Step 6: 实现 F9**

`_desktop_call_to_status` 中 result 归一：
```python
        result = doc.get("result")
        if isinstance(result, dict):
            result = dict(result)
            if "execution_summary" in result:
                result["executionSummary"] = result.pop("execution_summary")
            attachments = result.get("attachments")
            if isinstance(attachments, list):
                result["attachments"] = [
                    {"url": a.get("url"),
                     "mimeType": a.get("mime_type", a.get("mimeType")),
                     "label": a.get("label")}
                    for a in attachments if isinstance(a, dict)
                ]
```
`rest.py` `DesktopCallStatus.result` docstring：`{output,attachments,executionSummary}`。

- [ ] **Step 7: 运行验证通过**

`python -m pytest core/desktop/tests/test_bridge.py core/input/tests/test_upload.py -q` 全绿。

- [ ] **Step 8: Commit**

```bash
git add core/desktop/bridge.py core/input/api_routes.py frontend_contract/rest.py core/desktop/tests/ core/input/tests/
git commit -m "fix(batch8c): F8+F9 desktop 簇——output 空时 content 回退 executionSummary；tools/calls result 归一 camelCase（mimeType/executionSummary）"
```

---

### Task 8: voices current 缺失追加（F10）

**Files:**
- Modify: `core/input/api_routes.py:454-478`（tts_voices_handler）
- Test: `core/input/tests/test_tts_voices.py`

**Interfaces:**
- Consumes: `_active_tts_profile`、`list_voices()`（list[dict]）、`TtsVoiceEntry(id,name,...)`。
- Produces: `current` 恒 ∈ `voices[]`。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_tts_voices.py` 加（复用 `_FakeBackend`/`_make_app_agent`；`current` 经 `service_state.tts_voice` 显式注入，避免依赖 character 名大小写）：

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_voices_current_not_in_list_gets_appended(aiohttp_client):
    agent = _make_app_agent()
    agent._tts = _FakeBackend([{"id": "alloy"}], provider_name="openai")  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    agent.session.service_state.tts_voice = "elysia"  # noqa: SLF001 — 注入 current（不在列表）
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/api/tts/voices")
    data = await resp.json()
    ids = [v["id"] for v in data["voices"]]
    assert data["current"] == "elysia"
    assert "elysia" in ids         # 追加保证 current 在列表
    assert len(ids) == 2           # 原 1 + 追加 1


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_voices_current_in_list_not_duplicated(aiohttp_client):
    agent = _make_app_agent()
    agent._tts = _FakeBackend([{"id": "elysia"}], provider_name="openai")  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    agent.session.service_state.tts_voice = "elysia"  # noqa: SLF001 — current 已在列表
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/api/tts/voices")
    data = await resp.json()
    ids = [v["id"] for v in data["voices"]]
    assert data["current"] == "elysia"
    assert ids.count("elysia") == 1   # 不重复追加
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest core/input/tests/test_tts_voices.py -q -k append`
Expected: FAIL — current 不在列表。

- [ ] **Step 3: 实现**

`tts_voices_handler` 返回前：
```python
        current = getattr(agent.session.service_state, "tts_voice", None) \
            or _backend_default_voice(backend, character)
        if current and not any(v.get("id") == current for v in voices):
            voices.append(rest_contract.TtsVoiceEntry(id=current, name=current))
```

- [ ] **Step 4: 运行验证通过 + 回归**

`python -m pytest core/input/tests/test_tts_voices.py -q` 全绿。

- [ ] **Step 5: Commit**

```bash
git add core/input/api_routes.py core/input/tests/test_tts_voices.py
git commit -m "fix(batch8c): F10 voices current 缺失时追加到列表（current 恒 ∈ voices[]）"
```

---

### Task 9: trace 脚本——_ENV_RE 接入 + drain 静默断（F11 + F12）

**Files:**
- Modify: `scripts/capture_ws_trace.py`（_sanitize + drain 循环）
- Create: `scripts/tests/test_capture_ws_trace.py`（marker=unit）

**Interfaces:**
- Produces: `_sanitize(line)` 剥 `KEY=value`；drain 异常打印 + 截断标记。

- [ ] **Step 1: 写失败测试**

`scripts/tests/test_capture_ws_trace.py`：

```python
import pytest

pytestmark = pytest.mark.unit


def test_sanitize_scrubs_env_values():
    from scripts.capture_ws_trace import _sanitize
    line = 'export OPENAI_API_KEY=sk-abc123 more'
    assert "sk-abc123" not in _sanitize(line)
    assert _sanitize(line).startswith("export OPENAI_API_KEY=***")


def test_sanitize_keeps_path_scrub():
    from scripts.capture_ws_trace import _sanitize
    assert "$SANITIZED_PATH" in _sanitize("path /home/user/secret")


@pytest.mark.asyncio
async def test_drain_reports_truncation_on_exception():
    """mock ws.receive_json 抛异常 → drain 返回截断标记而非静默。"""
    from scripts.capture_ws_trace import _drain_connect_frames  # 本任务提取的 async helper

    class _BadWS:
        async def receive_json(self):
            raise ConnectionResetError("reset")

    frames, truncated = await _drain_connect_frames(_BadWS(), [], max_connect=5)
    assert truncated is True
    assert frames == []
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest scripts/tests/test_capture_ws_trace.py -q`
Expected: FAIL — `_sanitize` 未剥 env 值（`_ENV_RE` 未用）；`_drain_connect_frames` 不存在。

- [ ] **Step 3: 实现 _sanitize**

`capture_ws_trace.py` 把 `_ENV_RE` 改为可剥值并接入：
```python
_ENV_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=(?:[^\s\"',}]+|\"[^\"]*\"|'[^']*')")

def _sanitize(line: str) -> str:
    line = _PATH_RE.sub("$SANITIZED_PATH", line)
    line = _ENV_RE.sub(lambda m: f"{m.group(1)}=***", line)
    line = line.replace("test-key-placeholder", "***")
    return line
```

- [ ] **Step 4: 实现 drain helper + 截断报告**

提取 drain 为 async helper 并接入脚本（脚本调用处 `frames, truncated = await _drain_connect_frames(ws, frames)`；主流程末尾若 `truncated` 打印警示）：

```python
async def _drain_connect_frames(ws, frames: list[str], *, max_connect: int = 5
                                ) -> tuple[list[str], bool]:
    """排空建连帧；异常（连接被 reset/EOF）→ 返回 (frames, truncated=True) 不静默。

    返回 (frames, truncated)：truncated 表示连接异常截断（非正常 timeout 结束）。
    """
    truncated = False
    for _ in range(max_connect):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
        except asyncio.TimeoutError:
            break
        except Exception as e:
            truncated = True
            print(f"[capture] 连接/握手失败，轨迹可能截断: {e}", file=sys.stderr)
            break
        frames.append(json.dumps(msg, ensure_ascii=False))
        if msg.get("type") == "capabilities":
            break
    return frames, truncated
```

（drain 调用处原 `for _ in range(5): try ... except (asyncio.TimeoutError, Exception): break` 整块替换为本 helper 调用；`Exception` 裸捕获改为上面的区分处理，timeout 仍视为正常结束。）

- [ ] **Step 5: 运行验证通过**

`python -m pytest scripts/tests/test_capture_ws_trace.py -q` → 全绿。
（`scripts/` 不在 `make lint` 的 ruff 范围 `core/ Module/ frontend_contract/` 内，无需 lint。）

- [ ] **Step 6: Commit**

```bash
git add scripts/capture_ws_trace.py scripts/tests/test_capture_ws_trace.py
git commit -m "fix(batch8c): F11+F12 trace 脚本——_ENV_RE 接入 _sanitize 剥 env 值；drain 异常打印 + 截断标记不静默"
```

---

### Task 10: chunked 413 兜底生效（F13）

**Files:**
- Modify: `core/input/api_routes.py`、`core/input/session_routes.py`、`core/input/debug_routes/`
- Test: `core/input/tests/test_upload.py`、`core/input/tests/test_limits.py`

**Interfaces:**
- Consumes: 中间件 `_make_payload_middleware`（server.py:148，已含 `except web.HTTPRequestEntityTooLarge` → 413 信封）。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_upload.py` 加（aiohttp client 对 async generator data 强制 chunked 传输，无 Content-Length → 走 handler 读体路径）：

```python
async def _chunked_json():
    yield b'{"content": "'
    yield b"x" * 5000
    yield b'"}'

@pytest.mark.asyncio
async def test_chat_chunked_overlimit_413_envelope(aiohttp_client):
    """chunked（无 Content-Length）超限 json → 413 REQUEST_TOO_LARGE（非 400）。"""
    from unittest.mock import MagicMock
    from core.agent import RunState
    from core.input.server import create_app

    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    agent._state = RunState.RUNNING
    agent.process_message = None
    app = create_app(agent, serve_static=False, max_request_bytes=100)
    client = await aiohttp_client(app)
    resp = await client.post("/api/chat", data=_chunked_json(),
                             headers={"Content-Type": "application/json"})
    assert resp.status == 413
    assert (await resp.json())["code"] == "REQUEST_TOO_LARGE"
```

覆盖代表端点：api_routes chat_api_handler（json，上式）+ upload_handler（multipart 同理用 async generator 喂超限字段）+ session_routes sessions_create（json）+ debug_routes ltm 一处（json）。

- [ ] **Step 2: 运行验证失败**

`python -m pytest core/input/tests/test_upload.py core/input/tests/test_limits.py -q -k chunked`
Expected: FAIL — 400（handler 宽 except 吞 HTTPRequestEntityTooLarge）而非 413。

- [ ] **Step 3: 实现**

grep `await request.json()` / `await request.multipart()` 外包 `except Exception` 的位置（api_routes 7 处 + session_routes 2 处 + debug_routes 5 处，按 spec §3.13 枚举），每个宽 `except Exception` 之前补：
```python
        except web.HTTPRequestEntityTooLarge:
            raise
```

- [ ] **Step 4: 运行验证通过 + 回归**

`python -m pytest core/input/tests/test_upload.py core/input/tests/test_limits.py -q` 全绿；`core/input/tests/test_session_routes.py -q`、debug 相关测试全绿。

- [ ] **Step 5: Commit**

```bash
git add core/input/api_routes.py core/input/session_routes.py core/input/debug_routes/ core/input/tests/test_upload.py core/input/tests/test_limits.py
git commit -m "fix(batch8c): F13 chunked 413 兜底生效——body 读取 handler 宽 except 前补 HTTPRequestEntityTooLarge: raise"
```

---

### Task 11: 并发 WS 硬上限（F14）

**Files:**
- Modify: `core/input/server.py:320-338`（ws_handler）
- Test: `core/input/tests/test_limits.py`

**Interfaces:**
- Produces: create_app 闭包 `_ws_active` 计数；检查+自增原子，连接关闭 finally 自减。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_limits.py` 加：

```python
async def _open_ws(client):
    """打开 WS 连接（成功返回 ws，超限 503 返回 None）。"""
    import aiohttp
    try:
        return await client.session.ws_connect(client.make_url("/ws"))
    except aiohttp.WSServerHandshakeError:
        return None


@pytest.mark.service
@pytest.mark.asyncio
async def test_ws_concurrent_handshake_hard_limit(aiohttp_client):
    """max=1 下并发两个握手 → 恰一个成功（硬上限非 TOCTOU 预检）。"""
    from core.input.server import create_app

    agent = _make_agent()
    await agent.start()
    app = create_app(agent, serve_static=False, max_ws_connections=1)
    client = await aiohttp_client(app)
    try:
        # 两连接保持打开到两个握手都结束（避免首个 close 释放槽位引入竞态）
        ws1, ws2 = await asyncio.gather(_open_ws(client), _open_ws(client))
        ok = sum(1 for ws in (ws1, ws2) if ws is not None)
        assert ok == 1
        for ws in (ws1, ws2):
            if ws is not None:
                await ws.close()
    finally:
        await agent.stop()
```

- [ ] **Step 2: 运行验证失败**

Expected: 现状为预检——并发可能 2 个都过（TOCTOU），或计数含已关闭 channel 导致 0/过低；测试应稳定得恰 1 个。

- [ ] **Step 3: 实现**

create_app 内加闭包计数，ws_handler 改为：
```python
    _ws_active = 0

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        nonlocal _ws_active
        if max_ws_connections and max_ws_connections > 0:
            if _ws_active >= max_ws_connections:
                return _protocol_error_response("CONNECTION_LIMITED", "并发连接超限", 503, retryable=True)
            _ws_active += 1
            try:
                return await handle_ws(request, agent, wake_word_enabled)
            finally:
                _ws_active -= 1
        return await handle_ws(request, agent, wake_word_enabled)
```

- [ ] **Step 4: 运行验证通过 + 回归**

`python -m pytest core/input/tests/test_limits.py -q` 全绿（既有 test_ws_over_limit_rejected_503 也过）。

- [ ] **Step 5: Commit**

```bash
git add core/input/server.py core/input/tests/test_limits.py
git commit -m "fix(batch8c): F14 并发 WS 硬上限——闭包计数 check+自增原子 + 连接关闭自减（修 TOCTOU 与陈旧计数）"
```

---

### Task 12: v2 跳过 capabilities 广播（F15）

**Files:**
- Modify: `core/input/capabilities.py:109-125`（broadcast_capabilities）
- Test: `core/input/tests/test_desktop_roundtrip.py`

**Interfaces:**
- Consumes: `ch.protocol_version`（WebSocketChannel，websocket.py:58）。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_desktop_roundtrip.py` 加（真实 Agent + 假 ws 通道；`WebSocketChannel._send_json` 只调 `self._ws.send_json`，假 ws 可捕获）：

```python
async def test_broadcast_capabilities_skips_v2():
    from core.input.capabilities import broadcast_capabilities
    from core.cognitive.tests.test_v2_generation import _make_agent_with, _UsageLLM
    from Module.output import WebSocketChannel

    class _FakeWS:
        def __init__(self):
            self.sent = []

        async def send_json(self, d):
            self.sent.append(d)

    agent = _make_agent_with(_UsageLLM())
    v1 = WebSocketChannel(_FakeWS(), protocol_version=1)
    v2 = WebSocketChannel(_FakeWS(), protocol_version=2)
    agent.output._channels = [v1, v2]  # noqa: SLF001
    await broadcast_capabilities(agent)
    assert len(v1._ws.sent) == 1   # v1 收到 capabilities
    assert len(v2._ws.sent) == 0   # v2 跳过
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest core/input/tests/test_desktop_roundtrip.py -q -k broadcast_capabilities`
Expected: FAIL — `v2._ws.sent` 非空（v2 也收到）。

- [ ] **Step 3: 实现**

```python
        for ch in agent.output._channels:  # noqa: SLF001
            if isinstance(ch, WebSocketChannel) and getattr(ch, "protocol_version", 1) < 2:
                await ch._send_json(payload.wire_dict())  # noqa: SLF001
```

- [ ] **Step 4: 运行验证通过 + 回归**

`python -m pytest core/input/tests/test_desktop_roundtrip.py core/input/tests/test_version.py -q` 全绿（v1 建连仍收 capabilities）。

- [ ] **Step 5: Commit**

```bash
git add core/input/capabilities.py core/input/tests/
git commit -m "fix(batch8c): F15 broadcast_capabilities 跳过 v2 通道（v1 保留重播，v2 用 server_info 取代）"
```

---

### Task 13: max_request_bytes=0 不限（F16）

**Files:**
- Modify: `core/input/server.py:263`（cms）+ payload 中间件
- Modify: `core/input/api_routes.py:626`（upload_handler 自算 cms）
- Modify: `config.yaml:31`（注释）
- Test: `core/input/tests/test_limits.py`、`core/input/tests/test_upload.py`

**Interfaces:**
- Produces: `max_request_bytes=0` → 不限；`None`/缺省 → 64MB。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_limits.py` 加（server 语义——干净的红）：

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_max_request_bytes_zero_sets_unlimited_cms(self, aiohttp_client):
    """max_request_bytes=0 → app.client_max_size==0（aiohttp 原生不限），非回落 64MB。"""
    from unittest.mock import MagicMock
    from core.agent import RunState
    from core.input.server import create_app

    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    agent._state = RunState.RUNNING
    agent.process_message = None
    app0 = create_app(agent, serve_static=False, max_request_bytes=0)
    assert app0._client_max_size == 0          # 修复前为 64MB → 红
    app_def = create_app(agent, serve_static=False, max_request_bytes=None)
    assert app_def._client_max_size == 64 * 1024 * 1024   # 缺省仍 64MB
```

`core/input/tests/test_upload.py` 加（upload handler 走 cfg 同源路径的回归守卫——`make_app` 返回 (agent, app)，注入 config 后 handler 读到小上限 → 413；0→不限的 >64MB 行为由 server 单测语义 + Step 4 代码审查覆盖）：

```python
    @pytest.mark.asyncio
    async def test_upload_reads_max_request_bytes_from_config(self, make_app, aiohttp_client):
        """/api/uploads 聚合上限读 agent.config.server.max_request_bytes。"""
        agent, app = make_app()  # app cms 缺省 64MB，handler cms 走 config
        agent.config.server.max_request_bytes = 100  # noqa: SLF001 — 注入小上限
        client = await aiohttp_client(app)
        resp = await _post(client, filename="big.bin", content=b"\x00" * 1000, kind="file")
        assert resp.status == 413
        assert (await resp.json())["code"] == "REQUEST_TOO_LARGE"
```

- [ ] **Step 2: 运行验证失败**

`python -m pytest core/input/tests/test_limits.py -q -k "zero_sets_unlimited"`
Expected: FAIL — `app0._client_max_size == 64MB`（0 被 falsy 回落），非 0。

- [ ] **Step 3: 实现 server.py**

```python
    cms = max_request_bytes if max_request_bytes is not None else 64 * 1024 * 1024
```
payload 中间件预检：`if cms and request.content_length is not None and request.content_length > cms:`。
create_app docstring：`max_request_bytes: 业务请求体上限（client_max_size，app 级；0=不限，None/缺省 64MB；multipart 除外——upload_handler 同源同步）。`

- [ ] **Step 4: 实现 upload_handler cms 同步**

api_routes.py:626：
```python
        cms = getattr(cfg, "max_request_bytes", None)
        if cms is None:
            cms = 64 * 1024 * 1024
```

- [ ] **Step 5: 更新 config.yaml 注释**

`config.yaml:31`：`# max_request_bytes: 业务请求体上限，默认 64MB；0 = 不限（None/缺省回落 64MB）。`

- [ ] **Step 6: 运行验证通过 + 回归**

`python -m pytest core/input/tests/test_limits.py core/input/tests/test_upload.py core/config/tests/test_app_config.py -q` 全绿。

- [ ] **Step 7: Commit**

```bash
git add core/input/server.py core/input/api_routes.py config.yaml core/input/tests/test_limits.py core/input/tests/test_upload.py
git commit -m "fix(batch8c): F16 max_request_bytes=0 不限——server cms/中间件/upload_handler 自算 cms 三处同步（is not None 语义）"
```

---

### Task 14: get_build_sha 失败缓存（F17）

**Files:**
- Modify: `core/version.py:14-41`
- Test: `core/input/tests/test_version.py`（既有 version 协商测试文件）

**Interfaces:**
- Produces: `get_build_sha()` 无论成败每进程只跑一次 git。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.unit
def test_get_build_sha_failure_cached(monkeypatch):
    """git 失败 → 连续两次调用 subprocess.run 只跑一次。"""
    import core.version as v
    calls = []
    def _fake_run(*a, **k):
        calls.append(1)
        raise OSError("no git")
    monkeypatch.setattr(v.subprocess, "run", _fake_run)
    monkeypatch.setattr(v, "_build_sha", None, raising=False)   # 红态无该属性
    monkeypatch.setattr(v, "_build_sha_tried", False, raising=False)  # 红态无该属性
    assert v.get_build_sha() is None
    assert v.get_build_sha() is None
    assert len(calls) == 1
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL — 第二次调用再跑一次 git（calls == 2；`_build_sha_tried` 不存在时每次重跑）。

- [ ] **Step 3: 实现**

```python
_build_sha: str | None = None
_build_sha_tried: bool = False

def get_build_sha() -> str | None:
    global _build_sha, _build_sha_tried
    if not _build_sha_tried:
        _build_sha_tried = True
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, check=False, cwd=get_project_root(), timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                _build_sha = result.stdout.decode("utf-8", errors="replace").strip()
        except (subprocess.SubprocessError, OSError):
            _build_sha = None
    return _build_sha
```

- [ ] **Step 4: 运行验证通过 + 回归**

`python -m pytest core/input/tests/test_version.py -q` 全绿。

- [ ] **Step 5: Commit**

```bash
git add core/version.py core/input/tests/test_version.py
git commit -m "fix(batch8c): F17 get_build_sha 失败路径缓存（_build_sha_tried 标志，每进程只跑一次 git）"
```

---

### Task 15: TokenStats.byModel 对齐 + 契约 docstring（F19）★

**Files:**
- Modify: `frontend_contract/ws_message.py`（TokenStats.by_model→byModel；模块 docstring 成员数）
- Modify: `core/input/ws_handler.py:85-87`（SessionMessage token_stats 显式构造）
- Modify: `frontend/src/stores/chatStore.ts`（by_model→byModel，6 处 + 注释）
- Modify: `frontend/src/components/character/CharStatus.vue:17`
- Modify: `frontend_contract/tests/test_ws_message.py`（既有 TokenStats 用例）
- Test: 契约 + vue-tsc + gen-types/check-types

**Interfaces:**
- Consumes: Task 3 的 on_token_usage 构造（用 `by_model`——本任务改 `byModel`）。
- Produces: `TokenStats.byModel`（camel）；`SessionMessage.token_stats` wire 用 byModel。

- [ ] **Step 1: 写失败测试（契约）**

`frontend_contract/tests/test_ws_message.py`：

```python
def test_token_stats_wire_camel_by_model():
    from frontend_contract.ws_message import TokenStats
    d = TokenStats(total=1, byModel={"m": 1}).wire_dict()
    assert d["byModel"] == {"m": 1}
    assert "by_model" not in d
```

（不写「旧 by_model kwarg 应抛」测试——`ContractModel` 未设 `extra='forbid'`，旧键会被 pydantic 静默忽略而非抛错；红由「byModel 不被识别」与既有 `test_ws_message.py:42` 的 `TokenStats(total=1, by_model={})`（byModel 必填缺失 → ValidationError）共同保证。）

- [ ] **Step 2: 运行验证失败**

`python -m pytest frontend_contract/tests/test_ws_message.py -q`
Expected: FAIL — 新测试 `byModel` 不识别（ValidationError）；既有 :42 `by_model={}` 因 byModel 必填缺失报错。

- [ ] **Step 3: 实现契约层**

`ws_message.py` `TokenStats.by_model` → `byModel`。模块 docstring「18 具名成员」→「38 具名成员」（F3 后）。既有 `test_ws_message.py:42` 及其余 TokenStats 用例改 `byModel={}`。

- [ ] **Step 4: 写失败测试（v1 建连回放 session 帧 byModel）**

`core/input/tests/test_server.py` 加（真实 Agent + v1 建连；`get_session_stats` 恒返回非 None dict，rename 后 `_send_session_replay` 裸 dict 直传会校验失败且被 handle_ws 吞掉 → 无 session 帧 → 红）：

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_session_replay_token_stats_camel_key(aiohttp_client):
    """v1 建连回放 session 帧 token_stats wire 用 byModel 键（rename 后回放不崩）。"""
    from core.input.server import create_app
    from core.cognitive.tests.test_v2_generation import _make_agent_with, _UsageLLM

    agent = _make_agent_with(_UsageLLM())
    await agent.start()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        ws = await client.session.ws_connect(client.make_url("/ws"), headers={})
        seen = None
        for _ in range(10):
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            if m.type == WSMsgType.TEXT:
                d = json.loads(m.data)
                if d.get("type") == "session":
                    seen = d
                    break
        assert seen is not None, "未收到 session 回放帧"
        ts = seen.get("token_stats") or {}
        assert "byModel" in ts      # rename 后 wire 用 camel
        assert "by_model" not in ts
        await ws.close()
    finally:
        await agent.stop()
```

- [ ] **Step 5: 运行验证失败**

`python -m pytest core/input/tests/test_server.py -q -k session_replay_token_stats`
Expected: FAIL — 无 session 帧（`_send_session_replay` 校验失败被 handle_ws 吞掉，seen 恒 None）。

- [ ] **Step 6: 实现 ws_handler 转换**

`_send_session_replay`：
```python
    token_stats = None
    if hasattr(agent, 'token_tracker') and agent.token_tracker is not None:
        raw = await agent.token_tracker.get_session_stats(session.session_id)
        if raw:
            token_stats = TokenStats(total=raw.get("total", 0), byModel=raw.get("by_model", {}))
```
（Task 3 的 on_token_usage 构造同步改 `byModel=...`。）

- [ ] **Step 7: 实现前端消费点 + 同步既有断言**

`chatStore.ts:41`：`by_model` → `byModel`（ref 类型）；`:200-205`：`s.byModel` + 注释「后端字段为 byModel」；`:243/255`：`tokenStats.byModel`。`CharStatus.vue:17`：`st.byModel`。
`Module/output/tests/test_output.py`：Task 3 新增的 `test_on_token_usage_emits_modeled_message` 断言 `by_model` → `byModel`（`ws.sent[0]["session"]["byModel"]`）。

- [ ] **Step 8: 运行验证通过 + 门禁**

`python -m pytest frontend_contract/tests/ Module/output/tests/ core/input/tests/test_server.py core/input/tests/test_desktop_roundtrip.py -q` 全绿（含 Step 4 的 session 回放转绿与 test_output byModel 断言）。
`make gen-types` + `make check-types` 通过。
`cd frontend && npx vue-tsc --noEmit` 通过。

- [ ] **Step 9: Commit**

```bash
git add frontend_contract/ws_message.py core/input/ws_handler.py frontend/src/stores/chatStore.ts frontend/src/components/character/CharStatus.vue frontend_contract/tests/ frontend/src/types/contract.generated.ts
git commit -m "fix(batch8c): F19 TokenStats.by_model→byModel 对齐 ConversationUsage + 契约 docstring 成员数更新（含 SessionMessage/前端消费点同步）"
```

---

## 任务依赖与执行顺序

- 全任务线性可独立执行；**Task 15 依赖 Task 3**（Task 3 的 on_token_usage 构造用 `by_model`，Task 15 改 `byModel`）。Task 3 须先于 Task 15 完成。
- 每个 task 独立 commit；commit 前跑 `make lint`。
- 全部完成后：`make test-normal`（conda Elysia 环境）全量回归 + `make check-types`。
