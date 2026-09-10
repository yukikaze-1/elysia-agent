# Electron 后端改造 — 批次 4：统一 Tool Runtime 与 Desktop 工具桥（Plan）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 版本：** v0.2（修订：吸收批次 4 plan 两路 review findings——plugin_list 元数据扩展/迁移表 pending→客户端终态/注入双引用同步/bridge 构造顺序/负面隔离回归拆分/execute 委托与 paramHash 测试/desktop_runtime 断言/target_connection 填充/mime 兜底/重放版本门控/软依赖注明）
**依据 Spec：** `docs/superpowers/specs/archive/2026-08/2026-08-05-electron-backend-batch4-desktop-tools.md`（v0.2）
**前置：** 批次 0 基线 + 批次 1（认证/版本协商/错误信封）+ 批次 2（chat v2）+ 批次 3（多会话 + GenerationRegistry Mongo 持久化先例，HEAD `a1a9d2c9`）
**执行环境：** Elysia conda（`conda activate Elysia`）

---

## 概览

批次 4 把工具系统升级为 Runtime 感知（BE-040），新增 Desktop 工具桥（BE-041）让 Agent 安全请求 Electron 执行四个 P0 工具，落实结构化审批边界（BE-042）与 callId 幂等/取消/重连（BE-043）。核心组件：**DesktopToolBridge**（`core/desktop/bridge.py`：进程内 future + 按 call_id 锁 + 状态机）+ **DesktopCallRepository**（`core/storage/desktop_call_repo.py`：Mongo + 进程内缓存 + 迁移守卫）。

**硬约束**：v1 wire 逐字节不变（`tool_approval` enrich 仅 v2 生效——§7 版本门控）；`contract.generated.ts` v1 类型字节不变；现有 2176 测试全绿。**TDD：每个任务先写失败测试。**

## Global Constraints

- 禁相对导入（绝对导入，`make lint` 强制）；`__init__.py`/`__main__.py` 豁免。
- 时间戳一律 `now_iso()`（UTC ISO 8601 `+00:00`）。
- 新测试必须带 marker（unit/service），日常 `make test-quick`。
- Wire 字段统一 camelCase（callId/toolName/toolVersion/riskLevel/approvalAuthority/paramHash/expiresAt）；内部 Python/Mongo snake_case。
- 新增错误码入目录：`DESKTOP_UNAVAILABLE`(503，目录登记无生产者)/`CALL_NOT_FOUND`(404，落地于 `GET /api/tools/calls/{callId}`)/`CALL_EXPIRED`(408，目录成员)/`CALL_IDEMPOTENCY_CONFLICT`(409，目录成员)。
- **YAGNI 边界（spec §13，不得越界实现）**：不做 `desktop.command.run`；不做 Local Worker Runtime（枚举保留）；不做多 Desktop 客户端（单连接目标）；不做桥的自动重投（重连只可查/可重传结果）；不做跨离线窗口 callId 记忆（offline/expired 不参与去重，重试即新调用）；不做 outputSchema 强校验（声明性）；不做附件鉴权下载/短签名 URL（BE-070/071 批次 7）；不做 TTS 媒体附件；不做 multi-device 审批路由。
- **敏感纪律**：bridge/ws 层 logger 只记 callId/toolName/status，不打印 arguments 值；错误信封 `details` 不放本地路径/参数；附件只传后端托管 URL。
- **数据保护**：不得删除/清理生产 default 会话；测试用隔离 agent（`capture_ws_trace._make_agent_isolated` 模式）。

---

### Task 1: v2/新契约模型（frontend_contract）

**Files:**
- Modify: `frontend_contract/ws_message.py`
- Modify: `frontend_contract/rest.py`
- Generate: `frontend/src/types/contract.generated.ts`
- Test: `frontend_contract/tests/`

**Interfaces:**
- Produces（后续 Task 用）：
  - `ws_message.ToolRequestMessage`（type=`tool.request`，Server→Client）：callId/toolName/toolVersion/runtime/arguments/reason/riskLevel/permissionRequired/approvalAuthority/paramHash/expiresAt/requestId
  - `ws_message.ToolStatusMessage`（type=`tool.status`，Client→Server）：callId/status/toolName/toolVersion/runtime/paramHash/output/error/attachments/executionSummary
  - `ws_message.ToolAttachment`（{url, mimeType, label}）、`ToolStatusEnum`（accepted/rejected/running/result/error/cancelled）
  - `rest.ToolMetadata`（name/description/runtime/riskLevel/permissionRequired/approvalAuthority/version/inputSchema/outputSchema/available）、`ToolRegistryResponse`（tools[] + desktopAvailable）、`DesktopCallStatus`（callId/toolName/toolVersion/runtime/paramHash/status/createdAt/expiresAt/result/error）

- [ ] **Step 1: RED（unit）** — `frontend_contract/tests/` 新增测试：`ToolRequestMessage`/`ToolStatusMessage` round-trip（含 attachments）；`ToolStatusEnum` 六值；`ToolMetadata`/`ToolRegistryResponse`/`DesktopCallStatus` round-trip；**命名避开 debug.py 既有同名类**（`ToolMetadata` 无冲突，grep 确认）。
- [ ] **Step 2: 验证 RED** — `pytest frontend_contract/tests/ -v` FAIL（模型不存在）。
- [ ] **Step 3: 实现** — `ws_message.py` 加 3 个模型 + 枚举；`ToolStatusMessage` 加入 Client→Server 判别（现 7 种 → 8 种）；`rest.py` 加 3 个模型。
- [ ] **Step 4: 验证 GREEN** — `pytest frontend_contract/tests/ -v` PASS。
- [ ] **Step 5: gen_ts** — `make gen-types` 重生成；`git diff frontend/src/types/contract.generated.ts` **仅新增 v2/新类型，v1 类型字节不变**（`python -m frontend_contract --check` 通过）。
- [ ] **Step 6: Commit** — `feat(contract): 批次 4 契约——tool.request/tool.status + ToolMetadata/ToolRegistryResponse/DesktopCallStatus`

### Task 2: ToolPlugin 元数据 + Desktop 插件（BE-040）

**Files:**
- Modify: `Module/tool/protocol.py`（ToolPlugin 加 runtime/version/risk_level/output_schema）
- Modify: `core/tools/filesystem/plugin.py`、`core/tools/web/plugin.py`、`core/tools/memory/plugin.py`、`core/tools/time/plugin.py`、`core/tools/reminder/plugin.py`、`core/tools/runtime_config/plugin.py`、`core/tools/self_state/plugin.py`、`core/tools/vision/plugin.py`（补 risk_level）
- Create: `core/tools/desktop/plugin.py`（4 个 desktop 插件）
- Modify: `core/tools/__init__.py`（ALL_PLUGINS 追加）
- Test: `Module/tool/tests/`、`core/tools/tests/`

**Interfaces:**
- Produces（后续 Task 用）：
  - `ToolPlugin.runtime: str = "backend"`、`version: str = "1.0.0"`、`risk_level: str = "safe"`、`output_schema: dict`（默认 `{"type": "string", "description": "工具公开输出"}`）
  - `ToolPlugin.approval_authority` 属性 → `"client" if runtime in ("desktop", "local_worker") else "backend"`
  - `ToolPlugin.permission_required` 属性 → `risk_level != "safe" or (safety and safety.requires_human_approval)`
  - `desktop.file.select`（params `{}`，risk=safe，request_timeout=120）、`desktop.file.read`（path 必填，risk=medium）、`desktop.file.write`（path/content 必填，risk=high）、`desktop.screen.capture`（region 可选，risk=high，output_schema=object）——全部 `runtime="desktop"`、`version="1.0.0"`、`safety.request_timeout=120.0`（**必须**，防 SafetyPipeline 30s 截断，spec §4.4）
  - 12 个 backend 工具 risk 标注：ReadFile/ListDir=safe、WriteFile=high、WebSearch/WebFetch=medium、MemorySearch=safe、MemorySave=medium、Time/SetReminder/SelfState/DescribeImage=safe、RuntimeConfig=medium

- [ ] **Step 1: RED（unit）** — `Module/tool/tests/`：`ToolPlugin` 子类默认 `runtime=="backend"`、`approval_authority=="backend"`；设 `runtime="desktop"` 后 `approval_authority=="client"`、`permission_required` 随 risk_level 派生（safe+无审批 → False，high → True）。`core/tools/tests/test_desktop.py`：4 个 desktop 插件类名/name/runtime/risk/request_timeout 断言；desktop.file.read 的 param_model 校验 path 必填；**execute() 委托分支（review 修正，spec §11 单元要求）**：`ctx.services` 无 `desktop_bridge` → `ToolResult.fail(error_type=UNAVAILABLE)`；bridge 可用 → `bridge.request_and_wait` 被调（mock bridge）；**paramHash 确定性（review 修正）**：同参数两次计算同哈希、不同参数异哈希、canonical JSON 排序（`{"b":1,"a":2}` 与 `{"a":2,"b":1}` 同哈希）。
- [ ] **Step 2: 验证 RED** — FAIL（字段/插件不存在）。
- [ ] **Step 3: 实现** — `protocol.py` 加字段 + 2 个派生属性；8 个 backend 插件补 `risk_level` 类属性；`core/tools/desktop/plugin.py` 建 4 个插件（`execute()` 委托：`bridge = ctx.services.get("desktop_bridge")`，None 或 `not bridge.is_available()` → `ToolResult.fail(error_type=UNAVAILABLE)`；否则组装 call dict + `arguments_hash=sha256(canonical_json(arguments))` → `await bridge.request_and_wait(call=call)`）；`ALL_PLUGINS` 追加。
- [ ] **Step 4: 验证 GREEN** — `pytest Module/tool/tests/ core/tools/tests/test_desktop.py -v` PASS（`core/tools/tests/` 为随 test_desktop.py **新建的目录**——review 修正：当前不存在；`Module/tool/tests/` 现有 test_framework/test_tool_safety 可跑）。
- [ ] **Step 5: Commit** — `feat(tools): 批次 4——ToolPlugin runtime/version/risk_level/output_schema + desktop 四插件（execute 委托桥）`

### Task 3: DesktopCallRepository（Mongo + 进程内缓存 + 迁移守卫）

**Files:**
- Create: `core/storage/desktop_call_repo.py`
- Test: `core/storage/tests/test_desktop_call_repo.py`

**Interfaces:**
- Consumes：`Module/database/documentdb` 的 DocumentDB（同 `generation_repo.py` 模式）；`core/schema/utils` 的 `now_iso()`（**位置 review 修正：`def now_iso` 在 `core/schema/utils.py:28`，非 `core/utils/`**）。
- Produces（Task 5 用）：
```python
class DesktopCallRepository:
    def __init__(self, mongodb=None): self._db=mongodb; self._cache: dict[str, dict] = {}
    async def connect(self) -> bool                      # Mongo 可达 + 建索引（call_id unique；status+updated_at；TTL 30d）
    async def health_check(self) -> bool
    async def upsert(self, call: dict) -> None           # 写 cache + Mongo（离线仅写 cache + log）
    async def get(self, call_id: str) -> dict | None     # 读 cache（Mongo 可达时先 reload 到 cache？——Demo 简化：读 cache，离线不失效）
    async def query_pending(self, cutoff) -> list[dict]  # cache 中 pending/accepted/running 且未过期
    async def mark(self, call_id: str, **fields) -> bool # 迁移守卫（spec §4.1 迁移表）；返回是否成功
    async def mark_offline_by_connection(self, connection_id) -> int
    async def mark_stale_expired(self, cutoff) -> int    # pending+accepted/running 超时 → expired
    async def reload_from_db(self) -> None               # connect 后全量载入 cache
```
- **迁移守卫**（spec §4.1 表）：`mark` 校验当前 status → 新 status 合法性；终态（succeeded/failed/rejected/cancelled/expired）不可覆盖为其他终态枚举；`offline → succeeded/failed/rejected/cancelled` 合法（重传结果）；`pending → accepted/running/expired/offline` 合法；`accepted/running → 终态/offline` 合法。

- [ ] **Step 1: RED（unit）** — `test_desktop_call_repo.py`（注入 fake Mongo dict 或离线模式）：`upsert`→`get` round-trip（离线也命中 cache）；`mark` 迁移守卫——`pending→succeeded` 合法、`succeeded→failed` 拒绝、`offline→succeeded` 合法；`mark_offline_by_connection` 只标该连接的 pending/accepted/running；`mark_stale_expired` 覆盖 pending+accepted/running；离线时（mongodb=None）`upsert`/`get`/`query_pending` 全走 cache 不抛。
- [ ] **Step 2: 验证 RED** — FAIL（repo 不存在）。
- [ ] **Step 3: 实现** — 按 Interfaces 实现；cache 为唯一权威（读全走 cache）；Mongo 可达时写同步 Mongo（`dict()` 拷贝防 motor 变异，`insert_one`/`update_one(upsert)`）；离线 `_db is None` 时写仅 cache + `logger.debug`；`connect()` 建索引。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(storage): 批次 4——DesktopCallRepository（Mongo + 进程内缓存 + 迁移守卫 + 离线降级）`

### Task 4: DesktopToolBridge（锁 + future + 状态机 + 断连规则）

**Files:**
- Create: `core/desktop/__init__.py`、`core/desktop/bridge.py`
- Test: `core/desktop/tests/test_bridge.py`

**Interfaces:**
- Consumes：Task 3 repo。
- Produces（后续 Task 用）：
```python
class DesktopToolBridge:
    def __init__(self, repo, timeout: float = 120.0):
        self._repo=repo; self._timeout=timeout
        self._pending: dict[str, asyncio.Future] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._dedup: dict[tuple[str, str], str] = {}       # (tool, args_hash) → call_id
        self._connected = False; self._connection_id: str | None = None
        self._deliver: Callable[[dict], Awaitable[None]] | None = None
    def is_available(self) -> bool                       # 返回 self._connected
    async def connect_db(self) -> bool
    async def health_check(self) -> bool
    def bind_deliver(self, deliver: Callable[[dict], Awaitable[None]]) -> None
    async def on_client_connect(self, connection_id: str) -> None
    async def on_client_disconnect(self, connection_id: str) -> None
    async def request_and_wait(self, *, call: dict) -> ToolResult
    async def handle_status(self, status: dict) -> bool
    async def cancel_call(self, call_id: str) -> None
    async def get_call(self, call_id: str) -> dict | None       # async（review 修正：repo.get 为 async，同步签名不可实现）
    async def pending_calls(self) -> list[dict]                 # async（同上）
    async def save_all(self) -> None                       # no-op 预留（repo 即时 upsert）
```
- **`target_connection` 填充（review 修正，Task 8 附件持有者校验依赖）**：`request_and_wait` 组装 call dict 时**必须填 `target_connection=self._connection_id`**（spec §4.1 schema 字段）——Task 8 上传校验②「上传连接是 target_connection」依赖它；漏填则附件持有者校验恒失败。
- **核心语义**（spec §4.2）：
  - `request_and_wait`：① 锁内幂等去重（`_locks.setdefault((tool,hash), Lock())` 串行化——查 `_dedup`，pending 命中复用 callId + await 原 future，不二次投递）；② 锁内 `repo.upsert(status=pending, expires_at=now+timeout)` + `_dedup` 登记；③ **先登记 `_pending[call_id]=future` 再 `deliver`**（顺序不可颠倒，防极速 result 竞态；投递失败清理登记）；④ `await asyncio.wait_for(future, timeout)`；⑤ 超时 → 锁内 `mark expired` + `ToolResult.fail(TIMEOUT)`；⑥ 结果 → 按 spec §4.3 组装 ToolResult。
  - `handle_status`：校验 call 存在 + wire `toolName/toolVersion/runtime/paramHash` 与登记 `tool_name/tool_version/runtime/arguments_hash` 一致——**不一致 → mark failed + 立即 resolve TOOL_ERROR + return False**（篡改拒绝，不挂超时）；**终态守卫**：已终态 call 的晚到 status 仅更新 repo result/error 字段不重复 resolve；`if not future.done(): future.set_result(...)` 防 InvalidStateError；分支：accepted/running → 仅 repo；result/error/rejected/cancelled → resolve。
  - `on_client_disconnect`：repo 标 offline + `_connected=False`；pending（未接手）call → 立即 resolve UNAVAILABLE；accepted/running → 挂起到超时或晚到结果。
  - `cancel_call`：锁内 mark cancelled + `if not future.done(): set_result(CANCELLED)`。

- [ ] **Step 1: RED（unit）** — `test_bridge.py`（fake repo 注入，deliver 记录投递帧）：
  - 幂等去重：同 (tool, hash) 两次 `request_and_wait`（deliver 挂起不 resolve）→ 第二次复用 callId、`deliver` 仅调 1 次；
  - 并发去重：`asyncio.gather` 两个同参请求 → 仅 1 次投递、共享 future；
  - 极速响应：deliver 回调内同步 `handle_status{result}` → 结果不丢（先登记后投递生效）；
  - 篡改拒绝：`handle_status` 带错误 paramHash → return False + future resolve（TOOL_ERROR）+ repo mark failed；
  - 断连：pending call `on_client_disconnect` → 立即 resolve UNAVAILABLE；accepted call 断连 → 不 resolve，重连后 `handle_status{result}` → resolve + repo succeeded（offline→succeeded 合法）；
  - 超时：timeout=0.05 的 bridge，deliver 挂起 → wait_for 超时 → ToolResult.fail(TIMEOUT) + repo expired；
  - future 唯一：超时后晚到 result → 不抛 InvalidStateError、repo 记录 result 不覆盖 expired 枚举；
  - **rejected/cancelled 迁移（review 修正，spec §11 全迁移）**：`handle_status{status:rejected}` → ToolResult.fail(CANCELLED) + repo rejected；`cancel_call` → repo cancelled + future resolve(CANCELLED)。
- [ ] **Step 2: 验证 RED** — FAIL（bridge 不存在）。
- [ ] **Step 3: 实现** — 按 Interfaces 实现（含锁内去重、先登记后投递、终态守卫、断连规则）。
- [ ] **Step 4: 验证 GREEN** — `pytest core/desktop/tests/test_bridge.py -v` PASS。
- [ ] **Step 5: Commit** — `feat(desktop): 批次 4——DesktopToolBridge（幂等去重锁 + future 终态唯一 + 状态机 + 断连/重传规则）`

### Task 5: ToolManager 元数据导出 + tool_approval 版本门控（BE-040/042）

**Files:**
- Modify: `Module/tool/manager.py`
- Test: `Module/tool/tests/test_metadata.py`

**Interfaces:**
- Consumes：Task 2 的 ToolPlugin 字段。
- Produces（Task 7/8 用）：
```python
@dataclass(frozen=True)
class ToolMetadata:  # manager.py 内定义
    name: str; description: str; runtime: str; risk_level: str
    permission_required: bool; approval_authority: str
    version: str; input_schema: dict; output_schema: dict; available: bool

def registry_metadata(self, desktop_available: bool = False) -> list[ToolMetadata]  # available：desktop 工具=desktop_available，backend 恒 true
def definitions(self, format: str = "openai", filter_runtime: Callable[[str], bool] | None = None) -> list[dict]
```
- **`plugin_list` 元数据扩展（review 修正，spec §3.2 交付物）**：`plugin_list` property 每项补 `runtime`/`version`/`risk_level`/`permission_required`/`approval_authority`/`available`（desktop 工具=desktop_available、backend 恒 true；复用 `registry_metadata` 的派生逻辑）。
- **tool_approval 版本门控**：`set_hitl_broadcast(output_manager)` 的 `_broadcast` 回调改为两路广播——v1 通道发原样 `{tool, params: str(params)[:200], request_id}`（逐字节不变）；v2 通道发 enrich 版（+ `summary`（结构化参数摘要：逐参数 type/preview/truncated）+ `runtime/version/riskLevel/permissionRequired/approvalAuthority`，取自 `_plugins[tool_name]` 元数据）。

- [ ] **Step 1: RED（unit）** — `test_metadata.py`：`registry_metadata()` 返回 12 个 backend + 4 个 desktop（`available` 随 `desktop_available` 翻转，backend 恒 true）；`ToolMetadata` 字段完整（runtime/risk/approvalAuthority/permissionRequired/version）；`definitions(filter_runtime=lambda rt: rt!="desktop")` 过滤后无 desktop 工具、不传过滤全量；**`plugin_list` 每项含 runtime/version/risk_level/permission_required/approval_authority/available**（desktop 工具 available 随 desktop_available 翻转）。**版本门控**：fake output_manager 记录各 channel（protocol_version 1/2）收到的 command params——v1 收到 `{tool,params,request_id}`（无 summary/runtime），v2 收到 enrich 版。
- [ ] **Step 2: 验证 RED** — FAIL（方法/行为不存在）。
- [ ] **Step 3: 实现** — `ToolMetadata` + `registry_metadata`（遍历 `_plugins`，input_schema 复用 `definitions()` 的 `_flatten_refs` 路径，派生字段调 Task 2 属性）；`definitions` 加 `filter_runtime`（过滤 desktop）；**`plugin_list` 每项补元数据字段**；`set_hitl_broadcast` 改两路广播（按 `output_manager._split_versions` 或回调内判断 channel protocol_version）。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(tools): 批次 4——ToolMetadata 导出 + definitions 运行时过滤 + tool_approval 版本门控`

### Task 6: Agent / ToolAdapter / 生命周期集成

**Files:**
- Modify: `core/adapter/tool_adapter.py`（`__init__` 加 `desktop_bridge=None`；`definitions()` 注入 filter_runtime）
- Modify: `core/agent/agent.py`（构造 bridge/repo + `desktop_bridge` 属性 + `_start_persistence` 挂点）
- Modify: `core/agent/_lifecycle_tools.py`（`_start_tool_context`：bridge.bind_deliver + `start(extra_services={"desktop_bridge": bridge})`；**不建桥**——bridge 在 `Agent.__init__` 创建）
- Modify: `core/agent/_lifecycle_persistence.py`（`_start_persistence` 中 `await bridge.connect_db()`；`_stop_persistence` 中 `save_all()`）
- Test: `core/agent/tests/`

**Interfaces:**
- Consumes：Task 3 repo + Task 4 bridge。
- Produces（Task 7/8 用）：`agent.desktop_bridge: DesktopToolBridge`（只读属性）；ToolAdapter.definitions 自动按 `desktop_bridge.is_available()` 过滤 desktop 工具。

- [ ] **Step 1: RED（unit/service）** — `core/agent/tests/`：Agent 构造后 `agent.desktop_bridge` 存在、`is_available is False`（无连接）；ToolAdapter `definitions()` 无 desktop 工具（bridge 未连接）。
- [ ] **Step 2: 验证 RED** — FAIL（属性/过滤不存在）。
- [ ] **Step 3: 实现** — **构造顺序（review 修正：bridge 必须早于 ToolAdapter 与 `_start_persistence`）**：`ToolAdapter.__init__` 加 `desktop_bridge=None`，`definitions()` 内 `filter_runtime = (lambda rt: rt != "desktop" or (desktop_bridge is not None and desktop_bridge.is_available()))` 传给 manager；**`Agent.__init__` 内先创建 `DesktopCallRepository` + `DesktopToolBridge`，再构造 ToolAdapter 传 bridge（构造绑定）**——bridge 在 agent.py:158 ToolAdapter 实例化**之前**、且早于 `_start_persistence`（lifecycle 启动顺序：`_start_persistence` 先于 `_STARTUP_STEPS`）；`_start_tool_context` **不建桥**（仅 `bind_deliver`（deliver 回调：遍历 `agent.output._channels` 找 `client_platform=="electron"` 的 channel → `_send_json`）+ `start(extra_services={"desktop_bridge": bridge})`（execute 运行时注入））；`_start_persistence` 中 `await bridge.connect_db()`（bridge 已存在）；`_stop_persistence` 中 `save_all()`。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(agent): 批次 4——desktop_bridge 构造/注入/生命周期 + ToolAdapter 过滤集成`

### Task 7: WS 协议扩展（X-Client-Platform + tool.status + deliver）

**Files:**
- Modify: `core/input/ws_handler.py`
- Modify: `Module/output/websocket.py`（WebSocketChannel 加 `client_platform: str = ""`）
- Modify: `core/input/capabilities.py`（`desktop_runtime=True`）
- Test: `core/input/tests/`

**Interfaces:**
- Consumes：Task 4 bridge + Task 6 agent 集成。
- 行为：
  - `handle_ws` 解析 `X-Client-Platform` 头 → `WebSocketChannel.client_platform`；v2 + electron 连接：`await agent.desktop_bridge.on_client_connect(conn_id)`（`conn_id = conn_<uuid4 hex[:12]>`）；断开（`finally`）→ `on_client_disconnect`；
  - **`connection_id` 下发（Task 8 review 修正，附件持有者校验闭环）**：v2 electron 连接的 `server_info` 帧带 `connectionId`（= 握手 conn_id）；`ServerInfoMessage` 契约增可选 `connectionId` 字段（Task 1 未含，本任务补）；Electron 据此填 `X-Client-Connection` 头；非 electron 恒省略；
  - 收到 `tool.status` 消息：仅 v2 连接处理 → `await agent.desktop_bridge.handle_status(data)`；v1 忽略；
  - **deliver 回调**（Task 6 注册）：定位 `client_platform=="electron"` 的 channel，构造 `ToolRequestMessage(...).wire_dict()` → `_send_json`；
  - **pending 审批重放版本门控（review 修正：现有 ws_handler 建连重放恒发 v1 格式 `{tool, params: str(params)[:200], request_id}`）**——v2 连接重放 pending approvals 时发 enrich 版（复用 Task 5 版本门控的 enrich 逻辑：+ summary/runtime/version/riskLevel/approvalAuthority）；v1 连接重放原样；
  - `capabilities.py`：`desktop_runtime=True`（spec D-s）。

- [ ] **Step 1: RED（service，正向）** — `core/input/tests/`：**fake desktop 客户端安全往返**（复用批次 3 隔离模式）：WS 连接带 `X-Client-Platform: electron` + `X-Protocol-Version: 2` → 断言建连后 `bridge.is_available() is True`；发 `tool.status{...}` → `handle_status` 被调（bridge 记录）。
- [ ] **Step 2: 验证 RED** — FAIL（platform 解析/handle_status 分支不存在）。
- [ ] **Step 3: 实现** — 按 Interfaces 实现（platform 头解析 + `client_platform` 注入 + `on_client_connect/disconnect` + `tool.status` 分支 + `desktop_runtime=True`）。
- [ ] **Step 4: 验证 GREEN** — 正向断言 PASS。
- [ ] **Step 5: 补负面隔离回归测试（review 修正：实现前因功能缺失而平凡 PASS，属回归非 RED）** — v1 连接带伪冒 platform 头 → 不收 tool.request（deliver 无目标）、`tool.status` 忽略；v2 无 platform 头（web）→ `on_client_connect` 不被调；**server_info 连接帧断言 `features.desktop_runtime is True`**（spec §11 回归项）；**pending 审批重放**：有挂起审批时 v2 连接建连收到 enrich 版 tool_approval、v1 收到原样。跑通 PASS。
- [ ] **Step 6: Commit** — `feat(input): 批次 4——X-Client-Platform 握手 + tool.status 路由 + 负面隔离回归 + desktop_runtime=true`

### Task 8: REST 注册表 + 附件上传（BE-042/043）

**Files:**
- Modify: `core/input/api_routes.py`（或新建 `tool_routes.py`）
- Test: `core/input/tests/`

**Interfaces:**
- Consumes：Task 3 repo + Task 4 bridge + Task 5 metadata。
- 端点：
  - `GET /api/tools` → `ToolRegistryResponse`（`registry_metadata(desktop_available=bridge.is_available)`）；认证 401（AGENT_ACCESS_TOKEN 配置时）；
  - `GET /api/tools/calls/{callId}` → `DesktopCallStatus`（`await bridge.get_call(callId)`；未知 → 404 `CALL_NOT_FOUND`；status 用 repo 枚举）；
  - `POST /api/uploads/desktop/{callId}` → multipart 附件落盘 `/uploads/desktop/<callId>.<ext>` → `{url}`；**持有者校验**：① callId 存在（404 `CALL_NOT_FOUND`）；② 上传连接是 `target_connection`（403 `INVALID_REQUEST`）；③ call 非终态（终态拒绝）；类型/尺寸白名单沿用 `image_max_mb`/`image_allowed_types` + `application/octet-stream`。

- [ ] **Step 1: RED（service）** — `core/input/tests/`：`GET /api/tools` 返回 4 个 desktop 工具（runtime=desktop/risk/approvalAuthority=client/available）+ 12 backend；认证 401；`GET /api/tools/calls/{callId}` 正常/404；`POST /api/uploads/desktop/{callId}` 正常 → url + **未知 callId → 404、非 target 连接 → 403、终态 call → 拒绝**。
- [ ] **Step 2: 验证 RED** — FAIL（端点不存在）。
- [ ] **Step 3: 实现** — 按 Interfaces 实现（仿 `image_upload_handler` 结构：`read_chunk` 限流、`cfg.server.image_max_mb`、白名单）。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(api): 批次 4——GET /api/tools + calls/{callId} + 附件上传（持有者校验）`

### Task 9: loop 落盘 ImagePart + cancel 贯通

**Files:**
- Modify: `core/cognitive/loop.py`（`_execute_tool_round` 落盘 images → ImagePart；`cancel_current` 后 `bridge.cancel_call`）
- Test: `core/cognitive/tests/`

**Interfaces:**
- Consumes：Task 4 bridge。
- 行为：
  - `_execute_tool_round` 组装 `ToolResultMessage` 时：`tool_result.images` 非空 → `content=[TextPart(tool_result.llm_summary or content), *[ImagePart(src=item.url, mime=item.mime_type) for item in tool_result.images]]`（`core/schema/part.py` 已支持）；回放/搜索/资源索引自动吃到（SessionMessageItem.content 支持 list[ContentPart]）；
  - **`ImagePart.mime` 兜底（review 修正：`ImagePart.mime` 是 `ImageMime` 枚举，仅 image/png/jpeg/gif/webp）**——`MediaItem.mime_type` 默认空串、Task 8 白名单含 `application/octet-stream`（非图片）会触发 `ValidationError` 拖垮整轮工具结果。映射规则：`mime_type` 在 `ImageMime` 枚举内 → 原样；空/非图片 mime → **过滤**（非图片附件不落 `ImagePart`，仅文本摘要）；`desktop.screen.capture` 产物（image/png）走 ImagePart。
  - loop 中断/取消回合时对在途 desktop 调用 `await bridge.cancel_call(call_id)`（interrupt 语义）。

- [ ] **Step 1: RED（unit/service）** — `core/cognitive/tests/`：mock ToolResult 带 `images=[MediaItem(url="/uploads/desktop/x.png")]` → `_execute_tool_round` 落盘消息 content 含 `ImagePart`（type=image/src=url）；**mime 兜底**：`MediaItem(mime_type="")` → 不抛 ValidationError（过滤/回退）；取消回合 → `bridge.cancel_call` 被调。
- [ ] **Step 2: 验证 RED** — FAIL（images 被丢弃/cancel 未贯通）。
- [ ] **Step 3: 实现** — 按 Interfaces 实现（`_execute_tool_round` 落盘分支；`_handle_interrupt` 挂 `cancel_call`）。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(loop): 批次 4——ToolResult.images → ImagePart 落盘 + desktop cancel 贯通`

### Task 10: service 测试收敛 + 文档

**Files:**
- Modify: `core/input/tests/`、`core/cognitive/tests/`、`core/desktop/tests/`（补齐矩阵）
- Modify: `docs/electron-backend-protocol-v2.md`（§9.1 错误码目录增补）
- Modify: `docs/electron-backend-progress.md`（批次 4 结果）
- Modify: `docs/electron-backend-security.md`（已知缺口更新：desktop 附件鉴权）
- Test: 全量

**Interfaces:**
- 本任务无新接口，收敛 spec §11 测试矩阵缺口。

- [ ] **Step 1: 补齐 service 测试** — spec §11 矩阵逐项（**大部分行为已在 Task 2-9 实现，本步为收敛补测非新功能**——review 修正：补测路径有实现兜底，不做独立 RED 循环；若补测暴露未实现行为，回补对应 task 的实现步骤再 GREEN）：fake desktop 往返含多轮 tool-calling fake LLM——**新增测试基建**：`_make_agent_with` 变体产 `stream_think` tool_calls + 结果后文本回复；**`_inject_fake_storage` 扩展注入 `DesktopCallRepository` 且必须同步替换 `agent.desktop_bridge._repo`**（review 修正：bridge 在 `Agent.__init__` 构造时绑定 repo，只换 `agent._desktop_call_repo` 不同步 `bridge._repo` 会致 service 测试写真实 Mongo——仿 `_inject_fake_storage` 对 generation registry 的双引用同步先例）；断连/重连（pending 立即 UNAVAILABLE / accepted→offline→重传 result 恢复）；幂等复用；篡改拒绝；`GET /api/tools` 认证 + desktopAvailable 翻转；LLM 暴露过滤（连接/断连，**definitions() 与 plugin_list 双断言**）；tool_approval 版本门控（v2 enrich / v1 原样）；附件持有者校验；**desktop_runtime feature=true 断言**（server_info 连接帧）。
- [ ] **Step 2: 回归** — `make test-quick`（2176+ 新增）全绿；`make lint`；`make check-types`（contract.generated.ts 漂移门禁）。
- [ ] **Step 3: 文档** — protocol-v2 §9.1 增补 4 码 + tool.request/tool.status 契约节；progress 批次 4 结果节；security 已知缺口更新（`/uploads/desktop/*` 附件鉴权）。
- [ ] **Step 4: Commit** — `docs+batch4: 回归收敛 + 测试矩阵 + 协议/进度/安全文档`

---

## 执行顺序与依赖

`Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9 → Task 10`（严格顺序，前序 produce 的接口被后序 consume；Task 4 bridge 依赖 Task 3 repo；Task 5 依赖 Task 2 字段；Task 6 依赖 3/4/5；Task 7/8/9 依赖 6）。**软依赖（review 修正）**：Task 2 的 desktop 插件 `execute()` 前向引用 Task 4 的 `bridge.request_and_wait`/`is_available`——插件经 `ctx.services` 运行时取 bridge（`None` 守卫使 Task 2 测试不触发委托路径），按 Task 4 Interfaces 文档化签名 duck-typed 编码即可，不构成构建阻塞；Task 4 若改签名会静默破坏，故 Task 2 严格按本 plan Task 4 Interfaces 签名写。

## 验收（docs/14 场景 4）

四个 Desktop P0 工具完成安全往返：electron 客户端连接 → GET /api/tools 元数据 → LLM 选 desktop.file.read → tool.request 定向投递（paramHash/expiresAt）→ Electron 回 accepted/running/result → Agent 拿 ToolResult + ImagePart 落盘；write_file 审批 v2 enrich / v1 原样；同参并发去重单投、篡改拒绝及时失败、断连→offline→重连重传 result 恢复；`desktop.command.run` 不注册、沙盒 read/write 仍 backend。
