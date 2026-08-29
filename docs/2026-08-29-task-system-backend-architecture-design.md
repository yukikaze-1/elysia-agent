# Cyrene 任务系统后端架构设计

> **文档状态：** 架构讨论稿（目标设计，未实现）  
> **日期：** 2026-08-29  
> **事实基线：** `192.168.1.18:~/elysia-agent@9548d100`  
> **权威范围：** 后端任务服务、WorkflowEngine 接入、工具执行协调、持久化、正式 REST/WS API 与 Desktop Runtime 协作  
> **上位设计：** [任务系统与任务中心总体架构设计](./2026-08-29-task-system-overall-architecture-design.md)  
> **前端配套：** `elysia-frontend/docs/2026-08-29-task-center-frontend-architecture-design.md`

---

## 0. 结论

后端不是“没有任务系统”，而是已经有若干可工作的任务相关部件，却缺少产品级门面、统一执行管线和可长期查询的数据模型：

- `Module/workflow_engine` 已能规划和执行后台 Workflow；
- `core/task/TaskScheduler` 已负责普通聊天工具的并发短调用；
- `core/desktop/DesktopToolBridge` 已能把 Desktop 工具请求交给 Electron，并维护幂等、状态与审计；
- Cognitive Loop 已能通过 `submit_task` 创建 Workflow，并轮询 mailbox 把终态结果送回对话；
- 但正式产品 API、历史存储、来源关联、统一 ToolExecution、跨 Runtime 上下文和重启语义仍未闭环。

目标架构不推倒这些能力，而是在其上建立两个应用层组件：

```text
正式 REST / WebSocket / Cognitive submit_task
                    │
                    ▼
             TaskService
      （产品任务门面与状态权威）
                    │
                    ▼
             WorkflowEngine
        （计划、依赖、编排与控制）
                    │
             ExecutionGateway
                    │
                    ▼
       ToolExecutionCoordinator
       （所有真实工具执行的共同管线）
          │         │          │
          ▼         ▼          ▼
       Backend    Desktop    Local Worker
       Runtime    Runtime     （未来扩展）
```

核心结论：

1. `WorkflowEngine` 保持领域内聚，不直接承担产品 API、历史分页和全部 ToolExecution 所有权；
2. `TaskService` 是 Task/TaskStep 的产品门面与唯一正式任务接口；
3. `ToolExecutionCoordinator` 统一普通直调、Workflow 节点和未来定时任务的执行记录、上下文、事件与错误分类；
4. 当前 `core/task/TaskScheduler` 继续负责短调用并发，但实际工具调用逐步下沉到 Coordinator；
5. Desktop 工具不迁入后端执行，仍由 Electron Main 执行；后端负责编排、关联和公共状态；
6. 第一阶段先做到“可查、可控、可关联、可恢复展示”，再做进程重启后的安全续跑；
7. Debug SSE 继续服务开发诊断，正式产品面使用版本化 REST + WebSocket v2 事件。

---

## 1. 范围与非目标

### 1.1 本设计范围

- Task、TaskStep、ToolExecution 的后端职责与数据边界；
- WorkflowEngine 与产品任务层的适配方式；
- 普通工具调用与 Workflow 工具调用的公共执行管线；
- Backend、Desktop、未来 Local Worker 三类 Runtime 的路由与等待语义；
- 正式任务查询、控制与执行记录 API；
- WebSocket 增量事件、revision、重连补偿和事件顺序；
- MongoDB 存储、索引、保留期和后端重启策略；
- 对话、消息、代次、任务、步骤和调用 ID 的来源关联；
- 幂等、取消、重试、权限、安全、可观测性和测试边界；
- 从当前实现迁移到目标架构的建议批次。

### 1.2 非目标

- 第一版实现分布式 Workflow 调度集群；
- 第一版支持多个 Desktop 客户端同时竞争同一任务；
- 第一版实现 Local Worker 守护进程；
- 第一版把所有简单工具调用包装成单节点 Workflow；
- 第一版保证所有运行中 Workflow 在后端重启后自动续跑；
- 把 Desktop 权限判定和本地敏感审计迁到后端；
- 向前端暴露内部 prompt、原始 checkpoint、完整工具参数或密钥；
- 用任务中心替代聊天内现有工具状态帧。

---

## 2. 当前实现事实基线

本节只描述基线提交中的实际代码，不把目标设计写成已实现。

### 2.1 当前组件清单

| 组件 | 当前职责 | 当前产品缺口 |
|---|---|---|
| `Module/workflow_engine` | Workflow 计划、DAG 执行、暂停/恢复/取消、审批、mailbox | 无正式产品 API；终态会清理；checkpoint/artifact 为内存实现 |
| `Module/workflow_contract` | Workflow 对基础设施的窄协议，避免 Engine 依赖 `core` | 尚无产品级 ToolExecution gateway 协议 |
| `core/cognitive/tools.py` | `submit_task` 的委派准备与 Workflow 提交 | 返回 `async_task_id`，但没有产品 TaskSummary 持久化 |
| `core/agent/agent.py` | `_background_tasks` 注册、mailbox 轮询、终态结果回投 | 注册表在内存；来源关联不足；不是历史查询源 |
| `core/task` | 普通工具短调用的 worker pool、任务对象和调度 | `BACKGROUND` 类型未形成产品任务系统；命名易与产品 Task 混淆 |
| `core/cognitive/tool_phase.py` | 聊天工具调用的 started/completed 等帧和后台任务注册 | Workflow 内部工具调用没有复用同一帧/执行记录管线 |
| `core/desktop/bridge.py` | Desktop 请求交付、状态回传、断连语义和公共审计 | 当前连接模型主要是单连接；Task/Step/Execution 关联不足 |
| `core/storage/desktop_call_repo.py` | `desktop_calls` Mongo 存储与 TTL | 只覆盖 Desktop 调用，不是跨 Runtime 执行历史 |
| `frontend_contract` | 后端 Pydantic 原始契约、JSON Schema 和 TS 生成 | 尚无正式 Task/TaskStep/ToolExecution 产品契约 |
| `core/input/server.py` | HTTP/WS 服务装配 | 只有 Debug Task 路由，无正式 Task 路由 |

### 2.2 WorkflowEngine 当前能力

当前 `WorkflowEngine` 已提供内部 Python 方法，覆盖：

- 提交任务；
- 查询活动 Workflow；
- 暂停、恢复和取消；
- 审批继续；
- 通过 `WorkflowMailbox` 获取进度和终态事件；
- 对计划、依赖、资源预算和节点执行进行内部管理。

当前 Workflow 生命周期包含十二个内部状态：

```text
QUEUED
PENDING
PLANNING
RUNNING
PAUSE_REQUESTED
PAUSED
WAITING_APPROVAL
RESUMING
CANCEL_REQUESTED
CANCELLED
FAILED
COMPLETED
```

这些状态适合 Engine 内部精细控制，但不能原样成为唯一产品状态：产品层还需要 `waiting_for_runtime`、明确的权限等待、稳定 revision、来源关联和终态保留。

### 2.3 当前查询与 Debug API

当前任务 HTTP 入口只有：

```http
GET /debug/api/tasks
GET /debug/api/tasks/stream
```

它们位于 Debug 路由，面向开发诊断：

- 读取 Engine 私有活动快照；
- SSE 端点按固定间隔轮询；
- 没有产品分页、稳定 DTO、正式错误码和版本演进承诺；
- 不能作为任务中心的长期协议。

因此准确结论是：**WorkflowEngine 有内部控制接口和 Debug API，但尚未暴露正式产品任务 API。**

### 2.4 当前终态与持久化

当前 Workflow 进入终态后，会从 Engine 的活动任务、mailbox、上下文和 future 等内存结构中清理；相关 checkpoint 和 artifact 也会进入清理流程。`CheckpointManager` 与 `ArtifactStore` 当前实现均以进程内存为主。

由此带来三个直接结果：

1. Debug 查询主要只能看到活动任务；
2. 进程重启后没有产品级任务历史；
3. 文档中出现的“checkpoint”不能等同于可跨进程恢复的持久化 checkpoint。

### 2.5 普通聊天工具调用路径

正常生产路径中，Cognitive Tool Phase 把工具调用交给 `core/task/TaskScheduler.dispatch_tool_calls`，由短调用 worker pool 并发执行。Agent 内仍有无回调时的直接执行 fallback。

调度时会从基础 `ToolContext` 派生每次调用上下文，至少能保留现有 services，并写入该次 `call_id` 和 session 信息。这是 Desktop Bridge、记忆、HTTP 客户端等依赖可以工作的关键。

`TaskScheduler` 目前还定义了 `TaskType.BACKGROUND` 和后台分派能力，但生产中的后台 Workflow 主要由 `WorkflowEngine` 承担。这一类内部 scheduler task 不应直接升级成产品 Task。

### 2.6 Workflow 工具调用路径

当前 Workflow Executor 直接使用注入的 ToolAdapter，并自行构造新的 `ToolContext`。该上下文只有有限的 session/trace 信息，没有完整复制 Agent 运行时注入的 services，也没有稳定的产品 execution/task/step 关联。

这会造成结构性差异：

- Backend 纯函数型工具通常仍可执行；
- 依赖 `ctx.services` 的 Desktop Bridge、记忆或其他服务可能缺失；
- Workflow 节点没有进入普通 Cognitive Tool Phase 的统一 started/completed 事件链；
- 同一工具因调用入口不同而获得不同能力和可观测性。

这正是需要引入 `ToolExecutionCoordinator` 的主要原因。

### 2.7 Desktop Runtime 当前事实

Desktop 工具的真实执行位置是 Electron Main，后端插件只是委派器：

```text
Backend desktop tool plugin
  → DesktopToolBridge
  → WebSocket tool.request
  → Electron Main 权限与执行
  → WebSocket tool.status / result
  → Backend ToolResult
```

当前 Bridge 已具备重要基础：

- 连接存在性判断；
- 调用状态机；
- `source_call_id`；
- Mongo `desktop_calls` 审计；
- 断连、重连、结果补传和幂等防护；
- 终态 TTL；
- 非法状态跳转和篡改防护。

但它目前只有临时连接维度，且公共记录没有完整 Task/Step/Execution 来源字段。它应继续作为 Desktop 传输与审计组件，而不是变成 TaskService。

### 2.8 当前 Backend 工具与 Desktop 工具

基线注册的 Backend 工具包括记忆、时间、网络、文件、提醒、运行时配置、自身状态和视觉理解等插件；Desktop Runtime 当前有四类本地工具：

- `desktop.file.select`（文件选择）；
- `desktop.file.read`（文本读取）；
- `desktop.file.write`（文本写入）；
- `desktop.screen.capture`（屏幕截图）。

具体工具清单未来可以增加，不应硬编码在 TaskService。Runtime 与能力应从 Tool Registry/定义元数据解析。

### 2.9 当前提醒与 Local Worker

- `set_reminder` 目前是进程内 `asyncio` 延时任务，缺少持久化、查询、取消和后端重启恢复；
- `local_worker` 已进入跨端类型设计，但当前不存在独立可用的 Local Worker 进程和协议。

二者都是任务系统的自然扩展点，但不能在第一版 UI 中伪装成已经完备的能力。

---

## 3. 当前主要缺口与根因

| ID | 缺口 | 根因 | 用户影响 |
|---|---|---|---|
| BE-G01 | 正式任务列表/详情/控制 API 缺失 | Engine 直接面向内部调用，Debug API 无产品门面 | 前端任务中心只能占位或错误依赖 Debug 接口 |
| BE-G02 | 终态历史不可查 | Engine 内存结构终态清理，没有 Summary Store | 完成后任务从列表消失 |
| BE-G03 | 普通调用与 Workflow 节点记录分裂 | 两条路径直接调用 ToolAdapter，事件机制不同 | 无法形成统一执行记录 |
| BE-G04 | Workflow 上下文不完整 | Executor 自建最小 ToolContext | Desktop/服务依赖型工具在 Workflow 中不可靠 |
| BE-G05 | 对话来源关联不稳定 | `_background_tasks` 只保存有限 intent/mailbox 信息 | 切换会话后结果可能无法精确回到来源 |
| BE-G06 | “离线”等待与“执行失败”混淆 | Runtime 可用性没有成为一等状态 | 用户无法区分稍后可继续与真正失败 |
| BE-G07 | 重启语义未定义 | checkpoint/artifact 非持久化，副作用未知 | 重启后任务丢失或存在危险重放风险 |
| BE-G08 | `TaskScheduler` 和产品 Task 同名异义 | 早期内部调度命名被沿用 | 后续维护者容易把两个层次错误合并 |
| BE-G09 | Desktop 连接只有临时身份 | `connectionId` 主要代表一次 WS 连接 | 无法稳定表达“这个任务等待哪台设备” |
| BE-G10 | 事件 payload 松散 | Workflow mailbox data 以字典为主 | 跨层契约容易漂移，难以生成前端类型 |

根因并不是 Engine 能力太弱，而是“编排内核”被直接当成了“产品任务系统”。两者之间缺少应用服务、持久化投影和统一执行协议。

---

## 4. 设计原则

### 4.1 领域分离

```text
Task        = 用户可理解、可查询、可控制的目标
TaskStep    = Workflow 中一个有计划语义的步骤
ToolExecution = 一次真实工具执行尝试
```

一个 Task 可以没有工具执行，例如等待用户决策后被取消；一个 ToolExecution 也可以没有 Task，例如普通聊天中的直接工具调用。

### 4.2 简单工具不强制走 Workflow

低延迟、一次性、上下文明确的普通工具调用继续直调。只有满足复杂、多步骤、长耗时、后台、可暂停或需要依赖编排等条件时，才由 Cognitive 层选择 `submit_task`。

### 4.3 Engine 依赖窄协议

`Module/workflow_engine` 不应反向依赖 `core`。它只依赖 `Module/workflow_contract` 中的协议，例如 ExecutionGateway、CheckpointStore 和事件 sink；具体实现由 Agent 组装。

### 4.4 状态先持久化，事件后发布

前端收到事件后必须能通过 REST 读取到相同或更新状态。所有产品状态变更遵循：

```text
校验命令 → 原子更新状态/revision → 写事件/outbox → 发布 WS
```

不能先广播“已暂停”，再异步尝试落库。

### 4.5 不安全的未知副作用不得自动重放

对写文件、写剪贴板、打开链接、通知等有副作用的执行，如果后端重启时无法确定结果，必须标为 `outcome_unknown` 或需要人工决策，不能自动重试。

### 4.6 权威来源明确

- Backend：Task、TaskStep、跨 Runtime 公共 ToolExecution 摘要；
- WorkflowEngine：活动 Workflow 的编排细节；
- Electron Main：Desktop 权限判断、本地执行和本地敏感审计；
- MongoDB：产品历史和 revision 的持久化事实；
- Renderer：只保存展示投影，不成为执行权威。

---

## 5. 目标分层与依赖方向

### 5.1 组件职责

| 层 | 建议组件 | 职责 |
|---|---|---|
| 输入/契约 | `core/input/task_routes.py`、`frontend_contract` | 鉴权、参数校验、DTO、错误映射、WS 公开事件 |
| 应用服务 | `core/task_service/TaskService` | Task 命令、查询、来源关联、状态投影、历史、控制 |
| 执行协调 | `core/tool_execution/ToolExecutionCoordinator` | Runtime 路由、完整上下文、执行记录、统一结果与事件 |
| 编排内核 | `Module/workflow_engine` | 计划、DAG、节点调度、暂停/恢复/取消、内部 checkpoint |
| 窄协议 | `Module/workflow_contract` | Engine 可依赖的 gateway/store/event Protocol 和稳定数据结构 |
| 基础设施 | `core/storage`、`core/desktop`、ToolAdapter | Mongo、Desktop 传输、工具实际执行 |

### 5.2 依赖规则

允许：

```text
core/input → core/task_service
core/task_service → Module/workflow_engine
core/task_service → core/storage
core/tool_execution → core/adapter + core/desktop + core/storage
Module/workflow_engine → Module/workflow_contract
Agent composition root → 上述所有实现
```

禁止：

```text
Module/workflow_engine → core/task_service
Module/workflow_engine → core/desktop
Module/workflow_engine → frontend_contract
Renderer/HTTP route → WorkflowEngine 私有字段
TaskService → Electron 本地权限存储
```

### 5.3 为什么新增 `core/task_service`

当前 `core/task` 已经代表内部短调用调度器。如果直接把产品 TaskService 塞进去，`Task` 一词会同时指 worker-pool job、Workflow 和用户任务。

建议先新建语义明确的 `core/task_service/`。待迁移稳定后，再选择：

- 将现有 `core/task` 重命名为 `core/tool_scheduler`；或
- 保留路径，只把公开类命名收窄为 `ToolCallScheduler`。

这不是第一批的前置重构，但文档和新代码不得继续扩大歧义。

---

## 6. 后端领域模型

公共字段以上位总体设计为准；本节补充后端内部需要。

### 6.1 TaskRecord

建议字段：

```text
task_id
kind                    # workflow / scheduled / future extension
title
summary
status
status_reason_code
status_reason_public
source_session_id
source_conversation_id
source_message_id
source_generation_id
owner_user_id
bound_client_instance_id
workflow_id
progress_completed
progress_total
current_step_id
created_at
started_at
updated_at
finished_at
revision
schema_version
```

约束：

- `task_id` 是产品稳定 ID，由 TaskService 在提交 Engine 前生成；
- `workflow_id` 是 Engine 内部关联 ID，可与 `task_id` 相同，也可通过 adapter 映射；
- 对外不返回内部 prompt、完整计划上下文和敏感输入；
- `revision` 每次可见状态变化单调递增。

### 6.2 TaskStepRecord

建议字段：

```text
step_id
task_id
workflow_node_id
parent_step_ids
display_order
title
summary
status
runtime
tool_name_public
attempt_count
latest_execution_id
started_at
updated_at
finished_at
revision
```

第一版 UI 使用线性时间线，但存储保留依赖数组，以免丢失 DAG 语义。

### 6.3 ToolExecutionRecord

建议字段：

```text
execution_id
source_call_id
desktop_call_id
task_id
step_id
attempt
origin                  # direct_chat / workflow / scheduled / system
runtime                 # backend / desktop / local_worker
tool_name
tool_label_public
status
status_reason_code
status_reason_public
source_session_id
source_conversation_id
source_message_id
source_generation_id
client_instance_id
trace_id
started_at
updated_at
finished_at
duration_ms
result_summary_public
error_summary_public
revision
schema_version
```

内部可另存加密或受控参数摘要，但公共 API 默认不返回原始参数、路径、剪贴板内容、URL 查询参数或工具原始输出。

### 6.4 ID 语义

| ID | 生成方 | 生命周期 | 用途 |
|---|---|---|---|
| `taskId` | TaskService | 产品任务全生命周期 | 列表、详情、控制和来源跳转 |
| `stepId` | TaskService/Workflow adapter | Task 内稳定 | UI 步骤和执行归属 |
| `executionId` | ToolExecutionCoordinator | 一次执行尝试 | 跨 Runtime 公共记录和事件 |
| `sourceCallId` | Cognitive/调用来源 | 一次逻辑工具调用 | 与聊天工具卡关联 |
| `desktopCallId` | Desktop Bridge | 一次 Desktop 传输调用 | Bridge 幂等和回传 |
| `connectionId` | WS 服务 | 一次连接 | 只用于传输，不作为设备身份 |
| `clientInstanceId` | Electron Main | 一次桌面安装实例的稳定身份 | Runtime 绑定、重连和等待提示 |

不得把 `executionId` 与 `desktopCallId` 强行设为同一个值。它们可以一对一关联，但职责不同。

---

## 7. TaskService 设计

### 7.1 责任

`TaskService` 是正式产品任务的唯一应用门面：

- 创建 TaskRecord 并提交 Workflow；
- 把 Engine 内部状态投影为产品 Task/TaskStep 状态；
- 查询列表、详情和步骤；
- 接受 pause/resume/cancel 命令；
- 保存来源会话与消息关联；
- 订阅 Engine 和 ToolExecution 事件；
- 写 revision 与公共事件；
- 在任务终态后保留历史；
- 将终态结果精确回投到来源会话，而不是当前活跃会话。

### 7.2 建议内部接口

```python
class TaskService:
    async def submit(self, command: SubmitTaskCommand) -> TaskRecord: ...
    async def list(self, query: TaskListQuery) -> TaskPage: ...
    async def get(self, task_id: str) -> TaskDetail | None: ...
    async def pause(self, task_id: str, command_id: str) -> TaskRecord: ...
    async def resume(self, task_id: str, command_id: str) -> TaskRecord: ...
    async def cancel(self, task_id: str, command_id: str) -> TaskRecord: ...
```

这是目标接口示意，不要求类名和方法签名逐字照抄；必须保持的语义是：产品命令不直接穿透到 Engine 私有对象。

### 7.3 提交流程

```text
1. 验证用户、来源会话与存储可用性
2. 生成 taskId，持久化 queued TaskRecord
3. 发布 task.created
4. 构造 WorkflowSubmission，并携带 taskId/source context
5. 调用 WorkflowEngine.submit
6. 保存 workflowId 映射和 planning/running 状态
7. 返回 Task DTO；submit_task ToolResult 同时携带 taskId
```

如果第 4–5 步失败，TaskRecord 必须进入明确 `failed`，而不是从历史中消失。

### 7.4 控制命令

所有控制接口接受 `commandId`，用 `(taskId, commandId, action)` 做幂等：

- 同一命令重复提交，返回第一次结果；
- 对终态任务再次 cancel，返回当前终态，不重复触发副作用；
- 非法状态转换返回机器可读错误码和当前 revision；
- `pause` 是请求，不承诺正在执行的不可中断工具立即停下；
- `cancel` 会阻止新步骤，并尽力取消可协作中断的活动执行。

### 7.5 Engine 状态映射

建议映射：

| Engine 状态 | 产品 Task 状态 |
|---|---|
| `QUEUED`, `PENDING` | `queued` |
| `PLANNING` | `planning` |
| `RUNNING` | `running`，除非当前步骤覆盖为 Runtime/权限等待 |
| `WAITING_APPROVAL` | `waiting_for_permission` |
| `PAUSE_REQUESTED` | `pausing` |
| `PAUSED` | `paused` |
| `RESUMING` | `resuming` |
| `CANCEL_REQUESTED` | `cancelling` |
| `COMPLETED` | `completed` |
| `FAILED` | `failed` |
| `CANCELLED` | `cancelled` |

`waiting_for_runtime` 是产品层/执行层补充状态，不要求污染 Engine 的通用内部枚举。TaskService 根据活动 step 的 execution disposition 投影该状态。

### 7.6 替代 `_background_tasks`

迁移后，Agent 的 `_background_tasks` 不再作为产品状态源。可分两步替代：

1. 先保留 mailbox 兼容适配，但登记内容改为 `taskId + sourceConversationId + sourceMessageId`；
2. 再由 TaskService 的终态事件处理器负责结果回投和通知，移除 Agent 定时轮询注册表。

回投 payload 至少包含：

```text
taskId
sourceConversationId
sourceMessageId
status
publicSummary
finishedAt
```

若来源会话当前未打开，结果仍写入该会话历史；通知由前端策略决定。

---

## 8. ToolExecutionCoordinator 设计

### 8.1 为什么不把 ToolExecution 合并进 WorkflowEngine

ToolExecution 同时服务以下来源：

- 普通聊天直调；
- Workflow 节点；
- 未来持久化提醒/定时任务；
- 系统补偿或人工重试。

如果把它放进 WorkflowEngine，普通调用要么绕开统一记录，要么被迫创建虚假 Workflow。正确做法是让 Engine 依赖一个窄 ExecutionGateway，而 Coordinator 位于 Engine 之外。

### 8.2 ExecutionRequest

建议在 `Module/workflow_contract` 中定义不依赖 `core` 的窄结构：

```text
execution_id?           # 未提供时由 Coordinator 生成
source_call_id
origin
tool_name
raw_params              # 只在进程内传递，不直接写公共记录
session_id
conversation_id
message_id
generation_id
task_id
step_id
attempt
preferred_runtime
client_instance_id
trace_id
```

ExecutionGateway 返回结构化结果：

```text
execution_id
outcome                 # completed / failed / rejected / cancelled / timeout / waiting_runtime
tool_result
public_error_code
public_error_message
retryable
```

### 8.3 统一执行流程

```text
调用来源
  → Coordinator.resolve(tool definition + runtime)
  → 创建 requested ToolExecutionRecord
  → 构造/克隆完整 per-call ToolContext
  → 更新 waiting/running，持久化并发布事件
  → ToolAdapter.execute / Desktop delegate / future worker
  → 规范化 ToolResult 与错误
  → 持久化终态、duration、公共摘要
  → 发布 tool.execution.updated
  → 返回调用来源
```

### 8.4 ToolContext 规则

Coordinator 必须从 Agent 已启动的基础上下文克隆，而不是重新创建一个缺少 services 的空上下文。每次调用至少写入：

- `call_id = sourceCallId`；
- `session_id`；
- `trace_id`；
- 保留 `services`、user、agent、config 等既有上下文；
- 通过受控 extension/context metadata 携带 `executionId/taskId/stepId`。

如果 `ToolContext` 当前没有产品关联字段，优先新增类型化字段或只读 metadata；不要把这些 ID 临时塞入 params 传给插件。

### 8.5 普通调用接入

当前 `TaskScheduler` 的 worker pool 与并发控制可保留，只把 worker 内真正的：

```text
ToolAdapter.execute(...)
```

替换为：

```text
ToolExecutionCoordinator.execute(origin=direct_chat, ...)
```

Cognitive Tool Phase 继续产生聊天内工具帧，并通过 `sourceCallId ↔ executionId` 关联独立历史。无需创建 TaskRecord。

### 8.6 Workflow 接入

Workflow Executor 不再自行构造最小 ToolContext 并直接调用 ToolAdapter，而是调用 `ExecutionGateway.execute`：

```text
Workflow node
  → ExecutionRequest(taskId, stepId, attempt, toolName, params, source context)
  → Coordinator
  → ToolAdapter / Desktop Bridge
```

Engine 只关心结果、重试提示和等待 disposition，不读取 ToolExecutionRepository。

### 8.7 事件与进度

工具流式进度可以同时服务：

- 聊天内实时工具卡；
- TaskStep 进度摘要；
- ToolExecution 最新进度。

但高频 chunk 不应全部永久写 Mongo。建议：

- 进度节流后通过 WS 发布；
- 数据库只保存最新公共进度和关键状态转换；
- 原始输出继续走既有消息/附件机制，不复制进任务历史。

---

## 9. Runtime 路由与等待语义

### 9.1 Runtime 解析

Runtime 由 Tool Registry 中工具定义元数据决定，调用方可给偏好但不能伪造能力：

| Runtime | 执行位置 | 当前状态 |
|---|---|---|
| `backend` | Python 后端进程 | 已存在 |
| `desktop` | Electron Main | 已存在 |
| `local_worker` | 独立本地执行进程 | 仅保留扩展位，未实现 |

不存在匹配 Runtime 时，应返回结构化 `RUNTIME_UNSUPPORTED`，而不是尝试在错误位置执行工具。

### 9.2 Desktop 离线

Desktop 离线不应默认等同于工具失败。对 Workflow Task，建议：

```text
execution.status = waiting_for_runtime
step.status      = waiting_for_runtime
task.status      = waiting_for_runtime
reason           = DESKTOP_OFFLINE
```

当绑定的 Desktop client 重连且任务未取消时，TaskService/Coordinator 可恢复派发。

普通聊天直调不适合无限等待，建议继续快速返回结构化不可用结果，同时写一条 `failed` 或 `rejected` execution；是否提示用户“转为后台等待”由产品层另行设计。

### 9.3 设备身份

`connectionId` 只代表一次 WebSocket 连接。应由 Electron Main 生成并持久化不含个人信息的 `clientInstanceId`，在握手时上报：

```text
clientInstanceId
platform
appVersion
capabilities
```

第一版只支持每个用户一个活跃 Desktop client：

- Task 可绑定创建它或批准它的 `clientInstanceId`；
- 新连接替换旧连接时，服务端明确关闭/失效旧连接；
- 多设备选择、迁移和并发执行留到后续设计。

### 9.4 权限等待

Desktop 权限仍由 Electron Main 的 ToolService 判断。后端只接收公共状态：

```text
awaiting_permission
approved
rejected
expired
```

后端不得持久化完整本地 grant 规则。等待审批时，Task/Step 映射为 `waiting_for_permission`；批准后回到 `running`，拒绝后根据 Workflow 策略失败、跳过或请求重新规划。

### 9.5 Local Worker 扩展

未来 Local Worker 应复用与 Desktop 类似的 RuntimeGateway，但至少先定义：

- 稳定 worker 实例身份；
- 能力声明；
- 心跳与离线；
- 请求幂等；
- 取消与结果回传；
- 文件/资源沙箱；
- 独立权限模型。

第一版不得把 Desktop Bridge 重命名后直接当作 Local Worker 协议；两者的权限面不同。

---

## 10. 持久化设计

### 10.1 建议集合

| 集合 | 所有者 | 内容 |
|---|---|---|
| `tasks` | TaskService | 产品任务摘要、状态、来源、revision、终态 |
| `task_steps` | TaskService/Workflow adapter | 步骤投影、依赖、状态、最新执行关联 |
| `tool_executions` | Coordinator | 跨 Runtime 执行摘要与关联 |
| `task_commands` | TaskService | pause/resume/cancel 幂等命令，可按保留期清理 |
| `task_events` 或 outbox | TaskService | 可靠事件发布和重连补偿，是否独立集合待实现评估 |
| `workflow_checkpoints` | Workflow infrastructure | 后续跨重启恢复的内部 checkpoint |
| `desktop_calls` | Desktop Bridge | 保留现有传输审计，不与 `tool_executions` 合表 |

`desktop_calls` 与 `tool_executions` 是一对相邻但不同的记录：前者服务 Desktop 传输幂等和审计，后者服务跨 Runtime 产品历史。

### 10.2 核心索引

建议至少建立：

```text
tasks:
  unique(task_id)
  (owner_user_id, updated_at desc)
  (owner_user_id, status, updated_at desc)
  (source_conversation_id, created_at desc)

task_steps:
  unique(step_id)
  (task_id, display_order)
  (task_id, status)

tool_executions:
  unique(execution_id)
  (owner_user_id, updated_at desc)
  (task_id, step_id, attempt)
  (source_call_id)
  (runtime, status, updated_at desc)
  sparse(desktop_call_id)

task_commands:
  unique(task_id, command_id, action)
```

所有列表使用 cursor 分页，不使用会随并发插入漂移的深 offset 分页。

### 10.3 Revision 与并发

- Task 和 Execution revision 单调递增；
- 更新使用条件写入或事务，避免旧事件覆盖新状态；
- TaskStep 可拥有自己的 revision，也可同时推进 task aggregate revision；
- API 返回 `revision`，WS 事件携带同一值；
- 前端按实体 ID + revision 丢弃陈旧事件。

### 10.4 数据保留建议

具体期限仍待产品拍板，建议基线：

- 活动任务与活动执行：不做 TTL；
- 终态 Task/Step 摘要：先建议保留 90 天；
- 终态 ToolExecution 详情：先建议保留 30 天；
- Debug 细节和高频进度：短保留或不落库；
- `desktop_calls` 继续遵循现有 30 天 TTL；
- 删除账户时按用户维度级联清理公共记录。

TTL 只能对终态且有 `finished_at` 的文档生效，绝不能让长期等待的活动任务被误删。

### 10.5 存储不可用策略

对于承诺可查询、可恢复展示的产品 Task，建议 fail closed：

- Mongo 不可用时拒绝创建新的持久化后台任务，返回 `TASK_STORAGE_UNAVAILABLE`；
- 不允许 Engine 已经开始执行后才发现 TaskRecord 没有落库；
- 已在执行的任务若短暂写失败，进入受控重试/降级队列并报警；
- 普通聊天直调可继续执行，但 ToolExecution 历史标记为不可持久化或仅输出指标，不能虚假承诺完整历史。

这一策略牺牲短期可用性，换取任务中心不会出现“任务已执行但完全找不到”的不可解释状态。

---

## 11. 重启、恢复与未知结果

### 11.1 第一阶段恢复承诺

第一阶段只承诺：

- 历史 Task/Step/Execution 在重启后仍可查询；
- 重启前已经终态的任务不会丢失；
- 重启时仍处于非终态的任务被明确转为 `failed`；
- 原因码为 `BACKEND_RESTARTED` 或更精确的恢复失败码；
- 不自动重放未知副作用。

这比假装“自动恢复”更安全，也足以先完成任务中心闭环。

### 11.2 第二阶段安全续跑

完整续跑需要同时具备：

- Mongo/持久介质上的 Workflow checkpoint；
- 版本化计划和节点输入；
- Task/Step/Execution 对应关系；
- 每个工具的幂等性/副作用分类；
- 已提交但未确认结果的 reconciliation；
- artifact 的持久化引用；
- Engine 版本兼容检查。

只有纯读、天然幂等或有明确幂等键的步骤可自动重试。其他步骤恢复时进入 `waiting_for_permission` 或人工确认。

### 11.3 Desktop 断连与后端重启不同

- Desktop 断连：后端仍在，Task 可以 `waiting_for_runtime`；
- 后端重启：编排内存丢失，第一阶段 Task 进入明确失败；
- Electron 重启后补传已完成结果：必须由 `desktopCallId + executionId` 对账，不能创建第二次执行。

---

## 12. 正式 REST API

正式路由建议注册在认证中间件之后，并遵循现有 REST envelope/错误约定。

### 12.1 Task 查询与控制

```http
GET  /api/tasks
GET  /api/tasks/{taskId}
POST /api/tasks/{taskId}/pause
POST /api/tasks/{taskId}/resume
POST /api/tasks/{taskId}/cancel
```

列表查询参数建议：

```text
cursor
limit
status
kind
conversationId
createdAfter
createdBefore
```

详情返回 Task + 当前可见步骤 + 最近执行摘要。步骤或执行数量过大时采用子资源分页，不允许单个详情无限膨胀。

控制请求：

```json
{
  "commandId": "cmd_...",
  "expectedRevision": 17
}
```

`expectedRevision` 可选；传入时用于提示并发冲突。

### 12.2 ToolExecution 查询

```http
GET /api/tool-executions
GET /api/tool-executions/{executionId}
```

筛选建议：

```text
cursor
limit
taskId
stepId
conversationId
runtime
status
toolName
createdAfter
createdBefore
```

默认 DTO 只返回公共摘要。敏感参数和 Desktop 本地审计不得通过该 API 透传。

### 12.3 是否开放创建 Task API

第一版可以只让 Cognitive `submit_task` 创建任务，任务中心只查询和控制；若 UI 需要“新建任务”，再增加：

```http
POST /api/tasks
```

开放前必须明确输入是自然语言目标、模板 ID 还是结构化计划。前端不得直接提交 Engine 内部 DAG。

### 12.4 错误码建议

| 错误码 | HTTP | 语义 |
|---|---:|---|
| `TASK_NOT_FOUND` | 404 | 任务不存在或不属于当前用户 |
| `TASK_INVALID_TRANSITION` | 409 | 当前状态不允许该控制命令 |
| `TASK_REVISION_CONFLICT` | 409 | expectedRevision 已陈旧 |
| `TASK_STORAGE_UNAVAILABLE` | 503 | 无法保证持久化创建 |
| `TASK_RUNTIME_UNAVAILABLE` | 409/503 | 所需 Runtime 当前不可用，视命令语义决定 |
| `TASK_COMMAND_IN_PROGRESS` | 202/409 | 同类控制正在处理 |
| `EXECUTION_NOT_FOUND` | 404 | 执行记录不存在 |
| `RUNTIME_UNSUPPORTED` | 422 | 工具没有可用 Runtime |

错误响应沿用 `frontend_contract` 现有 envelope，不另造平行格式。

---

## 13. WebSocket 增量事件

### 13.1 公开事件类型

建议纳入现有 v2 WS 消息联合：

```text
task.created
task.updated
task.step.updated
tool.execution.updated
```

后端 raw wire 继续由 `frontend_contract` 建模，至少包含：

```text
type
timestamp
revision
task / step / execution
eventCursor?             # 只有实现服务端 outbox 回放时才提供
```

Electron Main 校验 raw frame 后，再映射为 Renderer 既有的 `event/eventId/sequence/timestamp` 公共事件外壳。`eventId/sequence` 是当前 Main 单连接领域事件编号；后端和前端跨重连收敛以实体 `revision` 为准，不能把它们混成同一个游标。

### 13.2 Payload 原则

- 只使用 `frontend_contract` 中的 Pydantic 模型；
- 不使用任意 `dict` 作为正式边界；
- payload 可是完整小型摘要，避免前端依赖 JSON Patch 实现细节；
- 高频进度应节流；
- 终态、权限等待、Runtime 等待和控制结果不得丢弃。

### 13.3 重连补偿

WS 只负责低延迟增量，不承担唯一真相。前端重连后：

1. 重新请求任务列表/活动任务快照；
2. 使用 revision 合并；
3. 若实现 outbox cursor，可从服务端 `eventCursor` 补事件；
4. 无 outbox 时也必须通过 REST 收敛到正确状态。

第一版可以不实现完整长期事件回放，但必须实现 REST 对账。

### 13.4 与 Debug SSE 的关系

Debug SSE 保留：

- 面向内部 DAG、调度器和调试细节；
- 可以比产品协议更频繁变化；
- 不纳入任务中心正式依赖；
- Debug 路由应在生产配置中继续受限。

---

## 14. `frontend_contract` 与契约生成

### 14.1 后端 SSOT

新增公共模型应落在后端 `frontend_contract`：

- TaskStatus、TaskStepStatus、ToolExecutionStatus；
- TaskSummary、TaskDetail、TaskStepSummary；
- ToolExecutionSummary/Detail；
- TaskPage、ToolExecutionPage；
- 控制请求/响应；
- Task WS message payload；
- 公共错误码枚举。

然后继续生成：

- `contract.schema.json`；
- `contract.raw.ts`；
- 前端同步的 generated raw contract。

前端可以再建立 domain adapter，但不能手抄一套与后端漂移的 raw DTO。

### 14.2 兼容策略

- 新字段优先可选/有默认值，再在后续版本收紧；
- 枚举新增必须验证前端未知值 fallback；
- 字段改名走兼容期，不原地静默替换；
- `schema_version` 只用于持久化迁移，API 版本按现有契约体系管理；
- raw contract 的生成差异必须进入后端契约测试和前端同步检查。

---

## 15. 来源关联与多会话正确性

### 15.1 必须显式传递的关联

任务与执行从创建起就应显式携带：

```text
sessionId
conversationId
messageId
generationId
taskId
stepId
sourceCallId
executionId
traceId
```

不允许在结果返回时再从“当前活动会话”推断来源。

### 15.2 代次与消息语义

- 任务来源消息被删除，不应删除正在执行的 Task；
- 重新生成新代次，不应让旧任务结果写入新代次；
- Task 详情可以显示来源已不可用；
- 来源跳转失败时前端仍能在任务中心查看摘要；
- 同一消息触发多个任务时依靠 taskId 分离。

### 15.3 结果回投

终态结果由 TaskService 以来源 ID 写入正确会话。若最终结果已经形成正式 assistant message，应保存该 `resultMessageId` 供任务中心跳转；若只形成系统通知，则保留公共 result summary。

---

## 16. 取消、重试和幂等

### 16.1 取消分层

```text
Task cancel
  → 阻止新 Step 启动
  → 请求取消活动 Workflow node
  → Coordinator 请求取消活动 Execution
  → Runtime 尽力协作中断
```

取消是有延迟的请求。状态从 `cancelling` 到 `cancelled`，只有确认不再有可见执行会继续改变任务结果后才能进入终态。

### 16.2 工具重试分类

建议 Tool Registry 增加执行特性：

```text
read_only
idempotent
side_effecting
supports_cancel
default_retry_policy
```

- 只读且瞬时错误：允许受限自动重试；
- 有幂等键的写操作：按相同 key 重试；
- 未知副作用：不自动重试；
- Desktop 断连：等待 Runtime，不盲目新建调用；
- 每次真实尝试生成新的 `executionId`，attempt 递增，逻辑 `sourceCallId` 可保持。

### 16.3 Desktop 幂等

同一次派发必须稳定携带 `desktopCallId` 和 `executionId`。重连重发时：

- Electron Main 已有终态：补传同一结果；
- 已 accepted/running：不得在未知情况下启动第二份；
- 未见过：才可接受执行；
- payload hash 不一致：拒绝为篡改/冲突。

---

## 17. 安全与隐私

### 17.1 授权

- 所有 Task/Execution 查询按认证用户过滤；
- 404 可同时表示不存在或无权访问，避免枚举；
- 控制命令验证任务所有权；
- Debug API 不得成为绕过鉴权的产品入口；
- Desktop 握手的 clientInstanceId 必须绑定当前认证会话。

### 17.2 数据最小化

公共历史只保存：

- 可展示工具名与 Runtime；
- 时间、状态和公开错误码；
- 经截断和脱敏的摘要；
- 必需关联 ID。

默认不保存/返回：

- 完整 prompt；
- 剪贴板正文；
- 文件正文和完整绝对路径；
- URL token/query secret；
- 模型内部推理；
- Electron 本地权限规则；
- 工具原始异常堆栈。

### 17.3 日志

结构化日志使用 ID 关联，不把 raw params 直接序列化。日志至少包含：

```text
task_id / step_id / execution_id / source_call_id / trace_id
runtime / tool_name / status / duration_ms / public_error_code
```

敏感信息仍遵循项目现有 redaction 规则。

---

## 18. 建议代码布局

目标结构示意：

```text
core/
  task_service/
    __init__.py
    service.py
    commands.py
    queries.py
    models.py
    status_projection.py
    workflow_adapter.py
    result_delivery.py
  tool_execution/
    __init__.py
    coordinator.py
    models.py
    runtime_router.py
    context_factory.py
    error_mapping.py
  storage/
    task_repo.py
    task_step_repo.py
    tool_execution_repo.py
    task_command_repo.py
  input/
    task_routes.py
    tool_execution_routes.py

Module/
  workflow_contract/
    execution.py
    events.py
    persistence.py
  workflow_engine/
    ... existing engine ...

frontend_contract/
  rest.py
  ws_message.py
  errors.py
```

文件名是建议，不是强制；关键是依赖方向和所有权。

### 18.1 Agent 装配顺序

建议 composition root：

```text
1. 初始化 Mongo repositories
2. 启动 ToolAdapter，得到完整基础 ToolContext
3. 初始化 DesktopToolBridge
4. 初始化 ToolExecutionCoordinator
5. 以 ExecutionGateway 注入 WorkflowEngine
6. 初始化 TaskService，并订阅 Engine/Coordinator 事件
7. 注册正式 routes 和 WS publisher
8. Cognitive submit_task / short scheduler 接入应用服务
```

停止顺序反向执行，并先停止接受新 Task，再 drain/取消活动执行。

---

## 19. 可观测性

### 19.1 指标

建议指标：

```text
task_submitted_total{kind}
task_terminal_total{status,reason}
task_active{status}
task_duration_seconds{status}
task_waiting_seconds{reason,runtime}
tool_execution_total{runtime,tool,status}
tool_execution_duration_seconds{runtime,tool,status}
tool_execution_retry_total{runtime,tool,reason}
task_command_total{action,result}
task_event_publish_failures_total
task_storage_failures_total
desktop_runtime_connected
```

工具名标签需控制基数，只使用注册表中的稳定名称。

### 19.2 Trace

Task 创建时继承或创建 `traceId`；每个 Step/Execution 建 child span。跨 WebSocket Desktop 委派时传播 trace correlation，但不要求 Electron 直接上传敏感 trace baggage。

### 19.3 运维查询

正式用户 API 与 Debug 运维查询分开。运维面可查看内部 Workflow ID、checkpoint 版本和失败堆栈；用户 API 只返回公共摘要。

---

## 20. 测试策略

本文不代表已经运行这些测试；以下是后续实现必须覆盖的验证面。

### 20.1 单元测试

- Engine → 产品状态映射；
- Task/Step/Execution revision 单调；
- ToolContext 完整克隆和关联字段；
- Runtime 路由；
- 错误码与敏感摘要映射；
- 控制命令幂等和非法转换；
- 副作用工具重试策略；
- cursor 编解码和所有权过滤。

### 20.2 契约测试

- Pydantic → schema → raw TS 生成一致；
- REST page/detail/control envelope；
- WS discriminated union；
- 未知枚举/可选字段兼容；
- 后端公共事件可被前端 Zod schema 接受。

### 20.3 集成测试

- 普通 Backend 工具经 Coordinator 但聊天行为不回归；
- 普通 Desktop 工具经 Coordinator 与 Bridge 完成闭环；
- Workflow Backend 节点使用完整 ToolContext；
- Workflow Desktop 节点在线、离线、审批、拒绝和重连；
- Task 终态后仍可 REST 查询；
- WS 丢失后 REST 对账；
- 跨会话切换后结果回到来源会话；
- 重复 pause/resume/cancel；
- Mongo 写失败时不会出现幽灵任务；
- 后端重启将未恢复任务明确收口。

### 20.4 安全测试

- 跨用户 Task/Execution 访问隔离；
- clientInstanceId 冒用；
- Desktop 重放与 payload hash 冲突；
- API/日志不泄漏工具参数；
- Debug 路由生产限制；
- 终态 TTL 不误删活动任务。

---

## 21. 分批实施建议

与总体设计保持一致，后端建议拆成五批。

### 批次 A：领域契约与持久化骨架

- 在 `frontend_contract` 增加 Task/Step/Execution DTO、状态和事件；
- 增加 repositories、索引、revision 和 page cursor；
- 建立 TaskService 基础查询/状态投影；
- 为当前 `submit_task` 增加 TaskRecord/Step 投影和终态清理前写入，不改变 Engine 执行路径；
- 上线只读 Task/Execution REST 查询；
- 现有工具实际执行路径暂不切换。

验收：当前 Workflow 的活动态和终态均可通过正式只读 API 查询，生成契约稳定；普通调用的完整 Execution 覆盖留到批次 B。

### 批次 B：统一 ToolExecution

- 新增 ExecutionGateway 与 Coordinator；
- 普通直调接入 Coordinator；
- Workflow Executor 接入 Gateway；
- 让全部真实执行写入批次 A 的 Execution read model，并上线执行增量 WS；
- 修复 Workflow ToolContext services 缺失。

验收：普通调用和 Workflow 节点都有统一执行历史，但普通调用仍不创建 Task。

### 批次 C：正式任务 API、控制与事件

- TaskService 完整接管 Cognitive `submit_task` 的产品命令与来源关联；
- Engine mailbox/状态投影写 Task/Step；
- 上线任务列表、详情、pause/resume/cancel；
- 上线 Task WS 事件和 REST 重连对账；
- 结果按显式来源回投。

验收：Backend-only Workflow 可在任务中心完整查询和控制，完成后不消失。

### 批次 D：Desktop Runtime 任务闭环

- 协议补齐 taskId/stepId/executionId/clientInstanceId；
- Desktop 离线映射 waiting_for_runtime；
- 权限等待映射；
- 重连对账、取消和结果补传；
- 前后端真机联调。

验收：Workflow 中的 Desktop 工具在线、离线、审批和重启 Electron 场景可解释且无重复副作用。

### 批次 E：持久化恢复与扩展

- 持久化 Workflow checkpoint/artifact；
- 安全恢复和未知结果人工处理；
- 将 `set_reminder` 迁为可查询的 Scheduled Task；
- 评估 Local Worker 和多设备。

验收：达到明确列出的跨后端重启恢复级别；未实现的 Runtime 不在产品中伪装可用。

---

## 22. 后端验收定义

后端任务系统达到第一版产品可用，至少满足：

1. TaskService 是正式任务查询和控制唯一入口；
2. Task 终态在 Engine 清理后仍可查询；
3. 普通工具调用不创建虚假 Task，但产生 ToolExecution；
4. Workflow 节点和普通调用复用同一 Coordinator；
5. Workflow Desktop 工具获得完整 ToolContext 和 Bridge service；
6. Desktop 离线、权限等待、用户拒绝和真实失败可区分；
7. pause/resume/cancel 有幂等 commandId 和合法状态校验；
8. WS 事件与 REST revision 可收敛，重连不依赖 SSE；
9. 对话切换后任务结果仍写入正确来源会话；
10. Backend 重启后的未完成任务有明确终态，不会静默消失或危险重放；
11. Task/Execution API 不泄漏原始敏感参数；
12. 后端契约能生成并被前端同步校验。

---

## 23. 待决策项

以下选择会改变数据或协议，实施相应批次前需要拍板：

| ID | 问题 | 建议基线 | 最晚决策点 |
|---|---|---|---|
| BE-D01 | 终态 Task/Execution 保留多久 | Task/Step 摘要 90 天，Execution 详情 30 天 | 批次 A 建 TTL 前 |
| BE-D02 | `taskId` 与 `workflowId` 是否同值 | 产品 ID 由 TaskService 生成，允许 Engine 接收外部 ID；仍保留两个字段 | 批次 C |
| BE-D03 | 是否第一版开放 `POST /api/tasks` | 否，只由 Cognitive 创建 | 前端“新建任务”设计前 |
| BE-D04 | 是否第一版实现 event outbox 回放 | 可先 REST 对账，终态事件需可靠；outbox 视当前 WS 基础设施评估 | 批次 C |
| BE-D05 | Mongo 不可用能否继续创建后台 Task | 否，fail closed | 批次 C |
| BE-D06 | 后端重启后的第一版语义 | 非终态任务显式 `failed/BACKEND_RESTARTED` | 批次 C |
| BE-D07 | 单用户多个 Desktop client 如何选择 | 第一版仅一个活跃 clientInstance | 批次 D |
| BE-D08 | 普通直调遇 Desktop 离线是否可转后台等待 | 第一版快速失败并提示，不自动转任务 | 批次 D |
| BE-D09 | reminder 何时迁入 TaskService | 正式任务中心稳定后进入批次 E | 批次 E |
| BE-D10 | `core/task` 是否重命名 | 先收窄公开命名，迁移稳定后再改路径 | 批次 B 后 |
| BE-D11 | Local Worker 首批能力 | 暂不实现，只保留 typed extension | 独立方案前 |
| BE-D12 | 哪些工具允许自动重试 | 由工具元数据逐个声明，默认不重试副作用工具 | 批次 B |
| BE-D13 | Desktop Runtime 最长等待多久 | 24 小时后以 `RUNTIME_WAIT_TIMEOUT` 收口，后续可提供整体重试 | 批次 D |

---

## 24. 实施前必须再次核对的代码热点

在写实施计划前，应以届时代码重新核对：

- `Module/workflow_engine/workflow_engine.py`：submit/control/query 与清理时机；
- `Module/workflow_engine/core/executor.py`：ToolContext 构造和调用入口；
- `Module/workflow_engine/mailbox.py`：事件类型与终态交付；
- `Module/workflow_engine/checkpoint.py`、`artifact_store.py`：真实持久化能力；
- `Module/workflow_contract`：新增协议后的依赖纯度；
- `core/cognitive/tools.py`、`tool_phase.py`：submit_task 和普通工具帧；
- `core/agent/agent.py`：`_background_tasks` 和来源回投；
- `core/task/scheduler.py`：短调用并发与实际 execute 位置；
- `core/adapter/tool_adapter.py`、`Module/tool/protocol.py`：基础 ToolContext 与克隆；
- `core/desktop/bridge.py`、`core/storage/desktop_call_repo.py`：Desktop 状态、幂等和 TTL；
- `core/input/server.py`、`core/input/ws_handler.py`：正式路由和 WS 事件装配；
- `frontend_contract/rest.py`、`ws_message.py`、`errors.py`：原始契约 SSOT。

原因是本文件是架构讨论稿；后端继续推进后，代码事实可能领先于基线提交。实施者应更新事实说明，而不是盲目照搬旧路径。

---

## 25. 一句话边界

> `TaskService` 回答“这个用户任务现在怎样、能否控制、历史是什么”；`WorkflowEngine` 回答“下一步该执行哪个节点”；`ToolExecutionCoordinator` 回答“这一次工具调用由哪个 Runtime 以什么上下文执行、结果和记录是什么”。

这三个问题相关，但不能由同一个类、同一张表或同一种状态机包办。
