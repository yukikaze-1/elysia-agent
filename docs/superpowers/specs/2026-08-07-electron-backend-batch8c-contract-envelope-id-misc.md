# Electron Backend Batch 8c Spec — 契约/信封/ID/杂项（E/F 簇 19 条）

> 前置：由 superpowers:brainstorming 产出。三个决策点 D1-D3 用户已拍板（2026-08-07）；其余按推荐实施并记录于 §1.2 与 §7。

## 1. 背景与范围

承接 `docs/electron-backend-audit-batches0-7.md` 剩余 E/F 簇（契约/信封/ID/杂项）19 条（3 important + 16 minor）。8a 闭合 A/B/C 簇 + 探活，8b 闭合 D 簇 WS/语音行为，本批次闭合契约面与杂项。

### 1.1 范围清单（19 条）

**3 important**
- F1：WS error 信封漏 retryable/details（voice_sessions.py:117，x-contract）
- F2：admin/debug 裸 error 无统一信封（server.py:373，x-contract）
- F3：HeartbeatAckMessage 死契约面 + on_action/on_token_usage 两处绕过（ws_message.py:474，x-contract）

**16 minor**
- F4：generation.started.requestId 从不传值（Module/output/manager.py:345，b2）
- F5：GET /api/sessions 畸形 cursor → 500 非 400（session_manager.py:236，b3）
- F6：generation 持久化 error 丢 code/retryable（loop.py:538，b3）
- F7：pendingMessages 对 conversation_id=None 跨会话可见（session_routes.py:734，b3）
- F8：desktop 工具 output 空 → LLM tool 消息 content 空串，llm_summary 未用（bridge.py:427，b4）
- F9：GET /api/tools/calls/{callId} result 沿用 snake_case 键（api_routes.py:838，b4，PLAUSIBLE）
- F10：GET /api/tts/voices current 引用不存在的音色（tts_profile.py:49，b5）
- F11：capture_ws_trace.py `_ENV_RE` 死代码，env 值脱敏保证不成立（:49，b0）
- F12：capture_ws_trace.py connect 帧 drain 循环 bare except 静默断（:285，b0）
- F13：chunked 413 兜底不生效——handler 宽 except 吞 HTTPRequestEntityTooLarge（server.py:148，b1）
- F14：并发 WS 上限 TOCTOU 预检 + 计数含已关闭 channel（server.py:310，b1）
- F15：v2 通道收到 capabilities 广播（capabilities.py:109，b1）
- F16：max_request_bytes=0 静默回落 64MB 而非不限（server.py:265，b1）
- F17：get_build_sha 失败路径不缓存（version.py:22，x-contract）
- F18：WS JSON decode / content-validation error 无 code（ws_handler.py:287，x-contract）
- F19：契约 docstring 过时 + TokenStats.by_model vs ConversationUsage.byModel 不一致（ws_message.py:1，x-contract）

### 1.2 已拍板决策（D1-D3）

- **D1（用户拍板）**：HeartbeatAckMessage 是 39 成员 ServerMessage 联合里的死契约面（零发射点、零前端消费）——**移除死成员**，并同步修 on_action/on_token_usage 两处绕过（ActionMessage.params 改 Optional、token_usage 走模型化 TokenUsageMessage），让生成 TS 描述真实 wire。
- **D2（用户拍板）**：`max_request_bytes=0` = **不限**（对齐 max_ws_connections「0/None 不限」语义；None/缺省 → 64MB）。
- **D3（用户拍板）**：GET /api/tts/voices 的 current 缺失时**追加到列表**（current 恒 ∈ voices[]）。

**记录中的推荐决策（中途决策点，按常驻指令记入文档并最终报告提出）**：
- F15：v2 跳过 capabilities 广播，**不新增** server_info 重播（Electron 主进程无消费 + YAGNI；多客户端 v2 切换同步记为已知限制 §7）。
- F19：`TokenStats.by_model` → `byModel`（camelCase 对齐 ConversationUsage，契约 wire 约定）。
- F7：conversation_id=None（v1/未归属）pending 仅**活动会话**下展示（实现注释「视为活动会话 best-effort」的既有意图）。
- F9：tools/calls result 归 camelCase（对齐 tool.status wire）。
- F18：decode/content-validation error 用既有 `CONTRACT_VALIDATION` 码（不新增错误码）。
- F2：admin/debug 认证失败用 `AUTH_REQUIRED`/`AUTH_INVALID`（与 access middleware 既有码一致）。
- F8：desktop 工具 output 空时 content 回退 executionSummary（spec §4.3「llm_summary 或 output」）。

## 2. 全局约束（继承项目规范）

- **禁相对导入**（ruff TID252，`__init__.py`/`__main__.py` 豁免）。
- **敏感纪律**：错误信封/日志不落正文、路径、密钥；`str(e)` 只进日志不进客户端信封。
- 契约单源：前端 `contract.generated.ts` 为只读快照，改 `frontend_contract/` 后须 `make gen-types` 再 `make check-types`（CI 门禁）。
- 新测试必须加 marker（unit/service/integration/slow），日常 `make test-quick` / `make test-normal`。
- WS 错误码统一走 `docs/electron-backend-protocol-v2.md` §9.1 错误码目录，禁止裸错误。
- 改动涉及 wire 键时，同步更新前端消费点（`frontend/src/`）与测试。

## 3. 修复设计

### 3.1 F1 WS error 信封漏 retryable/details（重要，x-contract）

**现状**：`ErrorMessage`（frontend_contract/ws_message.py:480-491）只有 `type/message/code/requestId`；`_emit_error`（voice_sessions.py:136-144）声明了 `retryable` 参数但构造 ErrorMessage 时未传；`_send_voice_error`（ws_handler.py:384-391）同样丢弃 `error.retryable`。协议文档 §9.1 声明 `STT_UNAVAILABLE` retryable=true，但客户端收不到。

**修复**：
1. `ErrorMessage` 加两个 Optional 字段（exclude_none 下缺省省略，既有 wire 不变）：
   ```python
   retryable: Optional[bool] = Field(default=None, description="是否可重试，可缺省")
   details: Optional[dict[str, Any]] = Field(default=None, description="公开安全详情，可缺省")
   ```
2. `_emit_error`（voice_sessions.py:136）：构造 ErrorMessage 时传 `retryable=retryable`。**语义说明**：`_emit_error` 默认 `retryable=False` 显式传值 → 未显式设 True 的永久错误（如 `ERR_RECORDING_TOO_LONG`）wire 从「无该键」变为 `retryable:false`（语义上正确——客户端可区分永久/可重试；前端仅 console.warn、`retryable?: boolean` 类型兼容，无破坏）。「缺省省略」仅指契约层 None → exclude_none 省略。
3. `_send_voice_error`（ws_handler.py:384）：传 `retryable=getattr(error, "retryable", None)`（`VoiceSessionError` 已带 `.retryable`，voice_sessions.py:183/264/329 设 True）。

**测试**：unit——ErrorMessage 带 retryable/details 时 wire_dict 含该键、缺省时省略；service——STT 关闭触发 `_emit_error(..., True)` → 客户端收到 `{type:'error', code:'STT_UNAVAILABLE', retryable:true}`。

### 3.2 F2 admin/debug 裸 error 无统一信封（重要，x-contract）

**现状**：/shutdown、/restart、/tools、/tools/reload 认证失败返回 `web.json_response({"error": "unauthorized"}, status=401)`（server.py:372/393/412/431）；debug 认证中间件与部分 handler 返回裸 `{"error": ...}`（debug_routes/__init__.py:32、archive.py:146、character.py:125）。而 /api/* 同类的认证失败已走 `_protocol_error_response`（ProtocolError 信封，rest.py:111-123，`error` 为 message 兼容别名）。v1 `ApiFail`/`ImageUploadError` 是文档化 v1-compat 形状，**不动**。

**修复**：admin 四个 handler + debug 全部裸 `{"error": ...}` 响应统一改 `_protocol_error_response`（ProtocolError 信封；`error` 字段为 message 兼容别名，v1 读 `.error` 不破坏）。**按模式枚举而非行号**（审计引用的行号是 8a/8b 前旧行号，已漂移）：
1. server.py 四端点（/shutdown、/restart、/tools、/tools/reload）认证失败：按 Authorization 头有无取 `AUTH_REQUIRED`（无头）/`AUTH_INVALID`（错误头），与 access middleware 一致：
   ```python
   if not _check_auth(request):
       code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
       msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
       return _protocol_error_response(code, msg, 401)
   ```
2. debug_routes/__init__.py 认证中间件：同码替换（现已有 `code` 变量）。
3. **debug_routes 全部业务错误 handler**（`web.json_response({"error": X}, status=Y)` 模式，grep 现约 30 处：archive.py 13、character.py 12、ltm.py 6）：改 `_protocol_error_response(code, X, Y)`，code 用语义码（分页/校验类 `CONTRACT_VALIDATION`、缺失类 `NOT_FOUND` 等，按 §9.1 目录）。
4. `_protocol_error_response` **函数内懒导入**（server.py:24 模块级导入 debug_routes，模块级互相导入成环——用 `from core.input.server import _protocol_error_response` 于 handler/中间件函数体内，复用 8a/8b 的懒导入绕环模式；api_routes.py:612 已有同款先例）。

**测试**：service——无 token 调 /shutdown → 401 信封含 code/retryable/requestId/error 别名；debug /debug/api/* 认证失败同样；debug 业务错误（如 ltm 非法分页参数）→ 信封（含 code）；debug 前端（frontend/src/debug）仅读 fetch 错误 message（debugStore.ts:29）不依赖 `.error` 精确串——不破坏。

### 3.3 F3 HeartbeatAckMessage 死契约面 + 两处绕过（重要，x-contract）★

**现状**：① `HeartbeatAckMessage`（ws_message.py:474-478）在 ServerMessage 联合（:851）但 core/Module 零发射点、前端零处理（仅 contract.generated.ts 有）。② `on_action`（Module/output/websocket.py:113-125）裸 dict 发射，代码注释说明因 `ActionMessage.params` 非 Optional（default_factory=dict）会强发 `params:{}`；③ `on_token_usage`（:186-196）裸 dict 发射，因 TokenRound 经 wire_dict(exclude_none) 会省略 null 的 cached_tokens/reasoning_tokens，与真实 wire（显式 null）不符。

**修复**（按 D1 移除）：
1. `ws_message.py`：删除 `HeartbeatAckMessage` 类 + 从 ServerMessage 联合移除 + **同步删 `frontend_contract/__init__.py:104/225` 的导入与 `__all__` 条目**（漏删则 `import frontend_contract` ImportError）。
2. `ActionMessage.params`：`dict[str, Any] = Field(default_factory=dict)` → `Optional[dict[str, Any]] = Field(default=None)`（对齐 CommandMessage；exclude_none 下 None 省略）。
3. `on_action` 切模型化：
   ```python
   await self._send_json(ActionMessage(action=action, params=params).wire_dict())
   ```
   （params None → 省略。）
4. **前端类型同步（必做）**：`frontend/src/composables/useWebSocket.ts` `handleAction`（:96-101）现写 `msg.params.tool` / `msg.params.call_id` / `msg.params.args` 无可选链；params 变 Optional 后 `vue-tsc --noEmit`（`npm run build` 门禁）在 strict 下报 TS2532。改 `msg.params?.tool` / `msg.params?.call_id` / `msg.params?.args`（运行时 tool_executing/tool_done 恒带 params，行为不变）。
5. `on_token_usage` 切模型化（数据源 token_tracker.py:109-121 `{turn:{rounds,turn_total}, session:{total,by_model}}`）：
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
           byModel=session.get("by_model", {}),
       ) if session else None,
   ).wire_dict())
   ```
   （TokenRound 的 null cached_tokens/reasoning_tokens 经 exclude_none 省略；前端 TokenLabel.vue:41-42 用 `!= null` 判定，undefined 与 null 渲染一致。）
6. `frontend_contract/tests/test_ws_message.py:20`：联合成员数 39 → 38（+ UnknownMessage 发射器追加）。
7. `make gen-types` 重新生成 TS（heartbeat_ack 消失；ActionMessage.params 变 Optional——前端消费点已在第 4 点同步；TokenRound 的 cached/reasoning 本就是 Optional，TS 不变）。

**测试**：unit——联合成员数断言更新；ActionMessage params None 时 wire 省略、非 None 时序列化；on_action 发模型化帧；on_token_usage 发模型化帧（by_model→byModel、null 键省略）。

### 3.4 F4 generation.started.requestId 回显

**现状**：`GenerationStartedMessage.requestId`（ws_message.py）与 spec §3.2 要求回显 request_id，但 `turn_started`（manager.py:346-356）不传。`GenerationContext.request_id`（generation.py:41，来源 UserInputData.request_id）已存在。

**修复**：`turn_started`（manager.py:346-356）广播参数补 `request_id=ctx.request_id`（`GenerationStartedMessage.requestId: Optional[str]` 已存在，ws_message.py）。

**测试**：service——v2 generation.started 帧携带 ctx.request_id（与 user_message.ack 的 requestId 一致）。

### 3.5 F5 畸形 cursor → 400 CONTRACT_VALIDATION

**现状**：`sessions_list_handler`（session_routes.py:384-393）调 `manager.list(cursor=...)`，内部 `_decode_cursor`（session_manager.py:236）抛 ValueError → 500。与 messages before 游标（handler 层 400）不一致。

**修复**：handler 包 try/except：
```python
try:
    items, has_more, next_cursor = await manager.list(cursor=cursor, limit=limit, search=search)
except ValueError:
    return _error_response("CONTRACT_VALIDATION", "cursor 格式非法")
```

**测试**：service——GET /api/sessions?cursor=garbage → 400 CONTRACT_VALIDATION（信封）。

### 3.6 F6 generation 持久化 error 结构化

**现状**：loop.py:538 存 `str(e)[:200]`；`GenerationRegistry.mark`（generation_registry.py:96-130）`error: str | None` 只存字符串；`_generation_status`（session_routes.py:670-690）已能透传 dict error（`isinstance(err, dict)` → 原样），非 dict 才包 `{message, retryable:false}`。

**修复**：
1. `GenerationRegistry.mark`：`error: str | dict | None = None`——dict 时 `doc["error"] = error`（原样存），str 时兼容包一层？不——直接存 str 会丢结构化信息；调用方改传 dict 后，此处仅透传。签名改 `error: str | dict | None`，`if error is not None: doc["error"] = error`。
2. loop.py:538 错误路径：
   ```python
   err_msg = str(e)[:200]
   ...
   await self._generation_registry.mark(
       self._generation_ctx.generation_id,
       status="failed", finish_reason="error",
       error={"code": code, "message": err_msg, "retryable": retryable})
   ```
   （code/retryable 已在同函数上方计算：CONNECTION_RESET / INTERNAL 分支。）
3. `_generation_status` 不变（dict error 已透传）。

**测试**：unit/service——generation 失败后 GET /api/generations/{gid} 的 error 为 `{code, message, retryable}` dict，code/retryable 与 loop 计算一致。

### 3.7 F7 pending 跨会话可见 → 仅活动会话

**现状**：session_routes.py:739-743 注释「conversation_id=None（v1/未归属）视为活动会话 best-effort」，但代码只 `if pm.conversation_id is not None and str(...) != str(sid): continue`——None 归属在任意请求会话下都展示。

**修复**：None 归属映射到活动会话（含 active_id 为 None 的防御——此时 None-归属 pending 一律不展示，避免泄漏残留）：
```python
active_id = getattr(getattr(agent, "session_manager", None), "active_id", None)
for pm in getattr(agent, "pending_messages", None) or []:
    cid = pm.conversation_id
    effective = str(cid) if cid is not None else (str(active_id) if active_id else None)
    if effective != str(sid):
        continue  # None 归属且 active_id 为空 → 不匹配任何 sid → 隐藏
    ...
```

**测试**：service——活动会话下请求 → 含 None 归属 pending；请求非活动 sid → 不含 None 归属（仅该会话自己 conversation_id 匹配的）。

### 3.8 F8 llm_summary 未用（desktop 工具 output 空）

**现状**：bridge.py `_map_terminal`（:449-458）：`content = msg.output or ""`，`llm_summary = msg.executionSummary or content`，`ToolResult.ok(content=content, llm_summary=llm_summary, ...)`。`ToolResult.to_llm_message`（protocol.py:577）：text 类型用 `content`，非 text 用 `llm_summary`。desktop.screen.capture 等 output 为空 → LLM tool 消息 content 空串，llm_summary 未注入。

**修复**：output 空时 content 回退 executionSummary（spec §4.3「llm_summary（execution_summary 去路径）或 output」）：
```python
content = msg.output or (msg.executionSummary or "")
llm_summary = msg.executionSummary or content
```
（用户可见 result_field.output 保持原始 msg.output；LLM 侧 content 与 llm_summary 都落 summary。）

**测试**：unit——RESULT 状态 msg.output 空 + executionSummary 非空 → ToolResult.content == executionSummary；output 非空 → content == output（summary 不影响）。

### 3.9 F9 tools/calls result → camelCase

**现状**：`_map_terminal` 存库 result_field 用 snake_case（`attachments[].mime_type`、`execution_summary`）；`tool.status` wire 用 camelCase（`mimeType`/`executionSummary`）。`_desktop_call_to_status`（api_routes.py:895-909）把 repo 文档 result 原样透传 → REST 响应与 wire 不一致。

**修复**：`_desktop_call_to_status` 对 result dict 归一 camelCase（REST 边界转换，不动 repo 存储）：
```python
result = doc.get("result")
if isinstance(result, dict):
    result = dict(result)
    if "execution_summary" in result:
        result["executionSummary"] = result.pop("execution_summary")
    attachments = result.get("attachments")
    if isinstance(attachments, list):
        result["attachments"] = [
            {"url": a.get("url"), "mimeType": a.get("mime_type", a.get("mimeType")), "label": a.get("label")}
            for a in attachments if isinstance(a, dict)
        ]
```
更新 `DesktopCallStatus.result` docstring（rest.py）：`{output,attachments,executionSummary}`。

**测试**：service——GET /api/tools/calls/{callId}（repo 有 snake 键 result）→ 响应 result 键为 executionSummary/mimeType。

### 3.10 F10 voices current 缺失追加（D3）

**现状**：tts_voices_handler（api_routes.py:454-478）：`current = service_state.tts_voice or _backend_default_voice(...)`；custom OpenAI 默认返回角色名（character），不在 `list_voices()` 列表。

**修复**（D3 追加）：current 非空且不在 voices 时补一条：
```python
if current and not any(getattr(v, "id", None) == current for v in voices):
    voices.append(rest_contract.TtsVoiceEntry(id=current, name=current))
```

**测试**：service——假后端 list_voices 返回固定列表 + current 为列表外值 → 响应 voices 含 id==current 条目；current 在列表 → 不重复追加。

### 3.11 F11 _ENV_RE 死代码（trace 脚本 env 脱敏）

**现状**：capture_ws_trace.py:49 定义 `_ENV_RE = re.compile(r"([A-Z][A-Z0-9_]{2,})")`（只匹配 env 名）从未使用；docstring（:21）承诺「环境变量值/绝对路径/真实内容替换为占位符」，但 `_sanitize`（:60-64）只剥绝对路径 + 一个字面量。

**修复**：把 `_ENV_RE` 改为能剥值的模式并 wire 进 `_sanitize`：
```python
_ENV_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=(?:[^\s\"',}]+|\"[^\"]*\"|'[^']*')")

def _sanitize(line: str) -> str:
    line = _PATH_RE.sub("$SANITIZED_PATH", line)
    line = _ENV_RE.sub(lambda m: f"{m.group(1)}=***", line)
    line = line.replace("test-key-placeholder", "***")
    return line
```

**测试**：unit——`_sanitize("export OPENAI_API_KEY=sk-abc123")` → `OPENAI_API_KEY=***`；不误伤 `VOICE=alloy` 这类非敏感（保留 key 名即可，值统一 ***）。

### 3.12 F12 trace bare except 静默断

**现状**：capture_ws_trace.py:285 connect 帧 drain 循环 `except Exception: break` 静默——握手失败产出空/截断轨迹无任何提示。

**修复**：`except Exception as e:` 打印诊断 + 置截断标记，脚本结束报告：
```python
except Exception as e:
    _truncated = True
    print(f"[capture] 连接/握手失败，轨迹可能截断: {e}", file=sys.stderr)
```
并在脚本收尾（写轨迹后）若 `_truncated` 则打印警示。

**测试**：unit——drain 循环在 `ws_connect` 成功之后（capture_ws_trace.py:279-286），「URL 不可达」在 ws_connect 就失败、走不到该 except。正确场景：mock `ws.receive_json` 在建连成功后抛异常（连接被 reset/EOF）→ stderr 含「轨迹可能截断」+ 截断标记。

### 3.13 F13 chunked 413 兜底不生效

**现状**：payload 中间件（server.py:148-171）已对 Content-Length 预检 413 + `except web.HTTPRequestEntityTooLarge` 兜底；但 chunked（无 Content-Length）场景 aiohttp 在读体时抛 HTTPRequestEntityTooLarge，被 handler 的宽 `except Exception` 吞成 400——中间件兜底永不触发。

**修复**：所有读取请求体的 handler，在宽 `except Exception` 之前补 `except web.HTTPRequestEntityTooLarge: raise`，让中间件 413 信封生效。**按模式枚举**（行号会随批次漂移，实现时以「`await request.json()` / `await request.multipart()` 外包宽 except Exception」为锚点）：
- api_routes.py：`providers_save_handler`、`providers_test_handler`、`tts_preview_handler`、`image_upload_handler`、`upload_handler`、`desktop_upload_handler`、`chat_api_handler`
- session_routes.py：`sessions_create_handler`（:420-423 `await request.json()` 外包 `except Exception`）、`sessions_update_handler`（:457-460 同）
- debug_routes.py：`archive.py:119`、`ltm.py:82/127`、`character.py:140/192` 的 `await request.json()`（核对其外包的 except Exception，同样补 re-raise）

**测试**：service——用 chunked 传输（aiohttp `data=...` 且不设 Content-Length）超限 body 打代表性端点 → 413 REQUEST_TOO_LARGE 信封（非 400）。覆盖：api_routes 的 upload_handler（multipart）+ chat_api_handler（json）+ session_routes 的 sessions_create + debug_routes 一处（如 ltm）。

### 3.14 F14 并发 WS TOCTOU 硬上限

**现状**：server.py:320-338 `count = sum(channels)`（含已关闭未移除的 channel）+ 预检与 add_channel 之间有 await（ws.prepare）——并发握手可全过、计数偏高。

**修复**：create_app 闭包维护专用活跃计数，检查+自增同步（asyncio 单线程下原子），handle_ws 返回（连接关闭）后 finally 自减：
```python
_ws_active = 0  # create_app 闭包

async def ws_handler(request):
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
（检查+自增无 await → 原子；关闭即自减 → 不再计入已关闭 channel。max_ws_connections=0/None 走原不限路径。）

**测试**：service——既有 test_limits.py::test_ws_over_limit_rejected_503 保持通过；新增并发握手测试（asyncio.gather 两个连接同时发起，max=1 → 恰一个成功）验证硬上限。

### 3.15 F15 v2 跳过 capabilities 广播

**现状**：`broadcast_capabilities`（capabilities.py:109-125）向所有 WebSocketChannel 广播 capabilities 帧（含 v2），与「v2 用 server_info 取代 capabilities」矛盾。浏览器前端恒 v1（无自定义头，settingsStore 依赖 capabilities 重播兜底校正）——v1 必须保留。

**修复**：广播时跳过 v2 通道：
```python
for ch in agent.output._channels:  # noqa: SLF001
    if isinstance(ch, WebSocketChannel) and getattr(ch, "protocol_version", 1) < 2:
        await ch._send_json(payload.wire_dict())  # noqa: SLF001
```

**测试**：service——v1 通道收到 capabilities 重播；v2 通道（protocol_version=2）不收到。

### 3.16 F16 max_request_bytes=0 不限（D2）

**现状**：server.py:263 `cms = max_request_bytes if max_request_bytes else 64MB`——0 静默回落。

**修复**（D2 不限）：
1. `cms = max_request_bytes if max_request_bytes is not None else 64 * 1024 * 1024`（0 → cms=0，aiohttp client_max_size=0 原生不限）。
2. payload 中间件预检补 `if cms and ...`（cms=0 时不预检；`except HTTPRequestEntityTooLarge` 在 cms=0 时天然不触发）：
   ```python
   if cms and request.content_length is not None and request.content_length > cms:
       return _protocol_error_response("REQUEST_TOO_LARGE", "请求体过大", 413)
   ```
3. **upload_handler 自算 cms 同步**（api_routes.py:626，注释自述「取 agent 配置同源（create_app 的 client_max_size 也是它）」）：`cms = getattr(cfg, "max_request_bytes", None) or 64 * 1024 * 1024` → `None` 时缺省 64MB、`0` 时不限（chunked 聚合上限与 app 级 client_max_size 语义一致，避免同一配置两套语义）。
4. config.yaml:31 注释更新（0 = 不限）；create_app docstring 更新。

**测试**：service——create_app(max_request_bytes=0) 发超大 body（Content-Length 明确）→ 非 413；`/api/uploads` 大 multipart 在 max_request_bytes=0 下不 413（handler 自算 cms 同步验证）；create_app(max_request_bytes=None) → 缺省 64MB 仍 413。

### 3.17 F17 get_build_sha 失败缓存

**现状**：version.py:19-41 失败路径 `_build_sha` 保持 None，每次握手重新 spawn `git rev-parse HEAD`。

**修复**：加 `_build_sha_tried` 标志，无论成败只跑一次：
```python
_build_sha: str | None = None
_build_sha_tried: bool = False

def get_build_sha() -> str | None:
    global _build_sha, _build_sha_tried
    if not _build_sha_tried:
        _build_sha_tried = True
        try:
            result = subprocess.run([...], ...)
            if result.returncode == 0 and result.stdout:
                _build_sha = result.stdout.decode("utf-8", errors="replace").strip()
        except (subprocess.SubprocessError, OSError):
            _build_sha = None
    return _build_sha
```

**测试**：unit——mock subprocess.run 抛异常，连续两次 get_build_sha → subprocess.run 只调用一次（第二次返回 None 不重跑）。

### 3.18 F18 decode error 无 code

**现状**：ws_handler.py:287（JSONDecodeError）与 :306（ValidationError 内容校验）构造 ErrorMessage 不带 code。

**修复**：两处补 `code="CONTRACT_VALIDATION"`（D-rec；不新增错误码）。

**测试**：service——发非法 JSON WS 消息 → error 帧 code=CONTRACT_VALIDATION；发合法 JSON 但内容非法（非 dict）→ 同码。

### 3.19 F19 契约 docstring + wire key 对齐

**现状**：① ws_message.py:1 模块 docstring 说「Server→Client 18 具名成员」而联合实际 39（F3 移除后 38）；② `TokenStats.by_model`（snake）与 `ConversationUsage.byModel`（camel）同概念不同 wire key。

**修复**（by_model → byModel，D-rec camelCase）：
1. `TokenStats.by_model` → `byModel`（ws_message.py:129-135）。
2. 数据源转换（内部 API `get_session_stats`/token_usage data 保持 `by_model` 键不变）：
   - ws_handler.py:87 SessionMessage：`token_stats` 显式构造 `TokenStats(total=raw.get("total",0), byModel=raw.get("by_model",{}))`（不能用裸 dict 直传——模型字段已改名）。
   - websocket.py on_token_usage：随 §3.3 的 TokenUsageMessage 构造已映射 by_model→byModel。
3. 前端消费点更新（`by_model` → `byModel`）：
   - `frontend/src/stores/chatStore.ts:41/200/202/203/243/255`
   - `frontend/src/components/character/CharStatus.vue:17`
   - 注释同步（:200「后端字段为 by_model」）。
4. **既有测试更新**：`frontend_contract/tests/test_ws_message.py:42` `TokenStats(total=1, by_model={})` → `byModel={}`（rename 后 pydantic 拒绝旧 kwarg）；同文件其他 TokenStats 用例一并核对。
5. 模块 docstring 成员数更新为实际值（F3 后 38）。
6. `make gen-types` 重新生成 TS；`make check-types` 通过。

**测试**：unit——TokenStats wire_dict 用 `byModel` 键；SessionMessage(token_stats=raw_by_model_dict) 经显式构造后 wire 为 byModel；TS 快照含 byModel（gen 后 check-types）。

## 4. 测试计划

- 单文件运行：`python -m pytest core/input/tests/test_limits.py core/input/tests/test_tts_voices.py core/input/tests/test_v2_ws.py core/cognitive/tests/test_v2_generation.py -v` 等按任务分组（WS handler 相关测试实际在 `test_v2_ws.py`/`test_voice_ws.py`/`test_session_ws.py`，无 `test_ws_handler.py`）。
- 契约测试：`python -m pytest frontend_contract/tests/ -v`（联合成员数 39→38、byModel、ErrorMessage 新字段）。
- 新增测试 marker：unit/service（F12 属脚本逻辑，unit；其余 service/unit 按既有分层）。
- 全量：`make test-normal`（conda Elysia 环境）。
- 门禁：`make lint` + `make check-types`（frontend_contract 漂移）每次改动后跑。

## 5. 文档更新

- `docs/electron-backend-audit-batches0-7.md`：19 条标「✅ 批次 8c 已修复（2026-08-07，<commit>）」。
- `docs/electron-backend-progress.md`：新增批次 8c 完成节（测试数增量、决策记录）。
- `docs/electron-backend-protocol-v2.md`：§9.1 错误码目录确认 CONTRACT_VALIDATION 覆盖 decode；§server_info/capabilities 段补 v2 跳过 capabilities 广播说明。
- `docs/electron-backend-security.md`（如涉及 admin 信封变更说明）、`core/input/README.md`（capabilities 广播 v2 跳过）、`config.yaml` 注释（max_request_bytes=0 不限）。

## 6. 合规对照（audit 19 条）

| 条目 | 修复位置 | § |
|------|----------|---|
| F1 WS error 信封 | ws_message.py / voice_sessions.py / ws_handler.py | 3.1 |
| F2 admin/debug 信封 | server.py / debug_routes | 3.2 |
| F3 HeartbeatAck + 绕过 | ws_message.py / websocket.py | 3.3 |
| F4 requestId 回显 | manager.py | 3.4 |
| F5 畸形 cursor | session_routes.py | 3.5 |
| F6 generation error | generation_registry.py / loop.py | 3.6 |
| F7 pending 归属 | session_routes.py | 3.7 |
| F8 llm_summary | bridge.py | 3.8 |
| F9 tools/calls camel | api_routes.py | 3.9 |
| F10 voices current | api_routes.py | 3.10 |
| F11 _ENV_RE | capture_ws_trace.py | 3.11 |
| F12 trace bare except | capture_ws_trace.py | 3.12 |
| F13 chunked 413 | api_routes.py + server.py | 3.13 |
| F14 WS TOCTOU | server.py | 3.14 |
| F15 v2 capabilities | capabilities.py | 3.15 |
| F16 max_request_bytes | server.py / config.yaml | 3.16 |
| F17 build_sha 缓存 | version.py | 3.17 |
| F18 decode code | ws_handler.py | 3.18 |
| F19 docstring + byModel | ws_message.py / ws_handler.py / 前端 | 3.19 |

## 7. 已知限制（记录）

- **F15 多客户端 v2 切换同步**：v2 通道跳过 capabilities 广播后，非发起方的 v2 客户端不会感知其他连接的模型/服务切换（server_info 仅握手下发）；单客户端 Electron 场景不受影响，多客户端同步留待后续（记录为限制，不做 server_info 重播）。
- **F10 custom OpenAI current 追加**：追加条目 name=id（无本地化显示名）；仅保证 current 可渲染，不保证显示名完整。
- **F8 executionSummary 去路径**：bridge 假设 Electron 侧 executionSummary 已去路径（敏感纪律 D-p 面向日志）；LLM tool 消息非日志通道，超出本批次范围。
- **F13 handler 枚举**：chunked 413 兜底覆盖 api_routes 7 处 + session_routes 2 处 + debug_routes 5 处（§3.13 已枚举）；未来新增 body 读取 handler 需同样补 `except HTTPRequestEntityTooLarge: raise`（写入 create_app docstring 提示）。
- **F9 仅 REST 边界转换**：repo 存储仍为 snake_case result，REST 响应归一 camelCase；其他 repo 消费方不受影响。
