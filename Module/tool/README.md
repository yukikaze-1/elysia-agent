# Module.tool — 可复用工具框架

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

为 AI Agent 提供**声明式、安全、可观测**的工具执行系统。零业务依赖，可独立用于任何 Python Agent 项目。

## 架构

```
ToolPlugin          开发者定义工具（Pydantic 参数 + SafetyProfile 安全声明）
    ↓
ToolManager         插件生命周期、执行调度、Schema 导出、热重载、Debug 接口
    ↓
SafetyPipeline     中间件管道: Pydantic校验 → 安全检查 → HITL审批 → 并发控制 → 超时执行 → Token截断
    ↓
ToolExecutor        CognitiveLoop 依赖的接口抽象（Protocol），解耦认知循环与工具实现
```

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `ToolPlugin` | `protocol.py` | 插件基类 — 定义 `name`、`param_model`、`capabilities`、`safety`、`execute()` |
| `ToolManager` | `manager.py` | 实现 `ToolExecutor` — `load_all()`、`execute()`、`execute_stream()`、`reload_plugin()`、`shutdown()` |
| `SafetyPipeline` | `safety.py` | 执行中间件 — validate → safety → HITL → execute → truncate |
| `SafetyProfile` | `safety.py` | 插件安全声明 — 网络/文件系统/数据库/并发/审批 |
| `Capability` | `capability.py` | 能力枚举 — 取代字符串 `requires`，支持可用性检测 |
| `ToolExecutor` | `protocol.py` | `Protocol` 接口 — CognitiveLoop 只依赖此抽象 |
| `PluginContext` | `protocol.py` | 类型化 DI 容器 — `setup()` 时注入数据库/网络/调度器等服务 |
| `ToolContext` | `protocol.py` | 运行时上下文 — `session_id`、`trace_id`、`agent_id`、`services` |

## 快速开始

```python
from pydantic import BaseModel, Field
from Module.tool import ToolPlugin, ToolManager, ToolContext, SafetyProfile, Capability, PluginContext

# 1. 定义参数
class GreetParams(BaseModel):
    name: str = Field(..., description="要问候的名字")

# 2. 实现插件
class GreetPlugin(ToolPlugin):
    name = "greet"
    description = "向指定用户打招呼"
    param_model = GreetParams
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx: ToolContext, emit=None, params: GreetParams) -> str:
        return f"你好, {params.name}!"

# 3. 加载并执行
async def main():
    tm = ToolManager()
    await tm.load_all([GreetPlugin], PluginContext(session_id="demo"))

    ctx = ToolContext(session_id="demo", trace_id="trace-001")
    result = await tm.execute("greet", ctx=ctx, name="世界")
    print(result.content)  # "你好, 世界!"

    print(tm.definitions())  # OpenAI function calling schema
    await tm.shutdown()
```

## 编写新插件

### 1. 定义 Pydantic 参数模型

```python
from pydantic import BaseModel, Field

class WebSearchParams(BaseModel):
    query: str = Field(..., description="搜索关键词")
    limit: int = Field(default=5, ge=1, le=10, description="结果数量")
```

### 2. 继承 ToolPlugin 并设置元数据

```python
from Module.tool import ToolPlugin, SafetyProfile, Capability

class WebSearchPlugin(ToolPlugin):
    name = "web_search"
    description = "搜索互联网，返回相关网页的标题、URL 和摘要"
    param_model = WebSearchParams
    capabilities = {Capability.NETWORK, Capability.WEB_SEARCH}
    safety = SafetyProfile(
        network_allowed=True,
        block_private_ips=True,
        max_concurrency=3,
    )
```

### 3. 实现 execute()

```python
    async def execute(self, *, ctx: ToolContext, emit=None, params: WebSearchParams) -> str:
        results = await some_search_api(params.query, limit=params.limit)
        return format_results(results)
```

### 4. 注册到 ToolManager

```python
from Module.tool import ToolManager, PluginContext

tm = ToolManager()
await tm.load_all([WebSearchPlugin], PluginContext(session_id="..."))
```

## 安全管道

每次 `execute()` 调用经过 6 个阶段：

```
raw_params → Pydantic 校验 → 安全检查 → HITL 审批 → 信号量获取 → 超时执行 → Token 截断
```

| 阶段 | 失败 → ErrorType | 说明 |
|------|-----------------|------|
| Pydantic 校验 | `INVALID_ARGUMENT` | 精简格式化，≤200 字符，供 LLM 自我修正 |
| 安全检查 | `PERMISSION_DENIED` | URL 内网 IP 过滤、文件路径沙箱、符号链接检测 |
| HITL 审批 | `PERMISSION_DENIED` | `requires_human_approval=True` 时触发，超时/拒绝/过期均拒绝 |
| 信号量 | `RATE_LIMITED` | `max_concurrency > 0` 时限制并发 |
| 超时执行 | `TIMEOUT` | `request_timeout` 总超时；`progress_timeout` 流式无进度超时 |
| Token 截断 | 无错误 | 中英文混合估算，`head_tail` 或 `head_only` 策略 |

## 流式执行

支持工具通过 `execute_stream()` 报告进度：

```python
async def execute_stream(self, *, ctx, emit, params):
    yield ToolProgress(message="下载中", percent=0.45)
    yield ToolProgress(message="下载中", percent=0.90)
    yield ToolResult.ok(name=self.name, content="下载完成")
```

- **背压控制**：`ToolEmitter._queue = asyncio.Queue(maxsize=1024)`
- **流式截断**：到达 token 阈值自动 `aclose()` generator，强制返回截断结果
- **进度超时**：`progress_timeout` 秒内无新 yield 视为卡死

## HITL 审批

高危操作设置 `SafetyProfile.requires_human_approval=True`：

```python
class WriteFilePlugin(ToolPlugin):
    safety = SafetyProfile(
        filesystem_allowed=True,
        requires_human_approval=True,
    )
```

执行时管道暂停，通过 OutputManager 广播审批请求到前端，等待用户确认后继续。

## 并发控制

```python
safety = SafetyProfile(network_allowed=True, max_concurrency=3)
```

`ToolManager` 为该插件创建 `asyncio.Semaphore(3)`，超并发时返回 `RATE_LIMITED`。

## 热重载

```python
await tm.reload_plugin("web_search")  # 修改插件代码后无需重启服务
```

约束：插件必须模块级无状态（所有状态依托于 `PluginContext` 或 `self`）。

## Debug 接口

```python
tm.plugin_list        # [{name, description, capabilities, safety, active}, ...]
tm.executing_now      # ["web_search"]
tm.history            # [{name, success, error, error_type, duration_ms, truncated, result}, ...]
tm.capability_status  # {"network": True, "database:read": True, ...}
tm.pipeline_stats     # {"total": 42, "timeout": 1, "denied": 2, "rate_limited": 0}
```

## 与项目的关系

```
Module/tool/    ← 本模块 — 可复用框架（零业务依赖）
core/tools/     ← Elysia 项目的具体工具插件
```

分层模式与 `Module/llm/`（框架）↔ `core/llm_adapter.py`（项目封装）一致。

## 依赖

- Python 3.11+
- Pydantic v2（参数校验 + Schema 生成）
- 无其他强制依赖（`httpx`、`psutil` 等由插件自行声明）

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [core/tools/](../../core/tools/) | Elysia 项目的具体工具插件 |
| [docs/architecture.md](../../docs/architecture.md) | ToolManager 架构 + 工具列表 |
