# Electron 后端改造 — 批次 4：统一 Tool Runtime 与 Desktop 工具桥（Spec）

**Spec 版本：** v0.2（修订：吸收批次 4 四角度 review 全部 findings——状态迁移表 + 终态守卫 + 幂等去重锁 + future 唯一 resolve + 篡改拒绝立即 resolve + 断连规则 + 进程内缓存 + request_timeout + ImagePart 落盘 + tool_approval 版本门控 + 附件持有者校验 + 测试基建声明）
**文档日期：** 2026-08-05
**依据：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`（BE-040~043）与 `docs/15-Cyrene-后端改造执行上下文与Prompt-v0.1.md`（§六批次 4、§七 Desktop 工具请求领域结构）
**前置：** 批次 0 基线 + 批次 1（认证/版本协商/错误信封）+ 批次 2（结构化聊天 v2）+ 批次 3（多会话/历史/搜索/资源 + GenerationRegistry Mongo 持久化先例）
**目标 HEAD：** `18c167dd`

---

## 1. 目的

把后端工具系统从"全部 Backend Runtime"升级为 **Runtime 感知的统一工具系统**：工具声明携带 runtime/riskLevel/version/permissionRequired/approvalAuthority/schema；新增 **Desktop 工具桥**，让 Agent 能安全请求 Electron Main 在用户电脑执行四个 P0 Desktop 工具（`desktop.file.select/read/write` + `desktop.screen.capture`），完成"请求 → 客户端校验 → 用户审批 → 执行 → 结果回传"安全往返；并落实工具级 `callId` 幂等、取消与重连恢复。

**兼容硬约束**：v1 客户端 wire 与现状**逐字节一致**；现有 2176 测试全绿；`tool_approval` 消息**类型不变**（enrich 仅 v2 生效、v1 收原样字段，前端零破坏——§7 版本门控）；`desktop.command.run` **不注册**；服务器沙盒 `read_file/write_file` 继续标 Backend、**不伪装 Desktop**（docs/15 §三.1）。

## 2. 已定决策

| 决策 | 结论 |
|---|---|
| D-a | **桥传输：复用聊天 WS（v2）**。新增契约消息 `tool.request`（Server→Client）+ `tool.status`（Client→Server），经现有 WS 连接定向投递。基建最少、与现有 `tool_approval` 广播路径同源；单 Electron Main 场景够用。 |
| D-b | **调用注册表：Mongo 持久化 + 进程内缓存**。新增 `desktop_calls` 集合（持久化模式同 GenerationRegistry 的 Mongo + 离线降级，但**读路径自建进程内缓存**——review 修正：既有 GenerationRepo 离线读返回 None、无缓存，本 repo 不沿用，保证幂等去重不因 Mongo 离线失效）；WS 重连与后端重启后 callId 状态/结果均可查询。 |
| D-c | **审批模型：两条独立流程**。后端工具审批沿用 `tool_approval`（enrich 结构化参数摘要/version/riskLevel/approvalAuthority）；Desktop 走独立 `tool.request`/`tool.status`（approvalAuthority=client，Electron 自管本地授权）。BE-042 边界显式、不破坏现有前端审批弹窗。 |
| D-d | **附件：后端持久化**。`desktop.screen.capture` 等结果附件字节回传后端 → 落盘 `/uploads/` → 返回可访问 URL + `MediaItem.images`，聊天可显示 + llm_summary。 |
| D-e | **实现位置**：4 个 Desktop 工具作为 `core/tools/desktop/plugin.py` 的 `ToolPlugin` 子类（`runtime="desktop"`，`execute()` 委托桥）；桥在 `core/desktop/bridge.py`，调用注册表在 `core/storage/desktop_call_repo.py`（与 GenerationRepo 分层一致）。注册表/LLM 暴露/权限体系统一。 |
| D-f | **版本**：`ToolPlugin` 显式类属性 `version: str = "1.0.0"`（不自动派生；docs/15 §七示例 `"1"` 与 `"1.0.0"` 同为版本字符串，采用语义化版本）。 |
| D-g | **riskLevel：显式字段**（safe/medium/high）。Desktop：`file.select`=safe、`file.read`=medium、`file.write`=high、`screen.capture`=high；Backend 现有工具逐个标注（§3.3）。 |
| D-h | **approvalAuthority 派生**：`"client" if runtime in ("desktop", "local_worker") else "backend"`。Backend 工具即使需人审批，approvalAuthority 仍 = backend（执行主体在后端；客户端弹窗审批不产生 Desktop 本地授权——BE-042）。local_worker 当前无实现，派生规则先行对齐 BE-042「Desktop/Local Worker 必须 client」。 |
| D-i | **permissionRequired 派生**：`risk_level != "safe" or safety.requires_human_approval`。 |
| D-j | **LLM 暴露：仅 Desktop 客户端连接时**。`bridge.is_available()` = false 时，4 个 desktop 工具从 `definitions()`（LLM schema）与工具面板 `plugin_list` 移除/标不可用；避免 LLM 选不可用工具。 |
| D-k | **注册表导出：认证 REST `GET /api/tools`**。返回完整元数据（runtime/risk/version/approvalAuthority/inputSchema/outputSchema + desktopAvailable）供 Electron 校验「只接受已注册工具和版本」（BE-042）。 |
| D-l | **Desktop 客户端标识：WS 握手头 `X-Client-Platform: electron`**。bridge 定向投递给该连接；浏览器（web）客户端不收 desktop 请求。Demo 单 Main 场景天然满足「目标连接/设备」。 |
| D-m | **Client→Server 是状态帧集合**（docs/15 §六）：`tool.status{status}` 判别消息覆盖 `accepted/rejected/running/result/error/cancelled` 六态 + 各自 payload。非单一 result。 |
| D-n | **请求字段对齐 docs/15 §七**：`callId/toolName/toolVersion/runtime/arguments/reason/riskLevel/permissionRequired/approvalAuthority/expiresAt`；另增 `paramHash`（规范化参数摘要 SHA-256，BE-043「参数摘要变化拒绝」的权威依据）+ `requestId`（关联 ID，批次 1 错误信封惯例）。 |
| D-o | **断连语义（review 修正）**：目标连接断开 → call 标 `offline`（可查）。**未接手**（pending）的 call **立即 resolve UNAVAILABLE**（Agent 回合及时收尾，无副作用已发生）；**已接手**（accepted/running）的 call **不自动取消**（Electron 可能已副作用）、future 挂起到超时或**晚到结果**（Electron 重连后重传 `result` → `offline → succeeded`，BE-043「重传结果」闭环）。**重连不自动重投执行请求**（防重复副作用），仅可查。 |
| D-p | **敏感纪律**（docs/15 §四.14）：`desktop_calls` 集合存完整 arguments（审计/幂等必需）；但 bridge/ws 层 **logger 只记 callId/toolName/status，不打印 arguments 值**；错误信封 `details` 不放本地路径/参数；附件只传后端托管 URL。**paramHash 仅覆盖 `arguments`**（result 的 output/attachments 不在哈希保护内——Demo 内网可接受，远程强制 TLS 反代兜底，显式认定该取舍）。 |
| D-q | **时间戳一律 `now_iso()`**（UTC ISO 8601，批次 0 约定）。 |
| D-r | **错误码增补**：`DESKTOP_UNAVAILABLE`（503，无 desktop 客户端执行时）、`CALL_NOT_FOUND`（404，查询未知 callId）、`CALL_EXPIRED`（408，调用超时/过期）、`CALL_IDEMPOTENCY_CONFLICT`（409，同 callId 参数被篡改）。 |
| D-s | **`desktop_runtime` feature 置 true**（`core/input/capabilities.py`，批次 4 交付后）。 |

## 3. Runtime 感知的 Tool Registry（BE-040）

### 3.1 ToolPlugin 元数据扩展（`Module/tool/protocol.py`）

```python
class ToolPlugin:
    # 现有: name / description / param_model / prompt_hint / capabilities / safety
    runtime: str = "backend"      # "backend" | "desktop" | "local_worker"（枚举保留，local_worker 无实现）
    version: str = "1.0.0"        # 显式版本，schema 变更时手动 bump
    risk_level: str = "safe"      # "safe" | "medium" | "high"
```

- **派生属性**（`ToolPlugin` 提供 `metadata()` 方法，或 ToolManager 侧统一派生）：
  - `approval_authority` = `"client" if runtime in ("desktop", "local_worker") else "backend"`（D-h，BE-042「Desktop/Local Worker 必须 client」）
  - `permission_required` = `risk_level != "safe" or (safety and safety.requires_human_approval)`（D-i）
- **inputSchema**：复用现有 `param_model` → JSON Schema（`ToolManager.definitions()` 的 schema 路径，含 `_flatten_refs`）。
- **outputSchema**：新增 `ToolPlugin.output_schema` 类属性（默认 `{"type": "string", "description": "工具公开输出"}`）；`desktop.screen.capture` → `{"type": "object", "description": "包含 attachments 的媒体结果"}`。**review 修正（声明性死重显式认定）**：批次 4 内 outputSchema **无后端消费者**（结果渲染走附件 URL / `MediaItem`；`ToolManager.definitions()` 只导出 inputSchema），12 个 backend 工具填默认值属噪音——字段保留以兑现 BE-040 清单，但不做强校验、不驱动任何逻辑；**output 形态（媒体/附件结构化）由批次 7 定**（届时再填实）。
- 现有 12 个 Backend 工具：`runtime="backend"` 继承默认值**零改动**；**仅需按 §3.3 补 `risk_level` 标注**（其余元数据全部走默认）。

### 3.2 ToolManager 元数据导出

```python
# Module/tool/manager.py
@dataclass(frozen=True)
class ToolMetadata:                       # 序列化给客户端校验（BE-042）
    name: str
    description: str
    runtime: str                          # backend|desktop|local_worker
    risk_level: str                       # safe|medium|high
    permission_required: bool
    approval_authority: str               # backend|client
    version: str
    input_schema: dict
    output_schema: dict
    available: bool                       # desktop 工具 = bridge.is_available()；backend 恒 true

def registry_metadata(self, desktop_available: bool = False) -> list[ToolMetadata]:
    """遍历 _plugins，返回完整元数据。desktop_available=false 时 desktop 工具仍列出（available=false，供客户端知晓存在但不可调）。"""
```

- **LLM schema 过滤（D-j）**：`ToolManager.definitions()` 增加可选参数 `filter_runtime: Callable[[str], bool] | None = None`（默认 None = 全量）——**`ToolExecutor` protocol 签名不动**，由 `ToolAdapter.definitions()` 内部注入 `lambda rt: rt != "desktop" or bridge.is_available()`（ToolAdapter 持有 bridge 引用）；desktop 未连接时 desktop 工具不出现在 LLM function schema 中。
- **工具面板 `plugin_list`**：每项增加 `runtime`/`version`/`risk_level`/`permission_required`/`approval_authority` + `available`（desktop 工具 = bridge.is_available()，backend 恒 true）。

### 3.3 Backend 工具 risk_level 标注（D-g）

| 工具 | risk_level | 依据 |
|---|---|---|
| `ReadFilePlugin` / `ListDirPlugin` | safe | 沙盒只读 |
| `WriteFilePlugin` | high | 写沙盒 + `requires_human_approval=True` 已有 |
| `WebSearchPlugin` / `WebFetchPlugin` | medium | 外部网络 |
| `MemorySearchPlugin` | safe | 只读检索 |
| `MemorySavePlugin` | medium | 写长期记忆 |
| `TimePlugin` / `SetReminderPlugin` / `SelfStatePlugin` / `DescribeImagePlugin` | safe | 无外部副作用 |
| `RuntimeConfigPlugin`（get_runtime_config） | medium | 读进程内存/服务配置/后台任务状态 |

## 4. Desktop 工具桥（BE-041/043）

### 4.1 DesktopCallRepository（`core/storage/desktop_call_repo.py`，Mongo 持久化）

新集合 **`desktop_calls`**（持久化模式沿用 `generation_repo.py`：Mongo 可用则读写、离线降级不阻塞；**读路径自建进程内缓存**，见下）。

```json
{
  "call_id": "call_<uuid4 hex[:12]>",
  "tool_name": "desktop.file.read",
  "tool_version": "1.0.0",
  "runtime": "desktop",
  "arguments": {"path": "/home/user/doc.txt"},
  "arguments_hash": "sha256...",
  "reason": "用户请求分析所选文件",
  "risk_level": "medium",
  "permission_required": true,
  "approval_authority": "client",
  "status": "pending|accepted|running|succeeded|failed|rejected|cancelled|expired|offline",
  "target_connection": "conn_<uuid4 hex[:12]>",
  "created_at": "…", "expires_at": "…", "updated_at": "…",
  "result": null | {"output": "…", "attachments": [{"url": "/uploads/…", "mime_type": "…", "label": "…"}], "execution_summary": "…"},
  "error": null | {"code": "…", "message": "…"}
}
```

- 索引：`call_id` unique；`status + updated_at`；TTL 30 天（沿用 session/generation TTL 模式）。
- 方法：`upsert(call)` / `get(call_id)` / `query_pending(cutoff)` / `mark(call_id, **fields)` / `mark_offline_by_connection(connection_id)` / `mark_stale_expired(cutoff)`（**启动清理超时孤儿**：pending + accepted/running 中 `updated_at < cutoff` 的批量标 expired——review 补充，避免后端重启后遗留 running 态 call 滞留到 30d TTL）。
- **进程内缓存（review 修正）**：既有 `GenerationRepository` 离线时读返回 None、无内存 fallback——**本 repo 不沿用该行为**。新增 `_cache: dict[call_id, dict]`（进程内全量写入缓存，写时同步 cache+Mongo；读优先 cache）；Mongo 离线时 `upsert`/`mark` 仅写 cache + log、`get`/`query_pending` 从 cache 返回。**幂等去重/状态查询不因 Mongo 离线而失效**（桥进程内完成往返）。启动时 `connect_db()` 成功则全量 reload 入 cache。
- **状态迁移表（review 修正，BE-043 终态唯一）**：

  | 迁移 | 触发 |
  |---|---|
  | `pending → accepted/running` | 客户端 `tool.status{accepted/running}`（Electron 已接手） |
  | `accepted ↔ running` | **客户端阶段推进刷新**（review 修正：Electron 回 `accepted→running→result` 序列时，`accepted→running` 须合法——迁移守卫放行双向） |
  | `pending → succeeded/failed/rejected/cancelled` | **客户端终态直达**（review 修正：§4.2 先登记 future 再投递，Electron 极速响应时 call 仍 pending 即回 `result/error/…`——迁移守卫允许 pending 直达终态） |
  | `pending → expired` | `request_and_wait` 超时（未接手） |
  | `pending → offline` | 断连（未接手；future 立即 resolve UNAVAILABLE，§4.2） |
  | `accepted/running → succeeded/failed/rejected/cancelled` | 客户端 `tool.status{result/error/rejected/cancelled}` |
  | `accepted/running → expired` | 超时（客户端未回终态） |
  | `accepted/running → offline` | 断连（已接手未回；future 挂起到超时或晚到结果） |
  | `offline → succeeded/failed/rejected/cancelled` | **晚到结果**（Electron 重连后重传，BE-043「重传结果」） |
  | `offline → expired` | 超时窗口耗尽 |
  | 终态（succeeded/failed/rejected/cancelled/expired） | **不可再迁移**——晚到 `tool.status` 仅更新 `result`/`error` 字段（记录真实结果供查询），不覆盖终态枚举 |

  `mark(call_id, **fields)` 落地时带迁移守卫：非法迁移（终态→终态枚举覆盖）拒绝；`offline → 终态` 合法（重传结果闭环）。

  **offline 为准终态（review 修正，显式声明）**：运行时**无主动过期定时器**——`offline → expired` 迁移表项仅为语义完备（若未来加定时器）；实际 offline 状态靠 30d TTL 兜底回收（Electron 永不回来时以 offline 可查形态滞留至 TTL）。这是 D-o「离线不自动取消、可查」的刻意结果，实现时**不要**为其加自动过期（会破坏「可查准终态」语义）。

### 4.2 DesktopToolBridge（`core/desktop/bridge.py`）

```python
class DesktopToolBridge:
    def __init__(self, repo: DesktopCallRepository, timeout: float = 120.0):
        self._repo = repo
        self._timeout = timeout
        self._pending: dict[call_id, asyncio.Future] = {}   # 进程内 await 等待
        self._locks: dict[call_id, asyncio.Lock] = {}        # 按 call_id 状态闩（review 修正：幂等/终态守卫）
        self._dedup: dict[tuple[tool, hash], call_id] = {}   # 进程内 (tool, args_hash) → call_id 去重索引（review 修正）
        self._connected: bool = False
        self._connection_id: str | None = None
        self._deliver: Callable[[dict], Awaitable[None]] | None = None

    # 生命周期（_lifecycle_tools.py / ws_handler 挂点）
    async def connect_db(self) -> bool                       # _start_persistence 中
    async def health_check(self) -> bool
    def bind_deliver(self, deliver: Callable[[dict], Awaitable[None]]) -> None
        # 投递回调：agent 层注入，内部定位 desktop channel（client_platform=="electron"）→ _send_json(tool.request)

    # 连接状态（ws_handler 握手/断开挂点）
    async def on_client_connect(self, connection_id: str) -> None   # 标 connected + 记录 connection_id
    async def on_client_disconnect(self, connection_id: str) -> None # 见下方「断连规则」（review 修正）
    def is_available(self) -> bool                            # 返回 self._connected

    # 调用生命周期
    async def request_and_wait(self, *, call: dict) -> ToolResult
        # ① **幂等去重（锁内完成，review 修正防竞态双投）**：
        #    `async with self._locks.setdefault(key, Lock())` 串行化同 (tool, args_hash) 的并发请求——
        #    查重 + 登记 + 投递在锁内（无 await 让步点竞态）；pending 中同 (tool, hash) 已有 →
        #    复用原 callId，await 原 future，不二次投递（防重复弹窗，BE-043）。
        #    offline/expired 不参与去重（断连后原弹窗不可恢复，重试即新调用——§12 显式认定该取舍，
        #    配合「断连立即 resolve」使重试在 is_available=false 时立即 UNAVAILABLE，不重复副作用）。
        # ② 锁内 repo.upsert（status=pending, expires_at=now+timeout）+ _dedup 登记
        # ③ **先登记 future 再投递（review 修正：顺序不可颠倒）**——`self._pending[call_id] = future`
        #    必须先于 `deliver(tool.request)`；否则 Electron 极速回 status 时 handle_status 在登记前
        #    执行 → `_pending[call_id]` 不存在 → 结果被丢弃、future 挂到超时。投递失败再清理登记。
        # ④ await asyncio.wait_for(future, timeout)
        # ⑤ 超时 → 锁内 mark expired（含守卫）→ ToolResult.fail(error_type=TIMEOUT)
        # ⑥ 结果回传 → 按 status 组装 ToolResult（§4.3）
    async def handle_status(self, status: dict) -> bool
        # 校验（BE-043）：call 存在；回传 toolName/toolVersion/runtime/paramHash（wire）与登记
        #    tool_name/tool_version/runtime/arguments_hash（内部）一致。
        #    **不一致（篡改拒绝，review 修正——不挂起）**：锁内 mark failed（CALL_IDEMPOTENCY_CONFLICT）
        #    + 立即 resolve 明确失败态（error_type=TOOL_ERROR, message="参数与请求不一致，已拒绝"）
        #    + return False——Electron 副作用已发生但结果被拒，Agent 及时收到拒绝而非挂 120s 超时。
        # **终态守卫（review 修正）**：已终态的 call，晚到 status 仅更新 repo result/error 字段
        #    （迁移表：终态不可再迁移），不重复 resolve；`if not future.done(): future.set_result(...)` 防 InvalidStateError。
        # 分支：accepted/running → 仅更新 repo（不 resolve）；
        #       result/error/rejected/cancelled → resolve（offline 半终态可被此覆盖为终态，即「重传结果」闭环）
    async def cancel_call(self, call_id: str) -> None
        # interrupt/回合取消 → 锁内 mark cancelled（守卫）+ `if not future.done(): set_result(CANCELLED)`
    def get_call(self, call_id: str) -> dict | None           # 状态查询（BE-043）
    def pending_calls(self) -> list[dict]                     # 重连重放用（pending/offline 未超时）
    async def save_all(self) -> None                          # no-op 预留（review 修正：repo 即时 upsert、无内存脏集可 flush）
```

**进程内 future 语义（review 修正）**：Mongo 持久化的是**审计/状态**；`request_and_wait` 的唤醒依赖进程内 `_pending[call_id]` future。future **只 resolve 一次**（所有路径经 `if not future.done()` 防护，超时/取消/结果到达竞态不抛 `InvalidStateError`）。

**断连规则（review 修正，平衡「回合冻结」与「不自动取消」）**：
- `on_client_disconnect`：repo 标 offline + 移除 `_connected`。
- call 仍 **pending**（未收到 accepted/running，Electron 未接手）→ **立即 resolve `UNAVAILABLE`**（Agent 回合及时收尾，不冻结 120s；此时无副作用已发生，早 resolve 安全——D-j 已保证断连后 LLM schema 移除 desktop 工具，重试立即 UNAVAILABLE）。
- call 已 **accepted/running**（Electron 已接手，可能已副作用）→ future **挂起到超时**或**晚到结果**（Electron 重连后经新连接重发 `tool.status{result}`，迁移表 `offline → succeeded/failed/…`，future 一并 resolve——BE-043「重传结果」闭环）。
- **重连不自动重投执行请求**（防重复副作用，BE-043）；`pending_calls()` 仅供查询/审计。

### 4.3 状态 → ToolResult 映射

| tool.status | ToolResult | LLM content |
|---|---|---|
| `succeeded`（result） | `ToolResult.ok` | `llm_summary`（execution_summary 去路径）或 `output` |
| `rejected`（用户拒绝） | `ToolResult.fail(error_type=CANCELLED)` | "用户拒绝了该工具调用" |
| `error` | `ToolResult.fail(error_type=TOOL_ERROR)` | error.message |
| `cancelled` | `ToolResult.fail(error_type=CANCELLED)` | "客户端取消了该调用" |
| 篡改拒绝（paramHash 失配） | `ToolResult.fail(error_type=TOOL_ERROR)` | "参数与请求不一致，已拒绝"（**立即 resolve**，review 修正） |
| `accepted`/`running` | （不 resolve，仅更新 repo） | — |
| 超时/expired | `ToolResult.fail(error_type=TIMEOUT)` | "桌面工具调用超时" |
| 断连未接手（pending → offline） | `ToolResult.fail(error_type=UNAVAILABLE)` | "桌面客户端已断开"（**立即 resolve**，review 修正） |
| 无 desktop 客户端 | `ToolResult.fail(error_type=UNAVAILABLE)` | "无桌面客户端连接" |

### 4.4 Desktop 工具插件（`core/tools/desktop/plugin.py`）

四个 `ToolPlugin` 子类，`runtime="desktop"`，risk_level 见 D-g：

| name | description | params | output_schema | risk | safety |
|---|---|---|---|---|---|
| `desktop.file.select` | 打开系统文件选择器，返回用户所选文件路径引用 | `{}`（无参，Electron 弹选择器） | string | safe | `request_timeout=120.0`（用户选文件可能久） |
| `desktop.file.read` | 读取用户所选/指定文件的文本内容 | `{path: string 必填}` | string | medium | `request_timeout=120.0` |
| `desktop.file.write` | 写入文本到用户指定路径 | `{path: string 必填, content: string 必填}` | string | high | `request_timeout=120.0` |
| `desktop.screen.capture` | 截取用户屏幕，返回截图附件 | `{region?: string}` | object（attachments） | high | `request_timeout=120.0` |

**`request_timeout` 必填（review 修正）**：`SafetyPipeline.execute` 对插件执行做外层 `asyncio.wait_for(…, plugin.safety.request_timeout)`（默认 30s）——若 desktop 插件不显式声明 `request_timeout>=120`，桥的 120s 往返会在 30s 被 pipeline 提前取消（返回 CANCELLED 且 repo 停 pending）。对照既有先例 `vision/plugin.py` 已设 `request_timeout=120.0`。四个 desktop 插件 `safety` 必须声明 `request_timeout=120.0`（≥ 桥超时），否则截断。

**执行委托**：`execute()` 里 `bridge = ctx.services.get("desktop_bridge")`；`bridge is None or not bridge.is_available()` → `ToolResult.fail(UNAVAILABLE)`；否则组装 call dict（含 `arguments_hash = sha256(canonical_json(arguments))`）→ `await bridge.request_and_wait(call=call)`。**插件不接触文件系统**——一切本地操作由 Electron 完成（docs/15 §三.2：后端不得直接操作用户电脑）。

**`core/tools/__init__.py`**：`ALL_PLUGINS` 追加 4 个 desktop 插件类。**`desktop.command.run` 不注册**（docs/15 §四.7）。

**select→read 信任边界（review 修正，显式声明）**：`desktop.file.select` 无参返回「用户所选文件路径引用」字符串给 LLM，随后 LLM 用该字符串调 `desktop.file.read`——但 `file.read` 的 `path` 参数接受任意值，spec 无机制证明该 path 来自真实用户选择（LLM 可凭空捏造路径请求读取）。**缓解**：该信任边界由 **Electron 本地路径校验兜底**（Electron 校验 path 在黑名单/沙盒/用户选择记录内，docs/15 §三.2）；后端侧每次 `file.read` 仍走 client 审批（`approvalAuthority=client`，risk=medium）。后端**不额外**实现「select 产出 path 白名单」——`path` 参数跨调用的来源追踪属 Electron 本地安全职责，后端只负责结构化传输 + paramHash 绑定（显式认定，不做跨调用绑定）。

**请求参数 `inputSchema` 校验**：四个插件 `param_model`（Pydantic）校验 `arguments` 结构与类型（path 为必填 string 等）——后端侧契约校验，非本地安全授权。

## 5. WS 协议扩展（BE-041）

### 5.1 Desktop 客户端握手标识（D-l）

- Electron Main 连 `/ws` 时带：`Authorization: Bearer <token>` + `X-Protocol-Version: 2` + **`X-Client-Platform: electron`**。
- `ws_handler.handle_ws` 解析 platform 头 → `WebSocketChannel` 增 `client_platform: str = ""` 属性 → v2 + electron 连接：`await agent.desktop_bridge.on_client_connect(connection_id)`（`connection_id = conn_<uuid4 hex[:12]>`）；断开（`finally`）→ `on_client_disconnect`。
- **`connection_id` 下发（review 修正，附件持有者校验闭环）**：v2 electron 连接的 `server_info` 帧（`build_server_info`）携带 `connectionId`（= 握手生成的 conn_id）——Electron 据此填附件上传 `X-Client-Connection` 头（Task 8 §5.4 持有者校验②依赖；**不下发则 Electron 无法取得 conn_id、合法上传全被 403 拦截，端到端不可用**）。`ServerInfoMessage` 契约增可选 `connectionId` 字段；非 electron 连接恒省略。
- **仅 v2 连接**处理 desktop 语义（v1 无 desktop 概念，忽略 platform 头）。

### 5.2 Server→Client：`tool.request`

```json
{
  "type": "tool.request",
  "callId": "call_...",
  "toolName": "desktop.file.read",
  "toolVersion": "1.0.0",
  "runtime": "desktop",
  "arguments": {"path": "/home/user/doc.txt"},
  "reason": "用户请求分析所选文件",
  "riskLevel": "medium",
  "permissionRequired": true,
  "approvalAuthority": "client",
  "paramHash": "sha256...",
  "expiresAt": "2026-08-05T00:01:00+00:00",
  "requestId": "req_..."
}
```

- **投递**：`bridge._deliver` 回调 → 定位 `client_platform == "electron"` 的 v2 channel → `_send_json(ToolRequestMessage(...).wire_dict())`。**定向单连接**（非广播）；无 desktop 连接时调用已在插件层被拒（§4.4）。
- **仅 v2 收**：v1 channel 不收 desktop 帧（client_platform 仅在 v2 解析）。

### 5.3 Client→Server：`tool.status`（判别消息，D-m）

```json
{
  "type": "tool.status",
  "callId": "call_...",
  "status": "accepted|rejected|running|result|error|cancelled",
  "toolName": "desktop.file.read",
  "toolVersion": "1.0.0",
  "runtime": "desktop",
  "paramHash": "sha256...",
  "output": "…",                              // status=result：公开输出文本
  "error": {"code": "…", "message": "…"},     // status=error：错误
  "attachments": [{"url": "/uploads/…", "mimeType": "image/png", "label": "截图"}],
                                              // status=result：附件引用（后端托管 URL）
  "executionSummary": "已读取文件（4.2 KB）"    // 去本地路径的执行摘要
}
```

- `ws_handler` 收到 → 仅 v2 连接处理 → `agent.desktop_bridge.handle_status(data)`。
- `accepted`/`running`：桥仅更新 repo（Electron 已接受/执行中）；`result`/`error`/`rejected`/`cancelled`：桥 resolve 对应 future（§4.3）。
- 回传的 `toolName`/`toolVersion`/`runtime`/`paramHash` 与登记不一致 → 桥拒绝（BE-043 参数篡改防护，§4.2 handle_status）。

### 5.4 附件上传

- `status=result` 的 `attachments`：Electron 需把附件字节传回后端。**传输方式（唯一路径）**：附件字节经 REST **`POST /api/uploads/desktop/{callId}`** 上传（仿既有 `POST /api/images` handler 结构：multipart 解析 + 落盘 + 返回 url）；后端落盘 `/uploads/desktop/<callId>.<ext>` → 返回 `{url}` → Electron 在 `tool.status` 里填该 url。
- **类型/尺寸约束**：`image_max_mb` / `image_allowed_types` 沿用（图片）；附加 `application/octet-stream` 等通用文件类型按白名单（`desktop.file.*` 读取结果非截图时）。
- **callId 持有者校验（review 修正，防伪附件/孤儿文件）**：上传前校验 ① callId 存在（否则 404 `CALL_NOT_FOUND`）；② 上传连接（握手 `X-Client-Platform: electron` 的 ws 连接）是 `target_connection`（否则 403 `INVALID_REQUEST`）；③ call 处于可收结果状态（pending/accepted/running/offline，终态拒绝——防向已结束调用投伪附件）。校验失败不落盘。
- **落盘为消息图片（review 修正——补上现有循环缺失的路径）**：`core/cognitive/loop.py::_execute_tool_round` 组装 `ToolResultMessage` 时，若 `tool_result.images` 非空 → `content=[TextPart(llm_summary/execution_summary), *[ImagePart(src=item.url, mime=item.mime_type) for item in tool_result.images]]`（`core/schema/part.py` 的 `ImagePart` 已存在；`ToolResultMessage.content` 已是 `list[Part]`）。回放 `SessionMessageItem.content` 已支持 `list[ContentPart]`（含 ImagePart），无需改契约——截图经落盘自动出现在历史回放/搜索/资源索引。**前端渲染 image block 属 Electron 联调范围，不在本批次后端范围**。

## 6. 注册表导出 REST（BE-042）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/tools` | — | `ToolRegistryResponse{ tools: ToolMetadata[], desktopAvailable: bool }` |
| GET | `/api/tools/calls/{callId}` | — | `DesktopCallStatus`（BE-043 状态查询；未知 callId → 404 `CALL_NOT_FOUND`） |

- 注册进 `core/input/api_routes.py`（或独立 `tool_routes.py`），走批次 1 产品认证（`AGENT_ACCESS_TOKEN` 配置时匿名 401）。
- `GET /api/tools`：`ToolManager.registry_metadata(desktop_available=bridge.is_available())`；Electron Main 建连后拉取校验"只接受已注册工具和版本"。
- `GET /api/tools/calls/{callId}`：`bridge.get_call` → 不存在 → 404 `CALL_NOT_FOUND`；存在则返回 `DesktopCallStatus`（**含 `status`，超时/过期态如实返回 `expired`，不抛 408**——408 `CALL_EXPIRED` 是目录成员，语义在桥内以 `error_type=TIMEOUT` 呈现给 Agent）。

## 7. 后端审批 enrich（BE-042）

现有 `tool_approval` 广播参数扩展（**消息类型不变**，仅增字段——前端零破坏）：

```json
{ "type": "command", "command": "tool_approval",
  "params": {
    "tool": "write_file", "request_id": "req_...",
    "params": "path=data/sandbox/... , content=...",      // 兼容显示摘要（str(params)[:200]，非权威，BE-042）
    "summary": {"path": {"type": "string", "preview": "data/sandbox/...", "truncated": true}},  // 结构化参数摘要
    "runtime": "backend", "version": "1.0.0", "riskLevel": "high",
    "permissionRequired": true, "approvalAuthority": "backend"
  }}
```

- **enrich 来源**：`ToolManager.set_hitl_broadcast` 的 `_broadcast` 回调里，从 `_plugins[tool_name]` 取 metadata（runtime/version/risk_level/permission_required/approval_authority）+ 结构化 `summary`（逐参数 type + preview + truncated，**保留** `str(params)[:200]` 兼容字段）。
- **版本门控（review 修正，保「v1 wire 逐字节不变」）**：现有 `OutputManager.command` 无版本感知、广播到全部通道。enrich 字段**仅对 v2 连接生效**：`_broadcast` 回调把基础 `{tool, request_id, params}` 广播给 v1 通道（逐字节不变）、把 enrich 版本广播给 v2 通道（新字段）。落地方式：① `OutputManager.command` 增加 `channels` 过滤（现有 `_split_versions` 按 protocol_version 分组已有先例）；或 ② `ToolManager` 的 `_broadcast` 改为两路广播（v1 原样 / v2 enrich）。spec 采用 **② 两路广播**（不触碰 OutputManager 通用路径，改动局部）：`set_hitl_broadcast` 接收 `(request_id, tool_name, params, protocol_version)` 或回调内部按通道版本各发一次。v1 客户端收到与现状**逐字节一致**的 `tool_approval`。
- **边界**（BE-042）：Backend 工具审批 `approvalAuthority=backend`（客户端弹窗批准后端执行，**不产生 Desktop 本地授权**）；Desktop 工具不走 `tool_approval`，走 `tool.request`（approvalAuthority=client）。
- **后端不发送任意代码字符串**：desktop 工具 `arguments` 经 `input_schema` 校验（param_model Pydantic）；无任何 JS/Python/Shell/Bash 代码执行字段。

## 8. Agent / 生命周期集成

- `agent.desktop_bridge: DesktopToolBridge` 公开属性（只读）。
- 构造顺序：`Agent.__init__` 创建 `DesktopCallRepository` + `DesktopToolBridge`；`ToolAdapter.start(extra_services)` 注入 `services["desktop_bridge"]=bridge`。
- **注入双轨收口（review 修正）**：desktop 注入有两条路径——① **`definitions()` 过滤**（D-j：desktop 工具是否暴露给 LLM）：用 `ToolAdapter` **构造时绑定的 bridge 引用**（`ToolAdapter.__init__` 增可选 `desktop_bridge` 参数，definitions() 内部据此过滤）；② **`execute()` 运行时依赖**（desktop 插件执行）：经 `ctx.services.get("desktop_bridge")`（ToolContext 运行时字典）。**两轨定死，不再「或经 extra_services」双写**——`extra_services` 只用于 execute 注入（现有机制），构造时绑定只用于 definitions 过滤。
- **deliver 回调注册**：`core/agent/_lifecycle_tools.py::_start_tool_context`（实际挂点名，非 `_start_services`）中 `bridge.bind_deliver(回调)`——回调遍历 `agent.output._channels` 找 `client_platform == "electron"` 的 `WebSocketChannel`，存在则 `_send_json(tool.request 帧)`；无目标连接 → 记 warning（插件层已提前拦截，此为兜底）。
- `_lifecycle_persistence.py`：`_start_persistence` 中 `connect_db()` 后 `await self._desktop_bridge.connect_db()`；`_stop_persistence` 中 `save_all()`。
- `_lifecycle_tools.py`：ToolAdapter 构建时传 bridge（构造绑定，供 definitions 过滤）+ `start(extra_services={"desktop_bridge": bridge})`（供 execute 运行时注入）。
- **cancel 贯通**：loop 中断/取消回合时 `bridge.cancel_call` 对在途 desktop 调用 mark cancelled（interrupt 语义）。断线（WS disconnect）标 offline（D-o），**不**与 generation 的 cancelled 混用。
- **`capabilities.py`**：`desktop_runtime=True`（D-s）。

## 9. 契约层（frontend_contract）

**`frontend_contract/ws_message.py`**（v2 类型增量；`ServerMessage` 联合 + Client→Server 契约更新）：
- `ToolRequestMessage`（type=`tool.request`，Server→Client）：callId/toolName/toolVersion/runtime/arguments/reason/riskLevel/permissionRequired/approvalAuthority/paramHash/expiresAt/requestId
- `ToolStatusMessage`（type=`tool.status`，Client→Server）：callId/status/toolName/toolVersion/runtime/paramHash/output/error/attachments/executionSummary
- `ToolAttachment`（{url, mimeType, label}）
- `ToolStatusEnum`：`accepted|rejected|running|result|error|cancelled`
- **Client→Server 消息从 7 条增至 8 条**（现状已 7 条：text/interrupt/switch_model/set_service/tool_approval/wake_toggle/activate_session，加入 `tool.status`）

**`frontend_contract/rest.py`**（命名避开 debug 既有同名类）：
- `ToolMetadata`（name/description/runtime/riskLevel/permissionRequired/approvalAuthority/version/inputSchema/outputSchema/**available**——review 修正：与 §3.2 Python `ToolMetadata` 字段对齐，客户端可感知单工具可用性）
- `ToolRegistryResponse`（tools[] + desktopAvailable）
- `DesktopCallStatus`（callId/toolName/**toolVersion/runtime/paramHash**/status/createdAt/expiresAt/result/error）——**status 用 repo 枚举**，与 wire 枚举的翻译固定（review 修正：显式映射防两套枚举漂移）：

  | wire `tool.status` | repo 枚举 |
  |---|---|
  | `accepted` / `running` | `accepted` / `running`（非终态） |
  | `result` | `succeeded`（终态） |
  | `error` | `failed`（终态） |
  | `rejected` / `cancelled` | `rejected` / `cancelled`（终态） |
  | —（仅服务端） | `pending` / `expired` / `offline` |

  toolVersion/runtime/paramHash 补齐是 BE-043「相同 ID 工具/Runtime/版本/参数摘要变化时拒绝」可经 REST 复核的依据（review 修正）。§4.3 映射表为 ToolResult 翻译权威。

**错误目录**（协议 v2 文档 §9.1 增补；落地状态标注；**需同步更新 `docs/electron-backend-protocol-v2.md §9.1` 与 `electron-backend-progress.md`**）：
- `DESKTOP_UNAVAILABLE`(503, retryable=false)：**目录登记、批次 4 内无错误信封生产者**（同批次 1 `RATE_LIMITED` 先例）——无 desktop 客户端时**执行** desktop 工具以 `ToolResult.fail(error_type=UNAVAILABLE)` 呈现给 Agent（LLM 可见）；错误信封落地随后续 REST 暴露。
- `CALL_NOT_FOUND`(404, false)：**落地**于 `GET /api/tools/calls/{callId}`。
- `CALL_EXPIRED`(408, false)：目录成员；语义在桥内（超时 → `error_type=TIMEOUT` + repo status=expired），查询如实返回 status 不抛错。
- `CALL_IDEMPOTENCY_CONFLICT`(409, false)：目录成员；语义在桥内（`handle_status` 参数篡改拒绝：mark failed + 立即 resolve 失败态 + return False——review 修正，不再「不 resolve 挂超时」）。

## 10. 兼容性硬约束

1. **v1 wire 逐字节不变**：v1 通道不收/不响应 desktop 帧；`tool_approval` **消息类型与字段逐字节不变**（enrich 经 §7 版本门控仅 v2 生效，v1 收原样 params）；现有 v1 事件帧全不动。
2. **v2 wire 增量**：新增 `tool.request`（Server→Client）+ `tool.status`（Client→Server）+ 新 REST `/api/tools`；既有 v2 字段不删除不重命名；`server_info.features.desktop_runtime=true`。
3. **现有 Backend 工具**：12 个工具 `runtime="backend"` 恒真、不伪装 desktop（docs/15 §三.1）；`desktop.command.run` 不注册（§四.7）。
4. **`contract.generated.ts`**：v1 类型字节不变；v2/新类型增量（gen_ts 全量重生成，diff 仅含新增）；`python -m frontend_contract --check` 无漂移。
5. **现有 2176 测试全绿**；`make lint` / `make check-types` 通过。
6. **Mongo 降级**：desktop_calls 离线时写仅落进程内缓存 + log、读从缓存返回（§4.1 进程内缓存）——幂等去重/状态查询不失效，桥进程内 future 仍完成往返（持久化降级不阻塞功能）。

## 11. 测试策略

| 层 | 覆盖 |
|---|---|
| 单元 | `ToolMetadata` 派生（approvalAuthority client/backend、permissionRequired 由 risk_level/requires_human_approval 推导、available）；`paramHash`（规范化 JSON 排序 + SHA-256，同参数同哈希）；`DesktopCallRepository`（Mongo mock upsert/get/query_pending/mark_offline/mark_stale_expired + **离线读从进程内缓存返回、幂等去重不失效** + **状态迁移守卫：终态拒绝覆盖、offline→终态合法**）；`DesktopToolBridge` 状态机（pending→succeeded/failed/rejected/cancelled/expired/offline 全迁移）；**并发幂等去重**（同 (tool,hash) 并发两请求经锁串行化 → 仅一次投递、复用 callId）；**future 终态唯一**（超时/取消/result 竞态不抛 InvalidStateError）；篡改拒绝（tool/version/hash 失配 → mark failed + 立即 resolve 失败态 + return False）；**晚到结果**（offline 后重发 result → repo 转 succeeded + resolve）；超时 → expired；`desktop` 插件参数校验 + 无 bridge/断连 → UNAVAILABLE |
| service | **fake desktop 客户端安全往返**：WS 连接带 `X-Client-Platform: electron` + v2 → **多轮 tool-calling fake LLM**（首轮 `stream_think` 产 `tool_calls=[desktop.file.read]`，收到 tool result 后第二轮产文本回复——review 修正：现有测试 fake 一律 `yield tool_calls: None`，需新写）→ 断言收到 `tool.request`（含 callId/risk/paramHash）→ 回 `tool.status{accepted→running→result}` → Agent 拿到 ToolResult + **落盘消息含 `ImagePart`（截图）**；**负面隔离（review 修正，交付安全边界）**：(a) v1 连接带伪冒 `X-Client-Platform: electron` 头 → **不收** `tool.request`、**不处理** `tool.status`；(b) v2 无 platform 头（web）连接 → **不收** `tool.request`（deliver 定向隔离）；**断连/重连**：pending 断连 → 立即 resolve UNAVAILABLE；accepted 断连 → offline（future 挂起）+ **重连后重发 `tool.status{result}` → future 恢复 + repo succeeded（BE-043「重传结果」闭环）**；重连不自动重投；`GET /api/tools/calls/{callId}` 可查；**幂等**：同参数重试复用 callId；**参数篡改**：回传改 paramHash → 拒绝 + Agent 及时收到失败（非超时）；**`GET /api/tools`**：元数据完整性（含 available）+ 认证 401 + desktopAvailable 随连接状态翻转；**LLM 暴露过滤**：连接时 desktop 工具在 definitions()/plugin_list、断连时移除/不可用；**tool_approval 版本门控**：v2 连接收到 enrich（structured summary/risk/version/approvalAuthority）+ 保留 `str(params)[:200]`；**v1 连接收到原样 tool_approval（逐字节不变）**；**附件**：`POST /api/uploads/desktop/{callId}` 正常路径 url → MediaItem + **未知 callId → 404、非 target 连接 → 403、终态 call → 拒绝** |
| 回归 | v1 wire 不变（现有测试全绿）；`tool_approval` 消息类型不变；`contract.generated.ts` 漂移门禁；desktop_runtime feature=true 断言；协议 v2 文档 §9.1 错误码目录与实现同步 |

**Fake 约定**：fake desktop 客户端 = WS 连接带 platform 头，测试内直接回 `tool.status`（复用 `scripts/capture_ws_trace.py` 的 `_make_agent_isolated` 隔离模式，**不触碰 default 会话**）；**多轮 tool-calling fake LLM**（首轮 tool_call + 结果后文本回复）为本批次**新增测试基建**（现有 `_UsageLLM/_SlowLLM` 不产 tool_calls、`capture_ws_trace.py::FakeLLMToolCall` 是脚本非 pytest 可导入）；**`_inject_fake_storage` 需扩展注入 `DesktopCallRepository`**（现有只换 session/message/token/version/generation repo，不换 desktop repo——断言 `GET /api/tools/calls/{callId}` 需可注入）；Mongo 不可用走进程内缓存降级；新测试 marker 沿用 unit/service。

## 12. 验收（docs/14 门槛 + 场景）

**docs/14 场景 4（BE-040~043，门槛：四个 Desktop P0 工具完成安全往返）**：
1. Electron Main（带 `X-Client-Platform: electron` 头）连接 → `GET /api/tools` 拉到 4 个 desktop 工具元数据（runtime=desktop、risk、approvalAuthority=client、版本、available）；
2. LLM 选 `desktop.file.read` → 后端建 callId + 定向投递 `tool.request` → 断言参数完整（含 paramHash/expiresAt）；
3. Electron 回 `accepted → running → result` → 后端 resolve → Agent 拿到 ToolResult；截图经附件上传 → 落盘消息含 `ImagePart`（聊天可见 URL）；
4. **BE-042**：backend 工具（write_file）审批走 `tool_approval`（v2 收到 enrich 结构化 summary/risk/version/approvalAuthority，v1 收原样）——approvalAuthority=backend；desktop 工具不回退到 `tool_approval`；
5. **BE-043**：同参数**并发**重试经锁串行化、仅一次投递（复用 callId 不二次弹窗）；回传参数篡改被拒且 Agent 及时收到失败；断连 → pending call 立即 UNAVAILABLE、accepted call 标 offline 可查；**Electron 重连后重传 `result` → call 转 succeeded（重传结果闭环）**；`GET /api/tools/calls/{callId}` 返回状态；
6. `desktop.command.run` 不在注册表；服务器沙盒 read/write 仍显示 runtime=backend。

## 13. 明确不做（YAGNI）

- **不做 `desktop.command.run`**：默认不注册（docs/15 §四.7）；
- **不做 Local Worker Runtime**：枚举保留、无实现（批次 5 Profile 引入「执行位置」能力字段时再议——BE-050 主体是模型 Profile 与密钥所有权，本机执行位置只是其能力字段之一，review 修正措辞）；
- **不做多 Desktop 客户端**：单连接目标（Demo 单 Main）；`connection_id` 字段保留，多设备路由后置；
- **不做桥的自动重投**：重连不自动重放执行请求（防重复副作用），只可查/可重传结果（D-o）；
- **不做跨离线窗口的 callId 记忆**：offline/expired 不参与去重、重试即新调用（§4.2 ①）——断连未接手的 pending call 已立即 resolve UNAVAILABLE 使重试在 `is_available=false` 时被拦，已接手的 accepted call 靠「重连重传结果」对账；两者合起来将重复执行概率压到最低，spec 显式认定该取舍（不引入跨窗口去重表，YAGNI）；
- **不做 Desktop 侧实现**：后端只声明/路由/收结果；Electron Main 负责路径解析、黑名单、沙盒、权限、审批、执行（docs/15 §三.2）；
- **不做 outputSchema 强校验**：第一版声明性描述；
- **不做附件鉴权下载/短签名 URL**：`/uploads/*` 认证沿用批次 1（`AGENT_ACCESS_TOKEN`），签名 URL 属 BE-070/071（批次 7）；
- **不做 TTS 媒体附件**：desktop 结果仅 image/文件文本；
- **不做 multi-device 审批路由**：pending approvals 全局（批次 3 既有语义）。
