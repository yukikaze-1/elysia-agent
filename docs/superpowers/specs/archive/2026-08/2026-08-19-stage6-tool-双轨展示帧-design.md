# 阶段 6 工具双轨：`tool.started/tool.completed` 展示帧 + `sourceCallId` 关联（B-6-01~09）

**文档版本：** v0.2
**文档状态：** 已评审（code-review 1 轮，2 Critical + 2 Important + 7 Minor 全消化）
**日期：** 2026-08-19
**仓库：** `/home/yomu/elysia-agent`
**上游决策：** `docs/review/2026-08-19-阶段6-工具双轨统筹决策记录.md`（D6-01~08 定案，方案 B）
**后端基线：** `main@c5928bb8e47b61289251faee58e53a553b4ed702`
**前端对照：** elysia-frontend 阶段 6 前端批次（F-6 镜像决策记录；契约合并前前端 pull 才见新帧计数）
**前置收口：** §7 未拍板项前 3 项按建议默认收口（outputSummary 2000 字符 / inputSummary `_safe_arg_summary` / reason 缺省）；项 4/5 属前端侧，记录为前端批决策。

---

## 1. 背景

阶段 6 前端推进前核对 backend/desktop 工具的「来路」，发现 wire 层**两条语义正确但互不关联**的通道，
以及 ID 双轨断裂：

- **Desktop 工具**走 `tool.request/tool.status` 执行握手（`DesktopToolBridge` 定向投递），`callId`
  为桥生成的 `call_<hex>`；
- **Backend 工具**只有泛型 `action{tool_executing/tool_done}`（`ActionMessage` 广播），`call_id`
  用 LLM `tc.id`——两轨 ID 无关联字段；前端 `RawFrameSchema` 甚至未收录 `action`，真机触发 backend
  工具会 `RAW_FRAME_INVALID` 告警后丢弃；
- 一个用户回合 = 一个 generation（至多 5 轮 ReAct 在同一 generation 内），`generation.completed`
  只发一次且无 `tool_calls` 字段，成功路径 `finishReason` 硬编码 `"stop"`；工具持久数据在归档
  （assistant `tool_calls` + role=tool 结果，`tool_call_id` = LLM `tc.id`）。

**方案 B 定案（D6-01~08）**：wire 保留两条语义正确的通道（Desktop 执行握手 + Backend 展示生命周期），
前端领域层统一为单一 `ToolCallRecord`；权威键 = LLM `tool_call_id`（`sourceCallId`）；Desktop 桥
`call_*` 仅作执行协议关联。**本批 = 后端 B-6-01~09 全部落地**；前端 F-6 在契约合并后由前端仓库并行。

## 2. 目标

- **契约冻结（B-6-01）**：新增 `ToolRuntime` StrEnum（backend/desktop/local_worker）；`ToolRequestMessage`
  补必填 `sourceCallId`（LLM tool_call_id）+ `runtime` 收敛为 `ToolRuntime`；新增 Server→Client
  `tool.started` / `tool.completed` 两个 v2 帧入 `ServerMessage` 联合；`make gen-types` 再生成三产物。
- **输出层分流（B-6-02）**：`OutputManager.tool_started/tool_completed` 按 `_split_versions` 分流——
  **v1 发 `action{tool_executing/tool_done}`（wire 逐字节不变），v2 发新帧**；`WebSocketChannel` 实现
  `on_tool_started/on_tool_completed`。
- **发射点统一（B-6-03/04）**：`core/cognitive/tool_phase.py` `_dispatch_tool_calls` 与 `_run_submit_tasks`
  改用 `tool.started/tool.completed`（v1 由输出层分流保持 action）；`runtime` 由 ToolAdapter 反查
  （`desktop.* → desktop`，`submit_task`/未知 → `backend`）。
- **ID 关联贯通（B-6-05/06）**：`desktop/plugin.py` `_delegate` 组装 call dict 时透传
  `source_call_id=ctx.call_id`、`request_id=ctx.trace_id`；bridge `_build_request_frame` 输出
  `sourceCallId`/`requestId`；repo 持久化两字段。
- **文档（B-6-08）**：protocol-v2.md 补 `tool.request.sourceCallId`、`tool.started/completed` 帧说明、
  v2 `action` 工具帧退役注记；错误码目录**保持 41 码**（无新增）。
- **golden**：`ws-v2-tool.jsonl` 因 `ToolRequestMessage.sourceCallId` 必填而**预期更新**（新增该字段）——
  本批 golden 不是零 churn，是**契约驱动的既有帧字段变更**（见 D-GL-01）。
- 不新增 wire 消息类型之外的枚举/错误码；`finishReason`/`generation.completed` 不扩展（D6-04/05）。

## 3. 现状（事实基线，已对 `c5928bb8` 核实）

### 3.1 契约层 `frontend_contract/ws_message.py`

| 项 | 行 | 现状 |
|---|---|---|
| `ActionMessage` | `:516` | `action{action, params?}`——v1 保留 |
| `ToolRequestMessage` | `:922-944` | `runtime: str`；无 `sourceCallId`；`requestId?` 可缺省 |
| `ToolStatusMessage` | `:947-966` | Client→Server 六态；`runtime: str` |
| `ServerMessage` 联合 | `:992-1033` | 38 具名成员；无 tool.started/tool.completed |

### 3.2 输出层 `Module/output/`

| 文件 | 现状 |
|---|---|
| `protocol.py:19-23` | `OutputChannel` ABC——「所有方法默认 no-op，capabilities + on_message + close 为 abstract」→ 加 `on_tool_started/on_tool_completed` 默认 no-op 不破坏既有通道 |
| `manager.py:194-213` | `action()` 广播 `on_action` 到**全部**通道（无版本分流） |
| `manager.py:336-345` | `_split_versions()` 按 `protocol_version` 拆 v1/v2 |
| `manager.py:494-520` | `turn_failed` v1/v2 分流模式（v1 逐字节复刻 + v2 发 generation.failed）——**B-6-02 复用此模式** |
| `websocket.py:127-135` | `on_action` → `ActionMessage(action, params).wire_dict()` |
| `websocket.py:49` | `WebSocketChannel(OutputChannel)` |

### 3.3 发射点 `core/cognitive/tool_phase.py`

| 行 | 现状 | 本批改法 |
|---|---|---|
| `:159-163` | `action("tool_executing", {tool, call_id, args: {k: str(v)}})`（执行前，对全部 dispatch_tcs） | → `tool_started(...)`（v1 由输出层保持 action） |
| `:184-193` | `action("tool_done", {tool, call_id, success, content, error, error_type, duration_ms, truncated})`（每结果） | → `tool_completed(...)` |
| `:114-123` | `_run_submit_tasks` 只有 `action("tool_done")`（无 executing） | → 补 `tool.started` + `tool.completed`（runtime=backend） |

全仓仅此 3 处发射 `tool_executing/tool_done`（已核实无其它生产发射点）。

### 3.4 ID 双轨与 desktop 侧

| 文件 | 现状 |
|---|---|
| `core/tools/desktop/plugin.py:91` | `_DesktopPluginBase.runtime = "desktop"` |
| `core/tools/desktop/plugin.py:117-126` | `_delegate` call dict 含 tool_name/version/runtime/arguments/arguments_hash/risk_level/permission_required/approval_authority——**无 source_call_id/request_id** |
| `core/desktop/bridge.py:219-231` | `request_and_wait`：`doc = {**call, "call_id": "call_"+uuid, ...}`——call dict 新增字段自动进 doc |
| `core/desktop/bridge.py:384-400` | `_build_request_frame`：`ToolRequestMessage(callId=doc["call_id"], ..., requestId=doc.get("request_id"))`——**无 sourceCallId** |
| `core/storage/desktop_call_repo.py` | call doc schema（spec §4.1）——需注记 source_call_id/request_id |
| `Module/tool/protocol.py:67-83` | `ToolContext` 有 `call_id` / `trace_id` 字段——`_delegate` 数据源 |
| `core/adapter/tool_adapter.py:204-216` | `resolve_llm_name`（LLM 脱点名→运行时名）；无 `runtime_for`——B-6-03 需新增 |

### 3.5 golden 与测试影响

| 项 | 现状 | 影响 |
|---|---|---|
| `scripts/gen_contract_frames.py:230-236` | `ToolRequestMessage(...)` **无 sourceCallId** → 生成 `ws-v2-tool.jsonl` | sourceCallId 必填后构造会校验失败 → **必须补** `sourceCallId="tc_frame_1"`；`ws-v2-tool.jsonl` 增该字段（**预期 churn，D-GL-01**） |
| `frontend_contract/tests/test_desktop_tools.py` | 构造 ToolRequestMessage | 需补 sourceCallId；新增 ToolRuntime/ToolStartedMessage/ToolCompletedMessage 用例 |
| `core/desktop/tests/test_bridge.py` | bridge 测试断言 tool.request wire | 需补 sourceCallId/requestId 断言 |
| `core/agent/tests/test_desktop_bridge_integration.py` / `core/input/tests/test_desktop_roundtrip.py` | 桌面工具往返 | 需补 sourceCallId 贯通断言 |
| `Module/output/tests/test_output.py` | on_action/action 测试 | 新增 tool_started/tool_completed 分流测试 |
| `core/cognitive/tests/` | 无 tool_phase 帧测试 | 新增两帧序列测试 |

## 4. 方案（D6-01~08 + §7 收口 + B-6-01~09）

### 4.1 契约冻结（B-6-01，`frontend_contract/ws_message.py`）

```python
class ToolRuntime(StrEnum):
    """工具 Runtime（wire 值 backend/desktop/local_worker；D6 权威键关联）。"""
    BACKEND = "backend"
    DESKTOP = "desktop"
    LOCAL_WORKER = "local_worker"
```

`ToolRequestMessage` 变更：`runtime: str` → `runtime: ToolRuntime`（wire 值不变）；新增
`sourceCallId: str = Field(description="LLM tool_call_id（前端统一领域键，D6-03）")`（**必填**）。

```python
class ToolStartedMessage(ContractModel):
    """Backend 工具开始（v2，Server→Client 展示生命周期，D6-02/08）。"""

    type: Literal["tool.started"] = "tool.started"
    callId: str = Field(description="LLM tool_call_id（归档同值，前端权威键）")
    toolName: str = Field(description="工具名")
    runtime: ToolRuntime = Field(description="工具 Runtime")
    inputSummary: Optional[str] = Field(default=None, description="服务端脱敏参数摘要（_safe_arg_summary），可缺省")
    generationId: Optional[str] = Field(default=None, description="生成实例 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")


class ToolCompletedMessage(ContractModel):
    """Backend 工具完成（v2，Server→Client 展示生命周期，D6-02/08）。"""

    type: Literal["tool.completed"] = "tool.completed"
    callId: str = Field(description="LLM tool_call_id（与 started 同值）")
    toolName: str = Field(description="工具名")
    runtime: ToolRuntime = Field(description="工具 Runtime")
    success: bool = Field(description="是否成功")
    outputSummary: Optional[str] = Field(default=None, description="截断后的公开结果摘要（≤2000 字符），可缺省")
    error: Optional[dict[str, Any]] = Field(default=None, description="错误（{code,message}，code=errorType 或 TOOL_ERROR），可缺省")
    errorType: Optional[str] = Field(default=None, description="ErrorType 值，可缺省")
    durationMs: Optional[float] = Field(default=None, description="耗时毫秒，可缺省")
    truncated: Optional[bool] = Field(default=None, description="outputSummary 超长截断标记，可缺省")
    generationId: Optional[str] = Field(default=None, description="生成实例 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
```

- 两新模型加入 `ServerMessage` 判别联合（现 `:992-1033`，38 成员 → 40 成员）。
- `scripts/gen_contract_frames.py:230` `ToolRequestMessage(...)` 补 `sourceCallId="tc_frame_1"`；
  `make gen-types` 再生成三产物 + `make gen-frames` 再生成 `ws-v2-tool.jsonl`（**预期 churn**）。

> **error 类型边界（D-ERROR，Critical 1 修正）**：`ToolResult.error` 为 `str | None`
> （`Module/tool/protocol.py:564`）。v1 `action.tool_done.error` 保持 **str**（wire 不变）；
> v2 `tool.completed.error` 按记录 §3.3 为 **dict `{code, message}`**——由 tool_phase 构造
> `{"code": error_type_str or "TOOL_ERROR", "message": tool_result.error or ""}`（有 error 时），
> **不得把 str 直接塞进 dict 字段**（否则 pydantic 校验失败、`_broadcast` 吞异常静默丢帧）。
> 失败工具必须仍产出良构 v2 `tool.completed`（Task 3 验收）。

> **语义边界（D6-08）**：`tool.started/completed` 是 Backend Runtime 的**展示生命周期**——后端只
> 发、绝不回环请求客户端；`tool.request/tool.status` 仍只属于 Desktop Runtime。Desktop 工具经
> `_delegate → tool.request/tool.status` 执行的同时，也发 `tool.started/completed`（runtime=desktop）
> 供前端统一 `ToolCallRecord` 展示。

### 4.2 输出层分流（B-6-02，`Module/output/`）

`OutputChannel` ABC（protocol.py）新增默认 no-op：

```python
async def on_tool_started(self, *, call_id: str, tool_name: str, runtime: str,
                          input_summary: str | None = None, generation_id: str | None = None,
                          conversation_id: str | None = None) -> None: ...
async def on_tool_completed(self, *, call_id: str, tool_name: str, runtime: str,
                            success: bool, output_summary: str | None = None,
                            error: dict | None = None, error_type: str | None = None,
                            duration_ms: float | None = None, truncated: bool | None = None,
                            generation_id: str | None = None,
                            conversation_id: str | None = None) -> None: ...
```

`OutputManager` 新增（复用 `turn_failed` 的 `_split_versions` 模式）：

```python
async def tool_started(self, *, call_id: str, tool_name: str, runtime: str,
                       v1_args: dict, input_summary: str | None,
                       generation_id: str | None = None, conversation_id: str | None = None,
                       v1_suppress: bool = False,
                       channels: list[OutputChannel] | None = None) -> None:
    """工具开始：v1 发 action{tool_executing}（wire 逐字节不变）；v2 发 tool.started（D6-02）。

    v1_suppress=True：跳过 v1 action{tool_executing}——submit_task 场景 v1 从未发过
    tool_executing，加帧会破坏「v1 帧集不变」（Critical 2 修正）。
    """
    v1, v2 = self._split_versions(channels)
    if v1 and not v1_suppress:
        await self._broadcast("on_action", "tool_executing",
                              params={"tool": tool_name, "call_id": call_id, "args": v1_args},
                              channels=v1, capability=ChannelCapability.ACTION)
    if v2:
        await self._broadcast("on_tool_started",
                              call_id=call_id, tool_name=tool_name, runtime=runtime,
                              input_summary=input_summary,
                              generation_id=generation_id, conversation_id=conversation_id,
                              channels=v2)

async def tool_completed(self, *, call_id: str, tool_name: str, runtime: str,
                         success: bool, v1_content: str, output_summary: str | None,
                         v1_truncated: bool, output_truncated: bool,
                         error_v1: str | None, error_v2: dict | None,
                         error_type: str | None, duration_ms: float | None,
                         generation_id: str | None = None, conversation_id: str | None = None,
                         channels: list[OutputChannel] | None = None) -> None:
    """工具完成：v1 发 action{tool_done}（wire 逐字节不变）；v2 发 tool.completed（D6-02）。

    error_v1（v1 tool_done.error，str，wire 不变）与 error_v2（v2 tool.completed.error，
    dict {code,message}，D-ERROR）语义独立，不得互相塞错类型。
    """
    v1, v2 = self._split_versions(channels)
    if v1:
        await self._broadcast("on_action", "tool_done",
                              params={"tool": tool_name, "call_id": call_id, "success": success,
                                      "content": v1_content, "error": error_v1,
                                      "error_type": error_type, "duration_ms": duration_ms,
                                      "truncated": v1_truncated},
                              channels=v1, capability=ChannelCapability.ACTION)
    if v2:
        await self._broadcast("on_tool_completed",
                              call_id=call_id, tool_name=tool_name, runtime=runtime,
                              success=success, output_summary=output_summary,
                              truncated=output_truncated, error=error_v2, error_type=error_type,
                              duration_ms=duration_ms, generation_id=generation_id,
                              conversation_id=conversation_id,
                              channels=v2)
```

> **v1 逐字节/帧集不变保证**：v1 分支的 `params` dict 键序/内容与现状 `tool_phase.py:159-193` 完全一致
> （`args` 为 `{k: str(v)}`；`content` 为**完整** `tool_result.content or ""`；`truncated` 为
> `tool_result.truncated`；`error` 为 str）。v2 的 `outputSummary` 是截断摘要。`v1_suppress` 保证
> submit_task 的 v1 **帧集**（仅 `tool_done`）不变（Critical 2 修正）。

`WebSocketChannel` 实现 `on_tool_started/on_tool_completed`：构造 `ToolStartedMessage/ToolCompletedMessage`
→ `_send_json(wire_dict())`。`ConsoleChannel` 不实现（默认 no-op）。

### 4.3 发射点改造（B-6-03/04，`core/cognitive/tool_phase.py`）

**runtime 反查（轻量，Important 3 修正）**：`ToolManager` 新增 `runtime_of(tool_name) -> str | None`
直读插件注册表（仿 `set_hitl_broadcast` 的 `self._plugins[tool_name].runtime` 先例，**不走**
`registry_metadata()`——后者每次重建全插件 schema，热路径昂贵）；`ToolAdapter.runtime_for`
委托 + 兜底：

```python
# Module/tool/manager.py
def runtime_of(self, tool_name: str) -> str | None:
    """按运行时工具名直读插件 runtime；未注册返回 None。"""
    plugin = self._plugins.get(tool_name)
    return plugin.runtime if plugin is not None else None

# core/adapter/tool_adapter.py
def runtime_for(self, tool_name: str) -> str:
    """按运行时工具名反查 runtime（backend/desktop/local_worker）；未知 → backend（D6-03）。

    Args:
        tool_name: 运行时工具名（resolve_llm_name 结果）。

    Returns:
        ToolRuntime 值（"backend"/"desktop"/"local_worker"）；submit_task/未知 → "backend"。
    """
    return self._manager.runtime_of(tool_name) or "backend"
```

**签名变更（Minor 9 修正）**：`_dispatch_tool_calls` / `_run_submit_tasks` 均新增
`conversation_id: SessionId | None = None` 参数，由 `_execute_tool_round`（已有该参数，
`loop.py:683-685` 透传）显式传入。

`_dispatch_tool_calls` 改造（`:158-193`）：

- 执行前循环：`action("tool_executing")` → `self._output.tool_started(...)`：
  - `v1_args = {k: str(v) for k, v in args.items()}`（保持 v1 wire）；
  - `input_summary = _safe_arg_summary(args)`（`Module/tool/safety.py:123`）；
  - `runtime = self._tool_adapter.runtime_for(func_name)`；
  - `generation_id = self._generation_ctx.generation_id if self._generation_ctx else None`；
  - `v1_suppress` 不设（默认 False——普通工具 v1 原有 tool_executing）。
- 每结果：`action("tool_done")` → `self._output.tool_completed(...)`：
  - `v1_content = tool_result.content or ""`；`output_summary, output_truncated =
    _truncate_output_summary(tool_result.content)`（≤2000 字符，§7-1）；
  - `v1_truncated = tool_result.truncated`；`error_type_str = tool_result.error_type.value if
    tool_result.error_type else None`；
  - `error_v1 = tool_result.error`（str，v1 wire 不变）；
  - `error_v2 = {"code": error_type_str or "TOOL_ERROR", "message": tool_result.error or ""}
    if tool_result.error else None`（dict，D-ERROR）——**无 `ErrorType` 时 `error.code="TOOL_ERROR"`
    且 `errorType` 字段缺省是预期行为**（Minor 3 修正：`error.code` 为前端权威错误键，两者可并存）；
  - 其余字段沿用现状。

`_run_submit_tasks` 改造（`:106-134`）：`_handle_submit_task` 前补 `tool_started`（runtime=backend，
`v1_suppress=True`——**submit_task v1 从未发 tool_executing，加帧破坏 v1 帧集不变，Critical 2 修正**；
`v1_args` 传 `{}` 即可（v1_suppress 下 v1 分支不消费该值，Minor 2 修正）；`input_summary=
_safe_arg_summary(parsed)`，parsed 推导加 `isinstance(dict)` 守卫、非 dict 置 `{}`，Minor 6 修正）
+ 结果后补 `tool_completed`（runtime=backend，error_v1/error_v2 同 `_dispatch_tool_calls` 规则）；
`action("tool_done")` 移除（由 tool_completed v1 分支接管，wire 不变）。

新增工具函数 `_truncate_output_summary(content: str | None, max_len: int = 2000) -> tuple[str | None, bool]`：

```python
def _truncate_output_summary(content: str | None, max_len: int = 2000) -> tuple[str | None, bool]:
    """outputSummary 截断（§7-1）：≤ max_len 原样；超长截断 + truncated=True。

    Args:
        content: 工具结果原始内容（可 None）。
        max_len: 截断上限（字符，默认 2000）。

    Returns:
        (output_summary, truncated)——output_summary 为 None 时 truncated 恒 False。
        截断后总长 ≤ max_len（`content[:max_len-1] + "…"`，含省略号，Minor 7 修正）。
    """
    if content is None:
        return None, False
    if len(content) <= max_len:
        return content, False
    return content[: max_len - 1] + "…", True
```

### 4.4 截断语义边界（§7-1 收口）

| 帧字段 | 语义 | 值 |
|---|---|---|
| v1 `action.tool_done.content` | **完整**工具结果（wire 不变） | `tool_result.content or ""` |
| v1 `action.tool_done.truncated` | 工具自身截断标记（wire 不变） | `tool_result.truncated` |
| v2 `tool.completed.outputSummary` | **截断摘要**，绝不含完整文件/网页正文 | `_truncate_output_summary(content)` 的摘要 |
| v2 `tool.completed.truncated` | outputSummary 是否超 2000 字符截断 | `_truncate_output_summary` 的 truncated |

> **工具级截断信号（Minor 10 修正）**：v2 `tool.completed.truncated` 只表达 outputSummary 超长截断；
> 工具的**自身**截断标记（如文件读取被截断）不在 v2 帧上——前端若需该信号，从归档回放
> （`role=tool` 消息的 `truncated`）取，不由实时帧推断。
>
> **文件型工具 outputSummary（Minor 11 修正）**：对大内容/文件型工具（含 backend filesystem 工具），
> `outputSummary` 取内容前缀、按 2000 字符截断——即截断前缀即为可接受语义（D-§7-1「不得塞完整内容」
> 已满足，未超上限即非完整内容）。D-§7-4 的「大结果回传路径」defer 只覆盖 desktop 工具
> `tool.request/tool.status` 通道（前端 Main 执行器设计），**不影响** `tool.completed.outputSummary`
> 的前缀语义。

### 4.5 ID 关联贯通（B-6-05/06，desktop 侧）

`core/tools/desktop/plugin.py` `_delegate`（`:117-126`）call dict 新增：

```python
call = {
    "tool_name": self.name,
    "tool_version": self.version,
    "runtime": self.runtime,
    "arguments": arguments,
    "arguments_hash": arguments_hash(arguments),
    "risk_level": self.risk_level,
    "permission_required": self.permission_required,
    "approval_authority": self.approval_authority,
    "source_call_id": ctx.call_id,        # B-6-05：LLM tool_call_id 透传（D6-07）
    "request_id": ctx.trace_id,           # B-6-05：请求追踪 ID 透传（D6-07）
}
```

`core/desktop/bridge.py` `_build_request_frame`（`:386-399`）补（`doc.get` 防 KeyError，Important 4 修正）：

```python
ToolRequestMessage(
    ...,
    sourceCallId=doc.get("source_call_id"),  # 必填（D6-07）；升级前 pending doc 缺键时 .get 兜底
    requestId=doc.get("request_id"),
)
```

`request_and_wait` 的 `doc = {**call, ...}`（`:221`）已自动携带新字段 → repo 持久化无需改代码，
`core/storage/desktop_call_repo.py` 文档 schema 注记补 `source_call_id/request_id` 两字段说明。

`core/desktop/bridge.py` 文档字符串（B-6-07）：敏感纪律 D-p 不变——日志只记 callId/状态/字段名，
**不打印 source_call_id 之外任何参数值**；加一句「`source_call_id` 为 LLM tool_call_id，非敏感」。

### 4.6 文档（B-6-08，`docs/electron-backend-protocol-v2.md`）

- §10（Desktop 工具契约）`tool.request` 帧补 `sourceCallId`（必填，LLM tool_call_id，前端统一领域键）；
  删除「阶段 6 待实现」占位注记（§10 前的 `>` 块，`c5928bb8` 引入）——改由正式契约说明替代；
- 新增 `tool.started` / `tool.completed` 帧说明（Server→Client，v2，字段表 + D6-08 语义：Backend
  Runtime 只展示、不回环）；
- v2 `action{tool_executing/tool_done}` 工具帧退役注记（v2 不再发，v1 保留）；
- 错误码目录：**无新增，保持 41 码**（`check_error_codes.py` 门禁）。

### 4.7 决策表

| 决策 | 定案 | 理由 |
|---|---|---|
| D0 批范围 | **A 后端 B-6-01~09 全落地** | 契约→输出→发射→ID→文档→门禁完整链路，单批交付；前端 F-6 契约合并后并行 |
| D-GL-01 golden | **A 预期更新 `ws-v2-tool.jsonl`** | `ToolRequestMessage.sourceCallId` 必填，`gen_contract_frames.py:230` 构造须补该字段 → golden 增 `sourceCallId`。**非零 churn，是契约驱动字段变更**（overturn 决策记录 §3.5「零 churn」措辞，见 §9）；`--check` 仍验证提交的 golden 与再生成一致 |
| D-§7-1 outputSummary | **2000 字符 + truncated=true** | 决策记录建议默认；绝不塞完整文件/网页正文 |
| D-§7-2 inputSummary | **`_safe_arg_summary`（key+类型/长度）** | 决策记录建议默认；复用既有脱敏工具，不传原始 arguments |
| D-§7-3 reason | **P0 缺省** | LLM tool_calls 无 reason 来源；权限窗用工具说明+输入摘要+数据流向替代 |
| D-§7-4 desktop.file.read 大结果 | **前端批决策** | 记录为前端 F-6-4 待定（tool.status.output 截断 vs 附件上传），不阻塞后端批；不影响 `tool.completed.outputSummary` 前缀语义 |
| D-§7-5 action 兼容帧 | **前端 raw 层决策** | 记录为前端批待定；后端 v2 停发后与后端无关 |
| D-runtime | **`ToolManager.runtime_of()` + `ToolAdapter.runtime_for()` 轻量反查** | 记录「runtime 由 definitions 反查」——标准 definitions() 无 runtime 字段；不用 `registry_metadata()`（热路径昂贵），直读 `_plugins[name].runtime`（Important 3 修正） |
| D-status-runtime | **`ToolStatusMessage.runtime` 保持 str** | 决策记录只收敛 `ToolRequestMessage.runtime`；status 为 **v2/Electron Client→Server 回传**（非 v1），收敛会扩大契约 churn，记为已知不对称 |
| D-截断 | **`_truncate_output_summary` 独立 helper** | v1 `content`/`truncated` 与 v2 `outputSummary`/`truncated` 语义独立，v1 必须逐字节不变 |
| D-ERROR | **v2 `tool.completed.error` = dict，v1 `action.tool_done.error` = str** | `ToolResult.error` 是 str；v2 按记录 §3.3 为 `{code,message}`，由 tool_phase 构造，不得 str→dict 直塞（Critical 1 修正） |
| D-v1-suppress | **`tool_started` 加 `v1_suppress` 参数；submit_task 用 True** | submit_task v1 从未发 tool_executing，加帧破坏「v1 帧集不变」；普通工具默认 False（v1 保持 tool_executing）（Critical 2 修正） |
| D-新 golden 帧 | **不新增 tool.started/completed golden 帧** | 现有 ws-v2-tool.jsonl 只补 sourceCallId；新帧由 frontend_contract/output/cognitive 三层单测覆盖，避免 golden trace 过度重构 |

**纪律**：`Makefile`、`providers.yaml`、历史文档零改动；`frontend_contract/` 只增模型/字段（不删改既有
语义）；错误码目录零新增；v1 wire 逐字节不变。

## 5. 测试计划

### 5.1 Task 1：契约（B-6-01）

- `frontend_contract/tests/test_desktop_tools.py`：既有 ToolRequestMessage 构造补 sourceCallId；
  新增——`ToolRuntime` 值域（backend/desktop/local_worker）、`ToolStartedMessage`/`ToolCompletedMessage`
  wire 形状（camelCase 键、必填/可缺省、可选字段缺省省略）、两模型入 `ServerMessage` 判别联合、
  `ToolRequestMessage.sourceCallId` 缺省 → `CONTRACT_VALIDATION`。
- `make gen-types` 三产物 + `make gen-frames` 更新 ws-v2-tool.jsonl + `make check-types` 三子项绿。

### 5.2 Task 2：输出层分流（B-6-02）

- `Module/output/tests/test_output.py` 新增：
  - v2 通道 `tool_started/tool_completed` → 收到 `tool.started/tool.completed` 帧；
  - v1 通道 → 仍收 `action{tool_executing/tool_done}` 且 params 与现状逐字节一致；
  - **v1 帧集不变**：submit_task 的 `tool_started(v1_suppress=True)` 在 v1 通道不产生
    `action{tool_executing}`（Critical 2 验收）；
  - 双通道并存互不串发；capability 过滤（ACTION）；
  - 失败路径：通道方法 raise 被 `_broadcast` 记录（`return_exceptions=True` + warning）且**不阻塞**
    其它通道（仿既有 `test_failing_channel_logs_error` 模式——注意 `_broadcast` 吞异常不传播，
    测试应断言 warning/其它通道照常，而非断言异常上抛）。
- `websocket.py` 单测：新帧 wire 形状。

### 5.3 Task 3：发射点改造（B-6-03）

- `core/cognitive/tests/` 新增（mock output）：
  - backend 工具：`tool.started`（callId=tc.id、runtime=backend、inputSummary=_safe_arg_summary 形态、
    generationId/conversationId 贯通）→ 执行 → `tool.completed`（success/outputSummary 截断/truncated）；
  - desktop 工具（mock _delegate）：`tool.started` runtime=desktop → `tool.completed`；
  - **失败工具**：仍产出良构 v2 `tool.completed`（error=dict{code,message}，Critical 1 验收）；
  - gate 拒绝路径：gate 拦截不产生两帧；
  - 取消/中断路径：无 tool.completed。
- `ToolManager.runtime_of` / `ToolAdapter.runtime_for` 单测：desktop→desktop、submit_task/未知→backend。

### 5.4 Task 4：submit_task（B-6-04）

- `_run_submit_tasks` 新增：`tool.started`（runtime=backend）+ `tool.completed` 两帧序列；归档
  （assistant tool_calls + tool 结果消息）不变。

### 5.5 Task 5：desktop ID 贯通（B-6-05/06/07）

- `core/tools/desktop/` 插件测试：call dict 恒含 `source_call_id`/`request_id`（= ctx.call_id/trace_id）；
  UNAVAILABLE 路径（bridge 缺失）不建 call 不受影响。
- `core/desktop/tests/test_bridge.py`：`_build_request_frame` wire 含 `sourceCallId`/`requestId` 与
  repo doc 一致；repo 往返（upsert/get）不丢两字段。
- `core/agent/tests/test_desktop_bridge_integration.py` / `core/input/tests/test_desktop_roundtrip.py`：
  桌面工具往返贯通 sourceCallId。
- bridge 日志 grep：不打印 source_call_id 之外的参数值。

### 5.6 Task 6：文档（B-6-08）

- protocol-v2.md §10 补 sourceCallId + 新帧 + v2 action 退役注记；删除「阶段 6 待实现」占位块。
- grep 验证：`阶段 6 待实现` 在活跃文档零命中；`sourceCallId`/`tool.started`/`tool.completed` 已落。

## 6. 门禁

- `make test-quick` / `make test-normal` / `make lint` / `make pyright` / `make check-types`
  （frontend_contract `--check` + golden `--check` + `check_error_codes.py` 41 码）。
- golden：`ws-v2-tool.jsonl` **预期更新**（增 sourceCallId），`--check` 验证再生成一致性（非零 churn）。
- 已通过链路回归：Desktop 工具请求/状态回传、backend 工具执行、submit_task、阶段 4/5 真实联调链路。

## 7. 验收（完成定义）

- 契约：`ToolRuntime` + `ToolStartedMessage` + `ToolCompletedMessage` 入 ServerMessage 联合；
  `ToolRequestMessage.sourceCallId` 必填 + `runtime: ToolRuntime`；三产物再生成；`make check-types` 绿
  （含 `ws-v2-tool.jsonl` 预期更新）。
- 输出层：v1 收 `action{tool_executing/tool_done}` 逐字节不变，v2 收 `tool.started/tool.completed`，
  互不串发（输出层测试断言）。
- 发射点：`_dispatch_tool_calls` / `_run_submit_tasks` 两帧序列（backend + desktop runtime 正确）；
  `runtime_for` 反查正确；gate 拒绝/取消路径无两帧。
- ID：`tool.request.sourceCallId` = LLM tool_call_id 贯通（plugin → call dict → repo → wire）；
  `requestId` = ctx.trace_id 贯通。
- 文档：protocol-v2.md 正式契约 + 退役注记 + 41 码不变。
- **标记**：本批可标「阶段 6 已开发/自动化已验证」；「阶段 6 真实联调通过」须待前端 F-6 批次 +
  Windows Electron 桌面四工具真机验证（决策记录 §6 依赖与顺序）。

## 8. 风险

| 风险 | 缓解 |
|---|---|
| v1 wire 变化（action 帧 params） | `tool_started/tool_completed` v1 分支 params 键序/内容与现状逐字节一致；Task 2 输出层测试逐字节断言 |
| golden churn 误触门禁 | D-GL-01 明确 `ws-v2-tool.jsonl` 预期更新；`--check` 验证一致性而非零 churn |
| `ToolRequestMessage.sourceCallId` 必填破坏既有发送/测试 | 全仓 4 个直接构造点逐一补齐（`bridge.py:386` + `gen_contract_frames.py:230` + `test_desktop_tools.py:49/71`）；`test_bridge.py` 走 `_build_request_frame`、集成/往返测试走插件（Minor 5 修正）；Task 1 覆盖 |
| outputSummary 与 v1 content 语义混淆 | §4.4 边界表钉死；`_truncate_output_summary` 独立 helper |
| `runtime` 反查未知工具 | `runtime_for` 兜底 backend（记录明确）；submit_task 显式 backend |
| 前端尚未 pull 契约（旧前端收新帧） | 前端 raw 层 `action` 已知忽略过渡（决策记录 §6）；`tool.started/completed` 为 v2 新帧，旧前端未收录时按未知 type 处理（前端整改项） |
| ToolStatusMessage.runtime 未收敛 | 记为已知不对称（D-status-runtime），wire 值恒 desktop 不破坏 |

## 9. 决策点记录

D0 后端单批全落地；D-GL-01 golden 预期更新（**overturn 决策记录 §3.5「零 churn」措辞**——`sourceCallId`
必填使 ws-v2-tool.jsonl 契约驱动增字段，非漂移）；D-§7-1/2/3 按建议默认收口（2000 字符 /
`_safe_arg_summary` / reason 缺省）；D-§7-4/5 记录为前端批决策；D-runtime 轻量 `runtime_of`/`runtime_for`；
D-status-runtime `ToolStatusMessage.runtime` 保持 str（已知不对称，v2 回传侧）；D-截断 v1/v2 语义独立；
**D-ERROR** v2 error=dict / v1 error=str（Critical 1 修正）；**D-v1-suppress** submit_task tool_started
v1 帧集不变（Critical 2 修正）；D-新 golden 帧不新增。中途决策均按推荐定案，记录于此。

## 10. Review 记录

code-review 1 轮（general-purpose agent）结论 **With fixes**（2 Critical + 2 Important + 7 Minor +
3 Recommendation，全消化）：

- **CRIT-1** `ToolResult.error` 是 str、`ToolCompletedMessage.error` 记录为 dict——str 直塞 dict 会
  pydantic 校验失败、`_broadcast` 吞异常静默丢 v2 帧 → 新增 **D-ERROR**（v2 error 由 tool_phase 构造
  dict `{code,message}`，v1 error 保持 str）+ Task 3 失败工具 v2 良构验收。
- **CRIT-2** `_run_submit_tasks` 加 tool_started 后 v1 会多一条 `action{tool_executing}`（submit_task
  v1 从未发过）→ 新增 **D-v1-suppress**（`tool_started` 加 `v1_suppress` 参数，submit_task 用 True）
  + Task 2 v1 帧集不变验收。
- **IMP-3** `runtime_for` 用 `registry_metadata()` 热路径昂贵 → 改 `ToolManager.runtime_of()` 直读
  `_plugins[name].runtime`（仿 `set_hitl_broadcast` 先例）。
- **IMP-4** `_build_request_frame` 用 `doc["source_call_id"]` 直接索引有 KeyError 风险 → 改 `doc.get`。
- **MIN-5** 「5 个构造点」→ 实际 4 个直接构造点（bridge:386 / gen_contract_frames:230 /
  test_desktop_tools:49/71），§8 已修。
- **MIN-6** submit_task `v1_args` 推导加 `isinstance(dict)` 守卫（非 dict 置 `{}`）。
- **MIN-7** `_truncate_output_summary` 截断含省略号超上限 → `content[:max_len-1] + "…"`（总长 ≤ max_len）。
- **MIN-8** `ToolStatusMessage` 是 v2/Electron Client→Server 回传非 v1，D-status-runtime 措辞已修。
- **MIN-9** `_dispatch_tool_calls`/`_run_submit_tasks` 的 `conversation_id` 签名显式化。
- **MIN-10** v2 `truncated` 只表 outputSummary 截断，工具自身截断信号从归档回放取（§4.4 注记）。
- **MIN-11** 文件型工具 outputSummary 取截断前缀即可接受语义；D-§7-4 只覆盖 desktop tool.status 通道。
- **REC** ServerMessage 联合行域 `:992-1019` → `:992-1033`（已修）；D-GL-01 显式 overturn 记录
  §3.5「零 churn」（已修）；失败工具 v2 验收测试（并入 CRIT-1 修复）。

**复验 1 轮**（同 agent）结论 **Ready to merge: Yes**——无 Critical/Important，3 Minor 措辞优化全消化：
MIN-1 §5.2 失败路径措辞对齐 `_broadcast` 实际语义（`return_exceptions=True` 吞异常记 warning、不传播，
测试应断言 warning/其它通道照常）；MIN-2 submit_task `v1_args` 传 `{}`（v1_suppress 下不消费）；
MIN-3 `error.code="TOOL_ERROR"` + `errorType` 缺省并存为预期（前端权威错误键为 `error.code`）。


