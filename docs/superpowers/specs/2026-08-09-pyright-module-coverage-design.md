# pyright 覆盖扩展到 Module/ + frontend_contract/ 设计

> 状态：设计定稿（2026-08-09）｜关联：[code-style-guide §1.4](../../../code-style-guide.md)、[core-id-newtype-design](./2026-08-08-core-id-newtype-design.md)

**Goal:** 将 pyright 类型检查门禁从 `core/` 扩展到 `Module/` + `frontend_contract/`，全仓生产代码纳入类型验证。

## 背景与动机

2026-08-08 引入 pyright 门禁时 `pyrightconfig.json` 仅 `include: ["core/"]`。实测后果：

- **frontend_contract/**：契约层类型化程度高，pyright **0 errors**——纯收益可直接纳入。
- **Module/**：28 errors（从未被 pyright 检查积累的类型欠账）——`Module/workflow_engine/core/executor.py` 等 Module 层改动（如衍生 ID 批次的 `session_id→SessionId`）**无类型门禁验证**，靠测试 + 人工审查兜底。

扩展后全仓生产代码纳入 pyright 覆盖，Module 层改动获得与 core 相同的类型安全网。

## 基线（2026-08-09 实测）

| 范围 | pyright | 明细 |
|------|---------|------|
| `frontend_contract/` | **0 errors** | 直接加 include，零清理 |
| `Module/` | **28 errors** | 见下分类 |

### Module 28 errors 分类

| 类别 | 数量 | 位置 | 错误本质 |
|------|------|------|---------|
| Optional 组件守卫后未窄化 | **11**（6 调用点） | `Module/workflow_engine/core/sub_agent/sub_agent.py`（:219/:231/:316/:341/:412/:440） | `self._planner: Planner \| None`，`_require()` 守卫（返回 None）后 pyright 不窄化，传非 Optional 参数 |
| 契约枚举传裸 str | 6 | `Module/output/websocket.py:165,182,226,289-291` | `AudioStartMessage(format=...)`/`UserAckMessage(status=...)`/`GenerationCompletedMessage(channel=..., finishReason=..., completeness=...)` 传 str 给契约枚举/Literal 字段 |
| int\|float 类型算术/传参 | 5 | `Module/workflow_contract/schema.py:193,202,206,210,218` | `_resolve_field` 内 `task_val > global_val`（`int\|float` vs `int\|float\|None`）；返回值 `int\|float\|None` 传 `int\|None` 字段 |
| 散落真实类型欠账 | **6** | `Module/tool/manager.py:443,447`、`Module/tool/protocol.py:152`、`Module/tts/registry.py:126`、`Module/llm/openai_compat.py:222`、`Module/database/vectordb/milvus.py:118` | dict setitem 类型、枚举字面量、库 stub 误报、API 参数类型 |

## 设计决策

### 决策 1：include 扩展范围

`pyrightconfig.json` 的 `include` 从 `["core/"]` 改为 `["core/", "Module/", "frontend_contract/"]`。`exclude`（`**/tests/**`、`**/test_*.py`、`**/__pycache__`）不变——测试文件继续豁免（pytest fixture 动态类型惯例，§4）。

### 决策 2：Module 28 errors 全清，不 ignore（库类型误报豁免）

这些绝大多数是真实类型欠账（非误报），**真修，不用 `# type: ignore`**。理由：
- Optional 守卫问题（sub_agent）是真实的可空性缺口（组件在 run() 前未初始化，pyright 正确报）
- 契约枚举传 str（websocket）是真实的契约对齐缺口
- int|float（schema）是真实类型收窄缺口
- ignore 会掩盖未来回归，违背门禁目的

**唯一豁免——库类型解析误报**：`milvus.py:118` 的 `has_collection` 运行时是同步（pymilvus 3.x `iscoroutinefunction=False`），pyright 因库 stub 类型解析误判为 coroutine——对该行允许定点 `cast(bool, ...)` 或 `# type: ignore[reportReturnType]`（`openai_compat.py` 已有 5 处同类库 stub 定点 ignore 先例 :155/161/220/226/240）。

### 决策 3：修法分类（按 error 本质）

**类别 A — Optional 守卫窄化（sub_agent.py，11 errors / 6 调用点）**
`self._require("_planner", ...)` 语义是"非 None 检查"但返回 None，pyright 不窄化。修法：**`_require()` 调用后显式 `assert self._xxx is not None`**（6 调用点，多组件调用点逐组件 assert）——语义重复但显式化，让 pyright 理解窄化；`_require` 保持返回 None（守护语义不变）。
> 备选：`_require` 改泛型返回组件（`-> T`）。但组件类型各异（Planner/Orchestrator/...），泛型会复杂化 `_require` 签名。选 assert（改动局部、语义清晰）。

**类别 B — 契约枚举/Literal 收口（websocket.py，6 errors）**
契约构造点（`AudioStartMessage`/`AudioEndMessage`/`UserAckMessage`/`GenerationCompletedMessage`）参数传 str。修法：**构造点收口枚举**，但**注意字段实际类型**（已核对 frontend_contract/ws_message.py）：
- `format=AudioFormat(format)`（AudioStartMessage :165 字段 `AudioFormat`、AudioEndMessage :182 字段 `AudioFormat | None`，两处均可赋）✓ 枚举收口
- `status=AckStatus(status)`（UserAckMessage :226）✓ 枚举收口
- `finishReason=FinishReason(finish_reason)`（:290）✓ 枚举收口
- **`channel`（:289）字段是 `Literal[MessageChannel.PUBLIC_REPLY]`，不是 `MessageChannel`——`MessageChannel(channel)` 过不了 pyright。修法优先：`on_generation_completed` 参数类型改 `channel: Literal[MessageChannel.PUBLIC_REPLY] = MessageChannel.PUBLIC_REPLY`（保留参数语义，同过 pyright）；若改动面大则构造点硬编码 `channel=MessageChannel.PUBLIC_REPLY`**（唯一生产调用方 `manager.py:487` 未传 channel 恒走默认）
- **`completeness`（:291）字段是 `Literal["complete"]`，恒 "complete"。修法：硬编码 `completeness="complete"`**（或参数类型 `Literal["complete"]`），不做条件式

wire 发射时 StrEnum 序列化为成员值，字节不变，前端无感（§2.2）。
> **边界注**：枚举构造（`AudioFormat(format)` 等）对枚举外值抛 `ValueError`——当前唯一生产调用点 `loop.py:1052` 硬编码 `"stop"`，无运行时变化；但这是新的失败模式（非法字符串不再静默透传而是报错），记录为已知权衡。

**类别 C — int|float 类型收窄（schema.py，5 errors）**
`_resolve_field` 闭包内 `task_val > global_val`：`global_val` 可能 None（`task_val` 非 None 分支已排除）。修法：**比较前 `if global_val is not None and task_val > global_val`**（None 时视为无全局上限，直接返回 task_val）。返回值类型：`_resolve_field` 返回 `int | float | None` 传给 `WorkflowConstraints` 字段（`max_dag_nodes: int | None` 与 `max_task_duration_s: float | None` 混合）——修法：**`_resolve_field` 泛型化**（`_resolve_field[T: int | float](...) -> T | None`），按调用处字段注解实例化，避免字面 `int | None` 对 float 时长字段类型撒谎。

**类别 D — 散落 6 errors（逐文件真修）**
| 位置 | 修法方向 |
|------|---------|
| `Module/tool/manager.py:443,447` | dict `__setitem__` 值类型（`d[k] = v`，v 类型与 dict 注解不符）——核对容器注解收窄或值类型化 |
| `Module/tool/protocol.py:152` | `ToolStatus` 字面量——`ToolStatus("processing")` 或枚举成员 |
| `Module/tts/registry.py:126` | `dict[str, str]` setitem int——**只改容器注解**（如 `dict[str, Any]`），**不能 `str(...)` 值转换**（`create()` pop `rate_limit_rpm` 后按 int 传后端，str() 会改运行时行为） |
| `Module/llm/openai_compat.py:222` | `stream_options` 参数类型（`dict[str, bool]` vs OpenAI TypedDict）——核对实际 API 类型 |
| `Module/database/vectordb/milvus.py:118` | **库 stub 误报**（pymilvus `has_collection` 运行时同步、pyright 误判 coroutine）——`cast(bool, await asyncio.to_thread(...))` 或 `# type: ignore[reportReturnType]`（决策 2 豁免） |

> 散落项的精确修法（容器注解、cast 位置、API TypedDict）在 plan 阶段逐一核对代码后定，spec 只定方向。

### 决策 4：门禁命令不变

`make pyright` 已用 `conda run -n Elysia pyright`（无路径参数），include 配置改动即生效。**无 Makefile 改动**。

## Task 划分（6 个）

- **Task 1**：pyrightconfig.json include 扩展（core/ + Module/ + frontend_contract/）+ 配置变更 + 验证 frontend_contract 0 error + 确认 Module 28 error 基线不变
- **Task 2**：sub_agent.py **11 errors / 6 调用点**（类别 A，`_require` 后 assert 窄化；:219 双组件、:440 四组件调用点逐组件 `assert ... is not None`）
- **Task 3**：websocket.py 6 errors（类别 B，契约枚举/Literal 收口；channel/completeness 硬编码成员）
- **Task 4**：schema.py 5 errors（类别 C，`_resolve_field` 泛型化 + `global_val is not None` 守卫）
- **Task 5**：散落 **6 errors**（类别 D，逐文件真修；milvus 走决策 2 豁免 cast/ignore）
- **Task 6**：全量验证 + 文档（CLAUDE.md pyright 门禁描述更新——:36-38 与 :448 两处，:448 明确写 `include core/`；code-style-guide §1.4 同步；README changelog 若提及"core 生产代码 pyright 门禁"需扩为全仓；todo.md 若相关）

## 验证标准

1. `conda run -n Elysia pyright`：**0 errors**（含扩展后的 Module/ + frontend_contract/；`core/monitoring/__init__.py` 1 条既有 warning 恒在，非 error）。
2. `conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__`：通过。
3. 测试（Elysia env）：受改动影响的模块测试 + `test-quick` 全量回归（`-m "unit or service" --ignore=Module/workflow_engine/tests`）+ Module 相关（workflow_engine/output/tool）测试。
4. **无运行时行为变化**：所有修法都是类型层（assert/枚举收口/int 收窄），wire 字节不变（StrEnum 序列化为成员值）。

## 已知权衡（记录，不改）

- 测试文件继续豁免 pyright（`**/tests/**` exclude 不变）——pytest fixture 动态类型惯例。
- `Module/bench/`、`Module/_common/` 等在 include 范围内，当前 0 error 直接通过；若未来引入类型欠账由门禁兜底。
- 类别 B 枚举收口引入新失败模式：非法字符串传枚举构造抛 `ValueError`（当前生产路径无非法值，记录为行为收紧）。
- `milvus.py:118` 库 stub 误报用定点 cast/ignore（决策 2 豁免，`openai_compat.py` 已有同类先例）。
- 散落项修法以"最小改动消除 error"为准，不借机重构。

## 关联文档

- [code-style-guide.md](../../../code-style-guide.md) — §1.4 pyright 门禁（本次扩展其范围）、§2.2 StrEnum 契约枚举
- [2026-08-08-core-id-newtype-design.md](./2026-08-08-core-id-newtype-design.md) — core pyright 门禁引入先例
