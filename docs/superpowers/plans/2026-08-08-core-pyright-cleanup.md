# core/ pyright 报错清理 + 门禁 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修清 core/ 生产代码的 pyright 报错（**92 处，Elysia env 基线**，A-E 五类）+ 引入 pyright 门禁
（`pyrightconfig.json` + `make pyright` + CLAUDE.md 文档），门禁锁定 Elysia env 检 core/ 生产 **0 errors**。
运行时以零变化为主（类型标注/收口；可空收窄 + StrEnum 边界已声明）。

**Architecture:** Task 1 门禁落地（配置已探明，锁 Elysia env）；Task 2-6 按 A-E 分类修 92 处；Task 7 全量验证。
每 Task 验证：pyright 该文件报错清零 + 相关 pytest 不回归。任务间依赖：全部完成后 pyright 才 0 errors
（门禁是整程序检查，Task 1 建立时 92 为已知失败态）。

**Tech Stack:** Python 3.12 + pyright 1.1.411（conda env `Elysia`，已装）。pytest（marker=unit/service）。
pyright 命令（锁 Elysia env）：`PATH="/home/yomu/miniconda3/envs/Elysia/bin:$PATH" pyright`

## Global Constraints

- 生产代码函数参数必须类型注解（ruff ANN001-3）。
- 禁相对导入（ruff TID252）。代码注释/README 用中文。
- **门禁检 core/ 生产代码**（pyrightconfig include core/、排除 tests、basic 模式、关 reportMissingImports）。
- **不因 pyright 用 getattr 退步**——mixin 属性用类型标注声明。
- **StrEnum 收口**（`Role(x)`/`RepoCallStatus(x)` 等）：x 上游已过滤到合法值，收口不抛（声明为边界）。
- spec: `docs/superpowers/specs/2026-08-08-core-pyright-cleanup-design.md`（92 处 A-E 全表）。
- 完整报错清单：`/tmp/pyright-errors-92.txt`。

---

## File Structure

- **Create:** `pyrightconfig.json`（已建，Task 1 提交）
- **Modify:** `Makefile`（pyright 目标）、`CLAUDE.md`（门禁记录）
- **Modify（A 类）:** `core/agent/server.py`、`_lifecycle_persistence.py`、`_lifecycle_tools.py`、`core/adapter/tts_adapter.py`、`core/tools/runtime_config/plugin.py`
- **Modify（B 类）:** `core/agent/agent.py`、`core/input/session_routes.py`、`core/input/ws_handler.py`、`core/cognitive/loop.py`、`core/monitoring/idle_monitor.py`、`core/input/debug_routes/archive.py`、`core/input/voice_sessions.py`、`core/input/api_routes.py`
- **Modify（C 类）:** `core/input/ws_handler.py`、`core/agent/_lifecycle_services.py`、`core/input/debug_routes/character.py`、`core/input/session_routes.py`、`core/input/voice_sessions.py`、`core/monitoring/token_tracker.py`、`core/desktop/bridge.py`
- **Modify（D 类）:** `core/input/api_routes.py`、`core/input/ws_handler.py`、`core/input/session_routes.py`、`core/memory/session.py`、`core/input/ssrf.py`、`core/input/voice_sessions.py`、`core/input/attachment_routes.py`、`core/agent/server.py`、`core/memory/memory_reflector.py`、`core/tools/time/plugin.py`
- **Modify（E 类）:** `core/tools/vision/plugin.py`

---

### Task 1: pyright 门禁落地

**Files:**
- Create: `pyrightconfig.json`（已在工作区，内容见 spec §1）
- Modify: `Makefile`、`CLAUDE.md`

**Interfaces:**
- Produces: `make pyright` 目标 + 门禁配置。Task 2-6 的验证工具。

- [ ] **Step 1: 提交 pyrightconfig.json**

`pyrightconfig.json` 已在仓库根（未跟踪）。内容：

```json
{
  "include": ["core/"],
  "exclude": ["**/tests/**", "**/test_*.py", "**/__pycache__"],
  "pythonVersion": "3.12",
  "typeCheckingMode": "basic",
  "reportMissingImports": false
}
```

- [ ] **Step 2: Makefile 加 pyright 目标**

在 Makefile 的 test 目标附近加（对齐既有 `conda run -n $(CONDA_ENV)` 惯例——查 Makefile 顶部确认 CONDA_ENV）：

```makefile
# 类型检查门禁（pyright，core 生产代码；须 Elysia env 运行——base env 无 aiohttp 会漏报 25 处）
pyright:
	conda run -n $(CONDA_ENV) pyright
```

- [ ] **Step 3: 验证基线**

Run: `conda run -n Elysia pyright`（或 `PATH="/home/yomu/miniconda3/envs/Elysia/bin:$PATH" pyright`）
Expected: **92 errors**（与 spec 基线一致——锁 Elysia env 后 aiohttp 泛型解析出 92 处，含 ws 可空等）。

- [ ] **Step 4: CLAUDE.md 记录门禁**

`CLAUDE.md`「Running tests」加 pyright 命令（`make pyright`，须 Elysia env）；「Conventions」约束文档列表
加 `docs/code-style-guide.md` 的 pyright 门禁说明（或直接在此记录）。

- [ ] **Step 5: 提交**

```bash
git add pyrightconfig.json Makefile CLAUDE.md
git commit -m "chore(pyright): 引入 core/ 生产代码 pyright 门禁（basic + 排除测试 + 锁 Elysia env）"
```

### Task 2: A 类——mixin/动态属性类型标注占位

**Files:**
- Modify: `core/agent/server.py`（17 处）、`core/agent/_lifecycle_persistence.py:133`、`core/agent/_lifecycle_tools.py:104`、`core/adapter/tts_adapter.py:310`、`core/tools/runtime_config/plugin.py:302,305`

**Interfaces:**
- Produces: `AgentServer` 类内声明 `session`/`stt_enabled`/`tts_enabled` 类型占位；`broadcast_capabilities` 参数放宽。
  运行时行为不变（mixin 组合后属性由 Agent 提供）。

- [ ] **Step 1: 验证失败态（pyright 报 A 类错）**

Run: `PATH="/home/yomu/miniconda3/envs/Elysia/bin:$PATH" pyright 2>&1 | grep -E "server.py|_lifecycle|tts_adapter|runtime_config"`
Expected: A 类报错存在（`AgentServer.session` 等）。

- [ ] **Step 2: server.py 类型占位**

`core/agent/server.py` 的 `AgentServer` 类内（类体开头）加类型占位（运行时由组合的 `Agent` 提供，mixin 标准修法）：

```python
class AgentServer:
    """Server/REPL 生命周期 mixin——运行时与 Agent 组合，session 等属性来自 Agent。"""
    session: SessionMemory    # 类型占位：运行时来自 Agent @property（mixin 组合）
    output: "OutputManager"   # 占位：broadcast_capabilities/build_capabilities 用
    stt_enabled: bool
    tts_enabled: bool
```

`broadcast_capabilities`（及它内部调用的 `build_capabilities`，在 `core/input/capabilities.py`）参数
`agent: "Agent"` → **同步放宽为 `"Agent | AgentServer"`**（review 修正：只放宽 broadcast_capabilities 会在
capabilities.py:109 新增 `build_capabilities(agent)` 与 `agent.output` 两处错——`AgentServer` 声明
`session`/`output`/`stt_enabled`/`tts_enabled` 后 union 有这些属性，两函数参数一起放宽才清零）。
import 加 `from core.memory.session import SessionMemory`（或 `TYPE_CHECKING` 下引入避免运行时环）。

- [ ] **Step 3: 其余 A 类**

- `_lifecycle_persistence.py:133`：`_LifecyclePersistence` 类内声明 `attachment_repo: AttachmentRepository` 占位（import）。
- `_lifecycle_tools.py:104`：`target._send_json(...)`——`target` 是 `OutputChannel`（ABC），`_send_json` 只在
  `WebSocketChannel`（Module/output/websocket.py:84）定义。**确定修法（review 修正）**：`cast(WebSocketChannel, target)._send_json(...)`
  （import `from typing import cast` + `WebSocketChannel`）。
- `tts_adapter.py:310`：`t.flush_on_cancel = True`（asyncio.Task 动态属性）→ `t.flush_on_cancel = True  # type: ignore[attr-defined]`（注释说明：仅禁用路径置位，取消时 flush 文本兜底）。
- `runtime_config/plugin.py:302,305`：`doc` 变量显式类型（如 `doc: dict[str, Any] = ...`），使 `doc["created_at"]` 可解析。

- [ ] **Step 4: 验证**

Run: `PATH="/home/yomu/miniconda3/envs/Elysia/bin:$PATH" pyright 2>&1 | grep -cE "server.py|_lifecycle|tts_adapter|runtime_config"`
Expected: 0（A 类报错清零）。再跑 `pytest core/agent/tests core/tools -m "not slow"` 确认不回归。

- [ ] **Step 5: 提交**

```bash
git add core/agent/server.py core/agent/_lifecycle_persistence.py core/agent/_lifecycle_tools.py core/adapter/tts_adapter.py core/tools/runtime_config/plugin.py
git commit -m "fix(pyright): A 类——mixin 属性类型占位（AgentServer.session 等）+ 动态属性豁免"
```

### Task 3: B 类——NewType/枚举收口

**Files:**
- Modify: `core/agent/agent.py:640,644`、`core/input/session_routes.py:302,309,351,362,515,529,536,652,711`、
  `core/input/ws_handler.py:429`、`core/monitoring/idle_monitor.py:112,129`、
  `core/input/debug_routes/archive.py:281`、`core/input/voice_sessions.py:519`、`core/input/api_routes.py:950`
  （**loop.py:699 不在此**——Task 4 token_tracker 放宽后自动清零）

**Interfaces:**
- Consumes: `SessionId`/`MessageId`（core.schema）、契约枚举（Role/FinishReason/AckStatus/RepoCallStatus/VoiceEndReason/VoiceReason）。
- Produces: 收口点 `SessionId(x)` 等；`agent.resolve_session(protocol_version: int)`、`switch_session(sid: SessionId)` 签名收窄。

- [ ] **Step 1: 验证失败态**

Run: `PATH="/home/yomu/miniconda3/envs/Elysia/bin:$PATH" pyright 2>&1 | grep -cE "agent.py:6[34][0-9]|session_routes.py:(302|309|351|362|515|529|536|652|711)|ws_handler.py:429|idle_monitor.py|archive.py:281|voice_sessions.py:519|api_routes.py:950"`
Expected: 非 0（B 类报错存在；loop.py:699 不在此 Task，Task 4 覆盖）。

- [ ] **Step 2: NewType 收口（SessionId/MessageId）**

- `session_routes.py`：`get_by_mongo_id`/`query_page`/`release_session_attachments`/`switch_session` 的调用点
  裸 str → `SessionId(x)`（:302,309,351,362,515,529,536）。
- `archive.py:281`：`release_session_attachments(..., SessionId(sid))`。
- `voice_sessions.py:519`：`_build_user_input(self, text, es, message_id: MessageId)`（import MessageId；调用处 :494 已传 MessageId）。

- [ ] **Step 3: 签名收窄**

- `agent.py:640`：`protocol_version: int | str` → `int`（已核实全部调用方传 int——ws_handler:127、voice_sessions:524、ws_handler:532,692）。
- `agent.py:644`：`sid: SessionId | str` → `SessionId`（session_routes:529,536 已在 Step 2 收口 `SessionId(sid)`）。

- [ ] **Step 4: 枚举收口**

- `session_routes.py:652`：`role` 是 `getattr(msg, "role", None)`（None-typed）——**先收窄 `role` 再取 `.value`**
  （review 修正：None 变量是 `role` 非 `msg`）：`:639` 成员过滤后加 `if role is None: continue`，然后 `Role(role.value)`。
- `session_routes.py:711`：**`FinishReason(finish_reason) if finish_reason is not None else None`**（review 修正：
  `finish_reason` 对 failed/cancelled 代是 None，裸 `FinishReason(None)` 抛 ValueError → 500；`_generation_status`
  :691 文档明示 error 代 finish_reason=None）。
- `ws_handler.py:429`：`AckStatus(...)`；`idle_monitor.py:112,129`：`VoiceReason(...)`。
- `voice_sessions.py:328,341,543,589`：`VoiceEndReason(...)`。
- `api_routes.py:950`：`RepoCallStatus(...)`（doc status 来自 repo 枚举）。
- **`loop.py:699` 不在本 Task 修**（review 修正）：该错是 `SessionId | None` 传给 `record(session_id: SessionId)`，
  Task 4 把 `record(session_id: SessionId | None = None)` 后自动清零；且 `or SessionId("default")` 会把无目标会话
  的 token 记到 default，污染 per-session 统计——**不在此改 loop.py**。

- [ ] **Step 5: 验证**

Run: pyright 同上命令 Expected: 0；再跑 `pytest core/input/tests core/cognitive/tests -m "not slow"` 确认不回归。

- [ ] **Step 6: 提交**

```bash
git add core/agent/agent.py core/input/session_routes.py core/input/ws_handler.py core/monitoring/idle_monitor.py core/input/debug_routes/archive.py core/input/voice_sessions.py core/input/api_routes.py
git commit -m "fix(pyright): B 类——NewType/枚举收口（SessionId/Role/AckStatus/VoiceEndReason 等）+ agent 签名收窄"
```

### Task 4: C 类——可空收窄

**Files:**
- Modify: `core/input/ws_handler.py`（`_send_ack`/`_send_voice_error` 签名 + 20 调用点 + :719）、
  `core/agent/_lifecycle_services.py:136`、`core/input/debug_routes/character.py:129,191`、
  `core/input/voice_sessions.py:484,500`、`core/monitoring/token_tracker.py:79,80,81,126`、`core/desktop/bridge.py:188`
  （**session_routes.py:652 已由 Task 3 的 `if role is None: continue` + `Role(role.value)` 一并解决**——不在此重复）

**Interfaces:**
- Produces: `_send_ack`/`_send_voice_error(ws: WebSocketResponse | None)` + 内部 `if ws is None: return`
  （一处改、20 调用点自动兼容）；`token_tracker.record` 参数标注 `X | None`。

- [ ] **Step 1: 验证失败态**

Run: pyright 查 ws_handler/_lifecycle_services/character/session_routes/voice_sessions/token_tracker/bridge。
Expected: C 类报错存在。

- [ ] **Step 2: ws_handler 统一方案**

`_send_ack`（:421）/`_send_voice_error`（:392）签名 `ws: "web.WebSocketResponse"` → `ws: "web.WebSocketResponse | None"`，
函数内部第一行加 `if ws is None: return`（voice 错误路径 ws 可空，静默跳过）。20 处调用点**自动兼容**（可空传可空），无需逐个改。
`:719` `voice_mgr.commit_recording`：入口 `if voice_mgr is None: return` 或断言（查上下文）。

- [ ] **Step 3: 其余可空收窄**

- `_lifecycle_services.py:136`：`set_enabled(self._tts_enabled)`——`_tts_enabled` 是 `bool | None`：
  `if self._tts_enabled is None: return` 或回退 config（查 service_state 三态语义，`None` 回退 config）。
- `character.py:129,191`：`result.wire_dict()`——result 推断 None，`if result is None: return _protocol_error_response(...)`。
- `voice_sessions.py:484,500`：`sessionId=self._session_id`（`str | None`）——`str(x) if x else ""` 或断言（voice session 已初始化）。
- `token_tracker.py:79,80,81,126`：**标注改 `model: str | None = None`、`round_num: int | None = None`、
  `session_id: SessionId | None = None`，且 body 内 `model = model or ""` 兜底**（review 修正：只改标注会让
  :98 `self.per_model[model]` 与 :104 `session_models[model]` 新增 None-not-assignable 错——`model` 是 `dict[str,int]`
  键，须 `or ""` 兜底；`sid` 已在 :87 兜底 `session_id if ... else self.session_id` 无需改）。
  **loop.py:699 随之清零**（`record(session_id: SessionId | None)` 接受 None 调用）。
- `bridge.py:188`：`Future | None` 赋给 `Future`——`assert` 后使用。

- [ ] **Step 4: 验证**

Run: pyright 查上述文件 Expected: 0；`pytest core/input/tests core/monitoring/tests core/agent/tests -m "not slow"`。

- [ ] **Step 5: 提交**

```bash
git add core/input/ws_handler.py core/agent/_lifecycle_services.py core/input/debug_routes/character.py core/input/session_routes.py core/input/voice_sessions.py core/monitoring/token_tracker.py core/desktop/bridge.py
git commit -m "fix(pyright): C 类——可空收窄（_send_ack 签名放宽 + token_tracker 标注 X|None + 各点断言）"
```

### Task 5: D 类——契约构造/返回类型/aiohttp 泛型

**Files:**
- Modify: `core/input/api_routes.py:489,491`、`core/input/ws_handler.py:93,218,222`、
  `core/input/session_routes.py:306,320,359,377`、`core/memory/session.py:168`、`core/input/ssrf.py:129`、
  `core/input/voice_sessions.py:296`、`core/input/attachment_routes.py:168`、**`core/input/server.py:345`**、
  `core/memory/memory_reflector.py:240`、`core/tools/time/plugin.py:62`

**Interfaces:**
- Produces: 契约参数放宽（`list[dict]`→`list[TtsVoiceEntry]` 等）、tuple 解包 `str(x)`、返回类型修正。

- [ ] **Step 1: 验证失败态**

Run: pyright 查上述文件。Expected: D 类报错存在。

- [ ] **Step 2: 契约构造**

- `api_routes.py:489,491`：**定型转换**（review 修正——`backend.list_voices()` 返回 `list[dict]`，:476/:488 操作原始 dict 列表）：
  保留 `voices: list[dict]`（backend 原始，供 :476/:488 `v.get`），构造响应前转换
  `entries = [TtsVoiceEntry(**v) for v in voices]`，:489 append `entries` 的模型、:491
  `TtsVoicesResponse(voices=entries)`（TtsVoiceEntry 列表）。查实际变量名，转换点放 append 前。
- `ws_handler.py:93`：`_build_session_items(session_msgs: list[dict])` → 放宽 `list[ClientMessageDict] | list[dict]` 或 `list[Any]`。
- `ws_handler.py:218,222`：`enriched` 字典值类型**含 dict**（review 修正——`:218` 的 `summary` 值是 dict，
  非 `str | bool`）：标注 `enriched: dict[str, Any]`（值类型混合 str/bool/dict）。
- `session_routes.py:306,320,359,377`：`tuple[Unknown|None, int]` → 解包处 `str(x)` 或返回类型 `tuple[str | None, int]`（查函数签名定）。

- [ ] **Step 3: 类型修正/cast**

- `session.py:168`：`ClientToolMessage` TypedDict 构造——补全键或 `cast(ClientToolMessage, d)`。
- `ssrf.py:129`：`str(ip)` 收窄（ip 是 `str | int`）。
- `voice_sessions.py:296`：`Callback` 类型——改 `Callback[..., Coroutine[Any, Any, None]]` 或调用点适配（查 `Callback` 定义）。
- `attachment_routes.py:168`：返回类型 `Response` → `StreamResponse`（FileResponse 是 StreamResponse 子类，查函数签名）。
- **`core/input/server.py:345`**（review 修正——初稿误写 core/agent/server.py）：`_ws_active` 返回
  `_protocol_error_response(...)`（`Response`）但标注 `WebSocketResponse`——返回类型改
  `web.Response | web.WebSocketResponse`（错误路径返回 Response，正常路径返回 WebSocketResponse），查函数签名定。
- `memory_reflector.py:240`：`KMeans(n_init=10)`——sklearn stub 误报 `n_init: str`，`# type: ignore[arg-type]`。
- `time/plugin.py:62`：变量标注 `timezone | None` → `tzinfo | None`（ZoneInfo 与 timezone 都是 tzinfo 子类）。

- [ ] **Step 4: 验证**

Run: pyright 查上述文件 Expected: 0；`pytest core/input/tests core/memory/tests core/tools -m "not slow"`。

- [ ] **Step 5: 提交**

```bash
git add core/input/api_routes.py core/input/ws_handler.py core/input/session_routes.py core/memory/session.py core/input/ssrf.py core/input/voice_sessions.py core/input/attachment_routes.py core/input/server.py core/memory/memory_reflector.py core/tools/time/plugin.py
git commit -m "fix(pyright): D 类——契约参数放宽/返回类型修正/aiohttp 泛型 cast"
```

### Task 6: E 类——可空迭代兜底

**Files:**
- Modify: `core/tools/vision/plugin.py:202,211`

**Interfaces:**
- Produces: 可空迭代 `x or []` 兜底。

- [ ] **Step 1: 验证失败态**

Run: pyright 查 vision/plugin.py。Expected: reportOptionalIterable 报错存在。

- [ ] **Step 2: 修复**

`vision/plugin.py:202,211`：可空可迭代对象 `for ... in x` → `for ... in (x or [])`。

- [ ] **Step 3: 验证**

Run: pyright 查 vision/plugin.py Expected: 0；`pytest core/tools -m "not slow"`。

- [ ] **Step 4: 提交**

```bash
git add core/tools/vision/plugin.py
git commit -m "fix(pyright): E 类——vision 可空迭代 or [] 兜底"
```

### Task 7: 全量验证（门禁绿）

**Files:** 无新增（验证任务）。

- [ ] **Step 1: pyright 门禁 0 errors**

Run: `conda run -n Elysia pyright`
Expected: **0 errors**（92 处全消；若仍有，回到对应 Task 修复）。

- [ ] **Step 2: 全量回归**

```bash
make test-normal
# 或: /home/yomu/miniconda3/envs/Elysia/bin/python -m pytest -m "not slow" -q --tb=short --ignore=Module/workflow_engine/tests
make lint
```
Expected: 全绿。

- [ ] **Step 3: 提交（若无代码改动则跳过）**

```bash
git status
# 若有遗漏修复，git add + commit；否则无提交
```

## Self-Review（已执行）

1. **Spec 覆盖**：门禁（T1）、A-E 分类（T2-6）、全量验证（T7）——对应 spec §1-7 与 92 处全表。
2. **占位符扫描**：无 TBD/TODO；每 Task 含验证命令（pyright 定向 grep）与修复代码模式。
3. **类型一致性**：`SessionId(x)`/`Role(x)` 等收口模式统一；`X | None` 标注统一；cast/ignore 仅在 stub 误报处。
4. **已知耦合**：agent.py:644 收窄 ↔ session_routes:529,536 收口（T3 内原子）；ws_handler `_send_ack` 签名放宽
   （T4）自动兼容 20 调用点；pyright 是整程序检查——T1 建立时 92 为已知失败态，T7 才 0 errors；
   文件跨 Task 重叠（session_routes/ws_handler/voice_sessions 在 B/C/D）——顺序执行，各自验证清零。

## Plan Review 修复记录（code-review 后并入）

| # | Review 发现 | 修复 |
|---|------------|------|
| 1 | session_routes:711 `FinishReason(None)` 对 failed/cancelled 代抛 ValueError → 500（运行时崩溃） | 改 `FinishReason(x) if x is not None else None` |
| 2 | Task 5 错标 `core/agent/server.py:345`（实际 `core/input/server.py:345`） | 文件归属修正 + git add 修正 |
| 3 | token_tracker 只改标注会在 :98/:104 dict 键新增 None 错 | 补 `model = model or ""` 兜底 |
| 4 | broadcast_capabilities 放宽在 capabilities.py:109 新增 build_capabilities/agent.output 两处错 | `AgentServer` 补 `output` 占位 + build_capabilities 参数同步放宽 |
| 5 | api_routes 489/491 两个选项不构成（list_voices 返回 list[dict]） | 定型 `[TtsVoiceEntry(**v) for v in voices]` 转换 |
| 6 | loop.py:699 修法冗余且 `or "default"` 污染 token 归属 | 移出 T3（T4 token_tracker 放宽自动清零），不碰 loop.py |
| 7 | _lifecycle_tools:104 修法未定且改名无效（ABC 无 _send_json） | 定型 `cast(WebSocketChannel, target)` |
| 8 | ws_handler:218 `dict[str, str\|bool]` 不含 dict 值（summary） | 改 `dict[str, Any]` |
| 9 | session_routes:652 None 变量是 `role` 非 `msg` | T3 内 `if role is None: continue` + `Role(role.value)`；T4 移除重复 |
