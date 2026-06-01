# Tool System Deep Refactor — Design Spec

## Context

对当前项目后台工具系统的全面 review 发现了 14 个问题，分布在三个层面：

- **安全/资源**：SSRF（web_fetch 无内网 IP 过滤）、大文件 OOM、符号链接循环无限递归、响应体无大小限制
- **执行管道**：async generator 模式无超时保护、interrupt 时 in-flight 工具未显式取消、调度器无差别重试所有失败
- **功能缺口**：无文件写入、无定时提醒、无运行时配置查看、工具无条件注册（后端不可用仍向 LLM 暴露）

用户确认：深度重构方案（方案 C），不保留向后兼容。后续将引入多 Agent 协作、大量外部 API 工具。

## Target Architecture

```
cognitive_loop → ToolExecutor(Protocol) ← ToolManager → SafetyPipeline → ToolPlugin.execute()
                     (接口抽象)              │                    │
                                       CapabilityChecker   ┌────┼────┼────┐
                                       Plugin Lifecycle    │validate│safety│HITL│execute│postprocess│
                                       Concurrency Gate    │(Pydantic)│(Profile)│(approve)│(timeout)│(token-trunc)│
                                       Hot Reload          └────────┘
                                       Debug Interface
```

### Core Principle: Interface Dependency Inversion

CognitiveLoop 依赖抽象的 `ToolExecutor` Protocol，不直接 import `ToolManager`。CognitiveLoop 未来升级（ReAct → Tree-of-Thought / 多 Agent 协作）时工具层无需改动。

### Layer Separation

```
Module/tool/  ← 可复用框架（零业务依赖，和 Module/llm/、Module/stt/ 同级）
core/tools/   ← Elysia 项目专属插件实现（依赖 memory/llm/scheduler/session 等服务）
```

## Component Design

### 1. ToolPlugin — Base Class

每个工具是一个继承 `ToolPlugin` 的类，放在 `core/tools/` 下。参数通过 Pydantic BaseModel 定义，自动生成 JSON Schema 并完成校验。

```python
class ToolPlugin:
    # 元数据（类属性）
    name: str
    description: str
    param_model: type[BaseModel]       # Pydantic Model，替代 dict[str, ToolParameter]

    # 能力与安全声明（类属性）
    capabilities: set[Capability]
    safety: SafetyProfile

    # 生命周期 — ctx 是类型化的 PluginContext
    async def setup(self, ctx: PluginContext) -> None: ...
    async def teardown(self) -> None: ...

    # 执行 — params 是 Pydantic Model 实例，非裸 dict
    async def execute(self, *, ctx: ToolContext, emit: ToolEmitter | None, params: BaseModel) -> Any: ...
    async def execute_stream(self, *, ctx: ToolContext, emit: ToolEmitter, params: BaseModel) -> AsyncIterator[ToolProgress | ToolResult]: ...
```

与旧 `@tool` 装饰器的区别：
- **Pydantic 参数**：`model.model_json_schema()` 零成本生成 OpenAI/Anthropic/Gemini schema，ValidateArgs 阶段一行 `Model(**raw_params)` 校验
- **有状态**：可持有连接池、缓存
- **有生命周期**：`setup()` 中检测能力，失败则工具不注册
- **安全声明结构化**：`SafetyProfile` 取代字符串 `requires: ["network"]`

**插件开发约束 — 模块级无状态（Stateless at Module Level）**：

所有持久化状态必须依托于 `PluginContext` 或绑定在实例的 `self` 属性上。禁止使用模块级全局变量（module-level globals）或静态类属性缓存状态。此约束确保 `reload_plugin()` 热重载时新旧实例不会发生状态交叉。

### 2. PluginContext — Typed Dependency Injection

取代弱类型的 `services: dict`，插件在 `setup()` 中获得类型安全的依赖：

```python
@dataclass
class PluginContext:
    session_id: str
    # 数据库
    memory: LongTermMemory | None = None
    mongodb: DocumentDB | None = None
    milvus: MilvusClient | None = None
    # 网络
    http_client: httpx.AsyncClient | None = None
    # 调度
    scheduler: TaskScheduler | None = None
    # 配置
    config: AppConfig | None = None
```

### 3. ToolContext — Runtime Execution Context

每次工具调用注入的上下文，支持多 Agent 链路追踪：

```python
@dataclass
class ToolContext:
    session_id: str
    trace_id: str              # 一次请求的全局追踪 ID（跨 Agent、跨工具调用串联）
    agent_id: str = "default"  # 触发该工具的 Agent 实例标识
    services: dict[str, Any]   # 保留弱类型字典用于向后兼容的快速原型
```

`trace_id` 在 CognitiveLoop 开始处理用户输入时生成（UUID4），贯穿工具调用 → LLM 响应 → TTS 的整条链路，写入所有日志和 ToolResult 记录。

**基础设施待办 — trace_id 跨界透传**：当 `WebFetchPlugin` 发起 HTTP 请求或 `MemorySearchPlugin` 查询 Milvus 时，`trace_id` 应通过 `X-Request-Id` 等请求头透传到外部服务。这需要 `PluginContext` 注入的 `http_client` 和数据库客户端在底层配置拦截器自动绑定当前上下文。此改动属于基础设施层（客户端工厂）职责，不纳入本次工具系统重构范围。

### 4. ToolExecutor — Interface Protocol

CognitiveLoop 只持有此接口引用：

```python
class ToolExecutor(Protocol):
    async def execute(self, name: str, *, ctx: ToolContext, **raw_params) -> ToolResult: ...
    async def execute_stream(self, name: str, *, ctx: ToolContext, emit: ToolEmitter, **raw_params) -> AsyncIterator[ToolProgress | ToolResult]: ...
    def definitions(self, format: str = "openai") -> list[dict]: ...
    async def cancel_all(self) -> None: ...
    def execute_now(self) -> list[str]: ...
```

注意：接口接受 `**raw_params`（dict），Pydantic 校验是 ToolManager 内部实现细节，不暴露给调用方。

**v2 预留 — 流式输入（Streaming Input）**：当前 `raw_params` 只能承载静态值。对于需要持续异步数据流的工具（如实时音频转录），v2 将支持 `AsyncIterator` 作为参数类型。Pydantic 无法直接建模 `AsyncIterator`，因此在 `ToolPlugin` 层面通过 `_stream_params: set[str]` 标记哪些参数应从运行时传入的流中取值，而非从 Pydantic Model 校验。接口签名届时扩展为：
```python
async def execute(self, *, ctx: ToolContext, emit: ToolEmitter | None,
                  params: BaseModel, streams: dict[str, AsyncIterator] | None = None) -> Any: ...
```

### 5. ToolManager — Implements ToolExecutor

```python
class ToolManager(ToolExecutor):
    _plugins: dict[str, ToolPlugin]
    _available: dict[Capability, bool]
    _pipeline: SafetyPipeline
    _history: deque[ToolResult]
    _executing: set[str]
    _tasks: dict[str, asyncio.Task]
    _semaphores: dict[str, asyncio.Semaphore]   # 并发控制

    async def load_all(self, plugins: list[type[ToolPlugin]], ctx: PluginContext) -> None: ...
    async def execute(self, name: str, *, ctx: ToolContext, **raw_params) -> ToolResult: ...
    async def execute_stream(self, name: str, *, ctx: ToolContext, emit: ToolEmitter, **raw_params) -> AsyncIterator[ToolProgress | ToolResult]: ...
    def definitions(self, format: str = "openai") -> list[dict]: ...
    async def cancel_all(self) -> None: ...
    async def shutdown(self) -> None: ...

    # Debug 接口
    @property
    def plugin_list(self) -> list[dict]: ...
    @property
    def executing_now(self) -> list[str]: ...
    @property
    def history(self) -> list[ToolResult]: ...
    @property
    def capability_status(self) -> dict[str, bool]: ...
    @property
    def pipeline_stats(self) -> dict: ...
```

关键行为：
- `load_all()` 调每个插件的 `setup(ctx)`，失败则跳过并 log warning，LLM 不可见
- `execute()` 统一走 SafetyPipeline：`raw_params → Pydantic校验 → CheckSafety → 获取Semaphore → execute → token截断 → record`
- `execute_stream()` 走 SafetyPipeline 流式变体：validate + safety（前置）→ `plugin.execute_stream()` yield → 最后 ToolResult → record
- `definitions()` 只导出已激活插件：`param_model.model_json_schema()` 生成 schema，并对 `$defs`/`$ref` 做一次 **Inline Flatten**（将 `$ref` 引用的子 schema 内联展开），确保兼容严格解析的 LLM 厂商
- `cancel_all()` 显式取消所有 in-flight 工具及流式执行
- `shutdown()` 用 `asyncio.gather(*teardowns, return_exceptions=True)` 确保单个卡死不阻塞退出
- `reload_plugin(name)` 热重载：`teardown()` 旧实例 → 重新 import 模块 → `setup(ctx)` 新实例 → 更新 `_plugins` 和 `_semaphores`。不中断正在执行的 `_executing` 任务（旧实例保留到执行完成）
- 并发控制：`SafetyProfile.max_concurrency > 0` 时，实例化 `asyncio.Semaphore(n)`，使用 `async with semaphore:` 上下文管理器确保 `CancelledError` 等异常下绝对释放

### 6. Capability — Enum

取代字符串 `requires`，支持能力可用性检测：

```python
class Capability(Enum):
    DATABASE_READ = "database:read"
    DATABASE_WRITE = "database:write"
    NETWORK = "network"
    WEB_SEARCH = "web_search"
    FILESYSTEM_READ = "filesystem:read"
    FILESYSTEM_WRITE = "filesystem:write"
    PROCESS = "process"
    SCHEDULE = "schedule"
```

### 7. SafetyProfile + SafetyPipeline

#### SafetyProfile (per-plugin declaration)

```python
@dataclass
class SafetyProfile:
    # 网络
    network_allowed: bool = False
    block_private_ips: bool = True
    max_redirects: int = 5
    max_response_bytes: int = 5 * 1024 * 1024
    request_timeout: float = 30.0
    max_concurrency: int = 0           # 0=不限，>0 则用 Semaphore 限制

    # 文件系统
    filesystem_allowed: bool = False
    read_dirs: list[str] = None
    write_dir: str = "data/sandbox/{session_id}"   # 支持 session_id 模板变量
    max_file_bytes: int = 10 * 1024 * 1024
    allow_symlinks: bool = False
    max_dir_depth: int = 3

    # 数据库
    database_allowed: bool = False
    database_readonly: bool = True

    # 结果处理
    max_result_tokens: int = 2000      # Token 估算上限
    truncate_strategy: str = "head_tail"  # "head_tail" | "head_only"

    # 安全审批
    requires_human_approval: bool = False  # 高危操作需用户确认
```

#### SafetyPipeline (execution middleware)

```
raw_params → Pydantic校验 → CheckSafety → HITL审批 → AcquireSemaphore → Execute → TokenTruncate → record
```

HITL 审批（仅 `SafetyProfile.requires_human_approval=True` 时触发）：
1. 管道暂停，生成 `approval_request_id`（UUID4），存入 `{request_id → asyncio.Event}` 内存字典
2. 通过 OutputManager 广播 `{"type": "tool_approval", "tool": name, "params": ..., "request_id": ...}` 到所有 WebSocket 客户端
3. 等待用户在 debug 页或聊天界面确认/拒绝，超时默认拒绝（默认 60s）
4. 确认 → 继续执行；拒绝/超时 → `PERMISSION_DENIED`
5. 审批过程中服务重启 → 前端发回 `request_id` 时内存字典已空，返回 `{"type": "tool_approval_result", "status": "expired"}`，前端显示"审批已过期"
6. 记录审批结果（含确认/拒绝/超时/过期）到 history

流式管道：
```
raw_params → Pydantic校验 → CheckSafety → HITL审批 → AcquireSemaphore → plugin.execute_stream()
                                                                            │
                                                        yield ToolProgress("下载中", 45%)
                                                        yield ToolResult("下载完成")
                                                                            │
                                                                       TokenTruncate → record → ReleaseSemaphore
```

管道各阶段失败对应 ErrorType：
| Stage | ErrorType |
|-------|-----------|
| Pydantic 校验 | `INVALID_ARGUMENT`（精简格式化，见下方） |
| CheckSafety | `PERMISSION_DENIED` |
| HITL 拒绝/超时/过期 | `PERMISSION_DENIED`（附带 `hitl_status: "denied"|"timeout"|"expired"` 元数据） |
| Execute 超时 | `TIMEOUT` |
| Execute 取消 | `CANCELLED`（含 `aclose()` generator 清理） |
| Semaphore 满 | `RATE_LIMITED` |

超时：`request_timeout` 是总超时；`progress_timeout`（流式）是连续无进度超时。

#### Pydantic ValidationError 精简格式化

捕获 Pydantic `ValidationError` 后，不返回冗长的堆栈，而是格式化为一行一句话：

```
# 输入 → 输出
Model(field=...) → "字段 'field'：必填项未提供"
Model(x=-1)      → "字段 'x'：值必须 >= 0"
Model(url="abc") → "字段 'url'：格式不是有效的 URL"
```

LLM 收到的 INVALID_ARGUMENT 内容不超过 200 字符，确保不淹没上下文窗口。

#### Token-Aware Truncation

不做精确 tokenization（不引入 tiktoken 依赖），用估算：英文 ~4 字符/token，中文 ~1 字符/token：

| Strategy | 行为 |
|----------|------|
| `head_tail` | 头部 ~80% tokens + `...[省略N tokens]...` + 尾部 ~20% tokens |
| `head_only` | 头部 ~100% tokens + `...[截断]` |

**流式截断（Stream Truncation）**：`execute_stream` 无法预知尾部数据，因此无论配置何种策略，流式执行**强制降级为 `head_only`**。一旦累计输出的 token 估算量达到 `max_result_tokens`，管道立即调用 `generator.aclose()` 中断底层插件，并强制 yield 一个 `ToolResult(success=True, content=已截断内容, truncated=True)` 作为收尾。中途被截断的 `ToolProgress` 事件不受影响（已发出的继续有效）。

**流式插件开发约束**：`execute_stream` 的 `finally` 块中禁止执行耗时的异步清理（如发送 HTTP 请求、等待数据库写入）。此类清理应在 `teardown()` 中完成。`finally` 块仅做轻量操作（关闭文件句柄、释放锁）。原因是 `aclose()` 在主事件循环中同步等待 finally 完成，重清理会阻塞事件循环。

#### Sandbox 机制

`Path.resolve()` + 白名单 + session 隔离：

```
write_dir: "data/sandbox/{session_id}"
                         │
                    ToolContext.session_id 填充 → "data/sandbox/abc123/"
```

路径解析规则：
| 输入 | 结果 |
|------|------|
| `read_file("notes.txt")` | `{read_dirs[0]}/notes.txt` |
| `read_file("/abs/path")` | 必须在 read_dirs 白名单中 |
| `write_file("out.txt")` | 强制在 `write_dir/{session_id}/` 下 |
| `write_file("../etc/passwd")` | `resolve()` 化解后拒绝 |

额外检查：
- Symbolic link 检测：`allow_symlinks=False` 时逐级检查
- 文件大小预检：`stat.st_size > max_file_bytes` 则拒绝
- 目录深度限制：`max_dir_depth` 防止递归炸弹

#### 背压控制（Backpressure）

`ToolEmitter._queue` 改为 `asyncio.Queue(maxsize=1024)`。当前 `maxsize=1024` 实质为无限（进度事件很小，1024 个积压足够），后续需要严格背压时改小即可。队列满时 `put_nowait` 丢最旧的事件（防止阻塞 producer）。

### 8. ErrorType — Expanded

```python
class ErrorType(Enum):
    INVALID_ARGUMENT = "invalid_argument"   # 可重试（Pydantic 校验错误包含字段路径）
    TIMEOUT = "timeout"                     # 可重试（缩小范围后）
    PERMISSION_DENIED = "permission_denied" # 新增 — 不可重试
    RATE_LIMITED = "rate_limited"           # 新增 — 可重试（退避后）
    NETWORK_ERROR = "network_error"         # 新增 — 可重试（短暂故障）
    UNAVAILABLE = "unavailable"             # 后端不可用
    CANCELLED = "cancelled"
    TOOL_ERROR = "tool_error"               # 工具内部错误，不可重试
    UNKNOWN_TOOL = "unknown_tool"           # 未注册工具 — 错误信息引导 LLM 换工具
```

`UNKNOWN_TOOL` 的 `llm_content` 特殊处理：
> "工具 'xxx' 当前未激活（可能未加载或依赖的后端不可用），请使用其他可用工具完成该任务。"

### 9. TaskScheduler Retry Intelligence

`_execute_tool()` 在重试前检查 `ErrorType.is_retryable`：
- `INVALID_ARGUMENT` → 不重试（参数错误，同样参数必然再失败）
- `TIMEOUT` / `NETWORK_ERROR` / `RATE_LIMITED` → 重试，指数退避 + jitter
- 其余 → 不重试

同时修复：`cancel()` 递归增加 visited set 防循环；`_save_checkpoint` 加 `asyncio.Lock` 防并发写入。

### 10. MemorySavePlugin — Runtime Parameter Filtering

在 `execute()` 中硬性过滤，防止 LLM 将运行时参数（模型名、TTS 开关、session params 等）误写入 L2 长期记忆：

```python
_RUNTIME_KEY_BLACKLIST = {
    "model", "tts_enabled", "stt_enabled", "mood", "mood_intensity",
    "silence_timeout", "proactive_interval", "max_pending_messages",
}

async def execute(self, *, ctx, emit, params: SaveMemoryParams) -> str:
    # 过滤运行时参数（如果 LLM 将其包装在 content 中作为 JSON，尝试解析并清洗）
    ...
```

## Plugin Roster

### Existing Tools (7) → Migrated

| Plugin Class | param_model | Capabilities | Key Safety Rules |
|---|---|---|---|
| `MemorySearchPlugin` | `SearchMemoryParams(query, limit=5)` | `DATABASE_READ` | limit 上限 20, `database_readonly` |
| `MemorySavePlugin` | `SaveMemoryParams(content, type="fact")` | `DATABASE_WRITE` | requires_human_approval: True, type 校验, 运行时参数过滤 |
| `TimePlugin` | `TimeParams(timezone="UTC", format="iso")` | (none) | 默认 ISO 8601 + 时区 |
| `WebSearchPlugin` | `WebSearchParams(query, limit=5)` | `NETWORK, WEB_SEARCH` | limit 上限 10, `block_private_ips`, `max_concurrency: 3` |
| `WebFetchPlugin` | `WebFetchParams(url)` | `NETWORK` | `block_private_ips`, `max_redirects: 5`, `max_response_bytes: 5MB` |
| `ReadFilePlugin` | `ReadFileParams(path, start_line=0, max_lines=200)` | `FILESYSTEM_READ` | `read_dirs: [...]`, `max_file_bytes: 10MB`, `allow_symlinks: false` |
| `ListDirPlugin` | `ListDirParams(path=".", depth=1)` | `FILESYSTEM_READ` | `max_dir_depth: 3`, `allow_symlinks: false` |

### New Tools (4)

| Plugin Class | param_model | Capabilities | Description |
|---|---|---|---|
| `WriteFilePlugin` | `WriteFileParams(path, content)` | `FILESYSTEM_WRITE` | requires_human_approval: True, 在 `write_dir/{session_id}/` 下写文件 |
| `SetReminderPlugin` | `ReminderParams(message, delay_seconds)` | `SCHEDULE` | 依赖 TaskScheduler BACKGROUND 任务 |
| `RuntimeConfigPlugin` | `RuntimeConfigParams(query=None)` | `PROCESS` | 查看模型名、TTS/STT 开关、内存使用 |
| `SelfStatePlugin` | (no params) | (none) | FSM 状态、会话消息数、已加载工具数 |

### Bug Fixes in Migrated Tools

- `search_memory`: 增加 `limit` 上限 (max=20)，加 try/except
- `save_memory`: 校验 `type` 参数，运行时参数过滤
- `read_file`: 改 `f.readlines()` 为逐行读取 + early stop at max_lines
- `list_directory`: 增加 visited set 防符号链接循环
- `web_fetch`: URL 安全检查（内网 IP 拒绝、重定向目标校验、响应体大小限制）
- `web_search`: 按 `max_concurrency` 限制并发
- `get_time`: 增加 `timezone` 和 `format` 参数，默认 ISO 8601 + 时区

## Files Changed

```
Module/tool/  ← 可复用框架
├── __init__.py            (修改: 导出新组件)
├── protocol.py            (修改: ToolPlugin基类, ErrorType扩展, PluginContext, ToolProgress)
├── capability.py          (新增)
├── safety.py              (重写: SafetyProfile, SafetyPipeline, token估算, path/URL安全)
├── manager.py             (新增: 从 registry.py 重写，含 Pydantic校验, 并发控制, 流式)
├── registry.py            (删除: 被 manager.py 替代)
├── decorator.py           (删除: @tool 不再需要)
└── schema.py              (删除: Pydantic 自动生成 schema)

core/tools/  ← Elysia 项目专属插件
├── __init__.py            (修改: register_all → ToolManager.load_all)
├── memory.py              (迁移: MemorySearchPlugin, MemorySavePlugin + 运行时过滤)
├── time.py                (迁移+改进: TimePlugin with Pydantic params)
├── web.py                 (迁移: WebSearchPlugin, WebFetchPlugin + SSRF修复)
├── filesystem.py          (迁移: ReadFilePlugin, ListDirPlugin + symlink修复)
├── write_file.py          (新增)
├── reminder.py            (新增)
├── runtime_config.py      (新增)
└── self_state.py          (新增)

core/
├── cognitive_loop.py      (修改: 依赖 ToolExecutor protocol)
├── task_scheduler.py      (修改: retry 使用 ErrorType.is_retryable, cancel 循环检测, checkpoint 加锁)
├── agent.py               (修改: ToolManager 替代 ToolRegistry, 构造 PluginContext)
└── debug_info.py          (修改: 通过 ToolManager debug 接口)
```

### Summary
- **Module/tool/**: 新增 2 文件, 修改 2 文件, 删除 3 文件
- **core/tools/**: 新增 4 文件, 修改 5 文件
- **core/**: 修改 4 文件

## Verification

| Phase | Content | Command |
|-------|---------|---------|
| Unit | 每个 Plugin 独立测试，mock ctx | `python -m pytest core/tools/ -v` |
| Pipeline | SafetyPipeline validate(Pydantic)/safety/timeout/truncate(token) | `python -m pytest Module/tool/test_safety.py -v` |
| Integration | ToolManager.load_all() → execute()/execute_stream() → record | `python -m pytest Module/tool/test_manager.py -v` |
| Pydantic | param_model → model_json_schema() 各 format | `python -m pytest Module/tool/test_schema.py -v` |
| Concurrency | Semaphore 限流、RATE_LIMITED 触发 | `python -m pytest Module/tool/test_concurrency.py -v` |
| HITL | 审批流程：请求→确认/拒绝/超时→记录 | `python -m pytest Module/tool/test_hitl.py -v` |
| Hot Reload | reload_plugin() 不中断执行中的任务 | `python -m pytest Module/tool/test_reload.py -v` |
| Regression | 所有现有 Module 测试仍通过 | `python -m pytest Module/ -v` |
| E2E | 启动 agent，debug 页面确认工具注册 + 能力状态 + trace_id 链路 | `python app.py` + browser `/debug` |
| Security | SSRF/路径遍历/symlink 攻击用例 | Dedicated test fixtures |
| Manual | 浏览器对话触发各工具调用（含流式进度、HITL 审批弹窗） | Browser `/` |

## TODO: Plugin Authoring Constraints

编写新的 ToolPlugin 需遵循的约束规则（待定义）：

- [ ] **param_model 规范**：字段命名约定（snake_case？），description 必填？default 值策略
- [ ] **参数数量限制**：单个工具最多几个参数？
- [ ] **命名规范**：`ToolPlugin.name` 的格式要求（前缀？分隔符？）
- [ ] **description 规范**：面向 LLM 的工具描述写作指南（含反例）
- [ ] **错误信息规范**：返回给 LLM 的错误信息格式
- [ ] **超时策略**：选 `request_timeout` 还是 `progress_timeout`？默认值建议
- [ ] **安全分级**：何时应设 `requires_human_approval=True`？
- [ ] **already-discovered constraints**：模块级无状态；流式 finally 禁重清理
