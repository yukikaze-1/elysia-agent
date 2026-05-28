# Tool Use 模块

可复用的 AI 工具定义、注册、执行系统。零依赖 core/，遵循 Module/ 的 Protocol-Registry-Backend 模式。

## 快速开始

```python
from Module.tool import tool, ToolRegistry

# 1. 定义工具 — 用 @tool 装饰器
@tool(description="获取当前日期和时间", timeout=5.0)
async def get_time() -> str:
    """返回当前时间字符串。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@tool(description="搜索长期记忆中的相关信息")
async def search_memory(query: str, limit: int = 5) -> str:
    """
    在长期记忆中搜索。

    :param query: 搜索关键词
    :param limit: 返回的最大结果数
    """
    return f"搜索 '{query}' 找到了 {limit} 条结果"

# 2. 注册到 Registry
registry = ToolRegistry()
registry.register_many([
    get_time._tool_definition,
    search_memory._tool_definition,
])

# 3. 导出 Schema（可跨 Provider）
openai_schema = registry.definitions("openai")       # OpenAI function-calling
anthropic_schema = registry.definitions("anthropic") # Anthropic tool use
gemini_schema = registry.definitions("gemini")       # Google Gemini

# 4. 执行工具
result = await registry.execute("search_memory", {"query": "Rust"})
print(result.content)     # "搜索 'Rust' 找到了 5 条结果"
print(result.success)     # True
print(result.duration_ms) # 1.2
```

## 核心 API

### @tool 装饰器

```python
@tool                           # 裸用法，name 自动取函数名
@tool(timeout=10.0)             # 设置超时
@tool(description="...")        # 覆盖描述
@tool(name="custom", ...)       # 覆盖名称
@tool(parameters={...})         # 手动覆盖参数 schema
@tool(progress_timeout=5.0)     # 流式 keep-alive 超时

# 资源声明 + 多模态结果
@tool(
    description="搜索网页并返回结果摘要",
    requires=["web_search", "network"],
    result_type="mixed",        # text | image | audio | file | mixed
    side_effects=False,         # 只读操作
    timeout=15.0,
    progress_timeout=5.0,       # 无进度更新超时 → 取消
)
async def web_search(query: str) -> ToolResult:
    ...
```

- 自动从 type hints (`str`/`int`/`float`/`bool`/`Literal`/`Optional`) 推导 JSON Schema
- 自动从 docstring `:param name: description` 提取参数描述
- 装饰后函数仍可正常调用
- `func._tool_definition` 包含生成的 `ToolDefinition`
- `requires` 声明所需资源（`"web_search"`/`"database:read"`/`"network"`/`"filesystem"` 等）
- `result_type` 声明产出类型，`side_effects` 标记是否有外部副作用

### ToolRegistry

```python
registry = ToolRegistry()

# 注册/卸载
registry.register(definition)       # 单个注册
registry.register_many(defs)        # 批量注册
registry.unregister("tool_name")    # 卸载，返回 True/False
registry.get("tool_name")           # 获取定义

# Schema 导出
registry.definitions()              # OpenAI（默认）
registry.definitions("anthropic")   # Anthropic
registry.definitions("gemini")      # Gemini

# 执行
result = await registry.execute("name", {"arg": "value"})

# 信息
registry.tool_names                 # 工具名列表
registry.history                    # 最近 20 条执行记录
len(registry)                       # 工具数量
"tool_name" in registry             # 是否存在
```

### ToolResult

```python
@dataclass
class ToolResult:
    success: bool           # 是否成功
    content: str            # 结果文本（展示给用户）
    error: str | None       # 失败原因
    duration_ms: float      # 执行耗时（毫秒）
    truncated: bool         # 结果是否被截断
    tool_name: str          # 工具名

    # 多模态结果
    result_type: ResultType # "text" | "image" | "audio" | "file" | "mixed"
    images: list[MediaItem] # 图片列表
    audio: MediaItem | None # 音频数据
    files: list[MediaItem]  # 文件列表

    # 给 LLM 看的文本描述（result_type != "text" 时使用）
    # 例: 图表工具 → llm_summary="营收增长30%" 注入 LLM context
    llm_summary: str

    @property
    def llm_content(self) -> str:
        """返回应注入 LLM messages 的内容。

        success=True:
          result_type="text" → content
          其他类型 → llm_summary (fallback → content)

        success=False (自动组装 LLM 纠错提示):
          INVALID_ARGUMENT → "Action 'X' failed due to invalid arguments.
                             Error: {detail}. Please review...and retry."
          TIMEOUT          → "...timed out...retry with a smaller scope..."
          UNAVAILABLE      → "...unavailable...Do not retry this action..."
          other            → "...failed. Do not retry with the same arguments."
        """
```

**工具返回 ToolResult 的两种方式：**

```python
# 方式 1: 返回 str（向后兼容），由 registry 自动包装
@tool(description="获取时间")
async def get_time() -> str:
    return "2026-05-28 14:30:00"
# → ToolResult(content="2026-05-28 14:30:00", result_type="text")

# 方式 2: 返回 ToolResult（完全控制）
@tool(description="生成图表", result_type="image")
async def chart(data: str) -> ToolResult:
    return ToolResult.ok(
        "chart",
        content="chart.png",
        duration_ms=150.0,
        result_type="image",
        llm_summary="图表显示 Q1 营收增长 30%",
        images=[MediaItem(url="https://x.com/chart.png", mime_type="image/png")],
    )
```

### MediaItem

```python
@dataclass
class MediaItem:
    url: str = ""           # 远程 URL（优先使用）
    data: bytes = b""       # 原始二进制数据（小文件用）
    mime_type: str = ""     # "image/png", "audio/wav", "application/pdf"
    label: str = ""         # 人类可读标签
```

### ToolDefinition / ToolParameter

```python
@dataclass
class ToolDefinition:
    name: str               # 工具名
    description: str        # 工具描述
    parameters: dict[str, ToolParameter]  # 参数定义
    required: list[str]     # 必填参数列表
    func: Callable | None   # 异步可执行函数
    timeout: float = 30.0   # 执行超时（秒）
    max_result_len: int = 2000  # 结果最大字符数

    # 工具属性声明
    result_type: ResultType     # 产出类型（默认 "text"）
    requires: list[str]         # 所需资源/能力声明
    side_effects: bool          # 是否会修改外部状态
```

`requires` 常用值: `"web_search"`, `"database:read"`, `"database:write"`, `"network"`, `"filesystem"`, `"browser"`

运行时（DemoAgent）据此检查后端是否可用，决定工具能否执行。

## 安全机制

`ToolRegistry.execute()` 内置三层防护：

1. **执行超时**: `asyncio.wait_for` 包裹，超时 → `ToolResult(success=False, error="工具执行超时 (Xs)")`
2. **结果截断**: 返回内容超过 `max_result_len` → 截断并标记 `truncated=True`
3. **异常隔离**: 工具内部抛异常 → 捕获并转为 `ToolResult(error=str(e))`，不传播到调用方

`CancelledError` 会重新抛出（让上层 cognitive loop 处理打断逻辑）。
如需全捕获版本，用 `execute_safe()`。

## 异步流式执行 (execute_stream + ToolEmitter)

对于长时间运行的工具，使用 `ToolEmitter` 上报进度，正常 `return` 最终结果：

```python
from Module.tool import tool, ToolResult, ToolRegistry, ToolEmitter

@tool(description="深度扫描系统状态", timeout=120.0)
async def deep_scan(scope: str, emit: ToolEmitter) -> ToolResult:
    """emit.progress() 上报进度 → 正常 return 最终结果。"""
    await emit.progress(0.0, "检查内存中...")
    await emit.progress(0.5, "扫描进程中...")
    await emit.progress(1.0, "分析日志中...")
    return ToolResult.ok("scan", "扫描完成: 无异常", 1500.0)

# 使用
registry = ToolRegistry()
registry.register(deep_scan._tool_definition)

async for event in registry.execute_stream("deep_scan", {"scope": "all"}):
    if isinstance(event, ToolResult):
        print(f"完成: {event.content}")
    else:
        print(f"进度: {event.percent:.0%}")
```

`emit` 参数由 `@tool` 自动检测并对 LLM 隐藏。也兼容旧式的 async generator 工具（`yield ToolProgress` → `yield ToolResult`）。

## 上下文注入 (Context Injection)

工具可以通过 `ctx` 参数获取运行时资源（DB、Session、LLM 等），而不会暴露给 LLM：

```python
from Module.tool import tool, ToolContext, ToolRegistry

@tool(description="在长期记忆中搜索", requires=["database:read"])
async def search_memory(query: str, limit: int = 5, ctx: ToolContext = None) -> ToolResult:
    """搜索长期记忆。"""
    memory = ctx.get("memory")  # LongTermMemory 由 DemoAgent 注入
    results = await memory.search(query, top_k=limit)
    return ToolResult.ok("search_memory", str(results), duration_ms=0)

# LLM 看到的 schema: {"name":"search_memory","parameters":{"query":...,"limit":...}}
# ctx 参数自动对 LLM 隐藏

# 运行时:
ctx = ToolContext(session_id="abc", services={
    "memory": long_term_memory,
    "database": mongo_client,
})
result = await registry.execute("search_memory", {"query": "rust"}, ctx=ctx)
```

`@tool` 装饰器自动检测类型为 `ToolContext` 的参数并将其标记为对 LLM 不可见。
如果 `ctx` 没有默认值，标记为必需（`_ctx_required=True`），`execute()` 时若未提供 ctx 则 Fail Fast。

## 错误分类 (ErrorType)

`ToolResult.error_type` 帮助认知循环决定重试策略：

```python
from Module.tool import ErrorType

class ErrorType(str, Enum):
    INVALID_ARGUMENT  # LLM 传错参 → 反馈后可重试
    TIMEOUT           # 超时 → 可重试
    UNAVAILABLE       # 后端不可用 → 重试无意义
    CANCELLED         # 被打断 → 不重试
    TOOL_ERROR        # 工具内部异常 → 不重试
    UNKNOWN_TOOL      # 工具未注册 → 不重试

# 使用
error_type.is_retryable  # True for INVALID_ARGUMENT and TIMEOUT
```

## 工具进度 (ToolProgress)

`ToolProgress` 是 `emit.progress()` 底层的进度数据模型，也用于 async generator 模式的 `yield`：

```python
from Module.tool import ToolProgress

@dataclass
class ToolProgress:
    status: Literal["pending", "processing", "done", "failed"]
    percent: float | None    # 0.0 ~ 1.0
    message: str
    data: Any
```

正常情况下通过 `ToolEmitter.progress()` 间接使用，不需要手动构造。仅在 async generator 兼容模式下需要 `yield ToolProgress(...)`。

## 复杂嵌套参数: Pydantic 最佳实践

对于嵌套对象或严格枚举约束的参数，推荐用 Pydantic model 替代纯 type hints：

```python
from pydantic import BaseModel, Field
from Module.tool import tool

# Before: type hints — 无法描述深层嵌套和约束
@tool(description="创建订单")
async def create_order(
    items: list[dict],  # LLM 不知道 dict 的键和类型
    address: dict,      # 同样模糊
) -> str: ...

# After: Pydantic — schema 精确，LLM 幻觉显著减少
class OrderItem(BaseModel):
    product_id: str = Field(description="商品 ID")
    quantity: int = Field(default=1, ge=1, le=100, description="数量")
    color: str | None = Field(default=None, description="颜色（可选）")

class ShippingAddress(BaseModel):
    name: str = Field(description="收件人姓名")
    phone: str = Field(description="手机号")
    street: str = Field(description="街道地址")
    city: str = Field(description="城市")
    zipcode: str = Field(description="邮编", pattern=r"^\d{6}$")

@tool(description="创建订单", side_effects=True)
async def create_order(
    items: list[OrderItem],
    address: ShippingAddress,
) -> ToolResult:
    """创建新订单。Pydantic 模型自动生成精确的 JSON Schema。"""
    ...

# 也用 pydantic_to_params() 手动提取
from Module.tool import pydantic_to_params
params, required = pydantic_to_params(ShippingAddress)
```

## Provider Schema 格式

`registry.definitions()` 结果被自动缓存。`register()` / `unregister()` 时缓存失效。
返回深拷贝，调用方修改不影响缓存。并发安全（`threading.Lock`）。

**多 Session 部署建议**：区分"全局静态 Registry"（启动时固化，不改动）和"Session 级动态 Registry"（会话创建/销毁）。避免动态注册触发全局缓存清空影响其他并发请求。

三种输出格式基于标准 JSON Schema，properties 内容一致：

| Provider  | `to_openai()`                       | `to_anthropic()`              | `to_gemini()`                  |
|-----------|-------------------------------------|-------------------------------|--------------------------------|
| 格式      | `{"type":"function","function":{...}}` | `{"name":...,"input_schema":{...}}` | `{"name":...,"parameters":{...}}` |
| Schema 键 | `function.parameters`               | `input_schema`                 | `parameters`                   |

## 从 type hints 推导 Schema

| Python 类型 | JSON Schema type |
|-------------|-----------------|
| `str` | `"string"` |
| `int` | `"integer"` |
| `float` | `"number"` |
| `bool` | `"boolean"` |
| `Literal["a", "b"]` | `"string"` + `enum` |
| `Optional[X]` | `required=False` |
| `list[X]` | `"array"` + `items` |
| 无 type hint | `"string"`（fallback） |

## 从 Pydantic model 推导 Schema

```python
from pydantic import BaseModel, Field

class SearchParams(BaseModel):
    query: str = Field(description="搜索关键词")
    limit: int = Field(default=5, ge=1, le=20)

params, required = pydantic_to_params(SearchParams)
# → ToolParameter 映射 + required 列表
```

## 开发者契约

**资源清理**: 涉及网络 I/O 或外部连接的工具，必须用 `try...finally` 或 `async with` 确保 `CancelledError` 中断时释放资源。**不得关闭由 ToolContext 注入的全局/Session 级长连接**（如数据库连接池），只清理工具内部实例化的局部资源：

```python
@tool(description="流式语音识别", timeout=60.0)
async def transcribe(audio_url: str, emit: ToolEmitter) -> ToolResult:
    client = DeepgramClient()  # 局部资源 — 负责关闭
    try:
        async for chunk in client.stream(audio_url):
            if emit.is_cancelled():  # CPU 密集循环中检查取消
                return ToolResult.fail(...)
            await emit.progress(message=chunk.text)
        return ToolResult.ok("transcribe", full_text, duration_ms)
    finally:
        await client.close()  # CancelledError 时也会执行
```

**并发安全**: `registry.definitions()` 使用 `threading.Lock` 保护缓存。对于纯 CPU 密集的 Schema 生成操作，同步锁不会阻塞事件循环。如有多 Session 动态工具注册场景，建议分离全局静态 Registry 和 Session 级动态 Registry 以避免锁争用。

**多模态结果边界**: `ToolResult` 承载媒体数据（`images`/`audio`/`files`），但注入 LLM 的格式转换由上层消息适配器负责。`registry.execute()` 只保证 `ToolResult` 结构完整，不负责生成 provider-specific 的多模态 payload (base64 image blocks 等)。

**嵌套 Schema**: `pydantic_to_params()` 自动将 Pydantic 的 `$defs`/`$ref` 展开为内联 JSON Schema，确保对 OpenAI/Gemini 接口的兼容性。

## 取消执行

`execute()` 和 `execute_stream()` 都是普通协程，调用方通过 `asyncio.Task.cancel()` 取消：

```python
# 取消单个 execute()
task = asyncio.create_task(registry.execute("slow_tool", {}, ctx=ctx))
# ... 用户点击"停止"
task.cancel()
try:
    result = await task
except asyncio.CancelledError:
    pass  # 工具已中断

# 取消 execute_stream()
async def run_stream():
    async for event in registry.execute_stream("deep_scan", {}):
        handle(event)

task = asyncio.create_task(run_stream())
task.cancel()  # execute_stream 内部 yield CANCELLED ToolResult 后 re-raise
```

取消发生后，`execute()` / `execute_stream()` 会：
1. 向工具协程注入 `CancelledError`
2. 工具内部的 `finally` 块清理局部资源
3. yield/return 一个 `ToolResult(error_type=CANCELLED)` 
4. re-raise `CancelledError` 让调用方感知

## 类型安全的上下文 (Typed ToolContext)

默认 `ctx.get("memory")` 返回 `Any`，IDE 无类型提示。推荐继承 `ToolContext` 获得强类型：

```python
from Module.tool import ToolContext

class MyContext(ToolContext):
    """应用级上下文 — 带类型提示。"""

    @property
    def memory(self) -> LongTermMemory:
        return self.services["memory"]

    @property
    def database(self) -> DocumentDB:
        return self.services["database"]

# 使用
ctx = MyContext(services={"memory": long_term_memory, "database": mongo_client})

@tool(description="搜索记忆", requires=["database:read"])
async def search_memory(query: str, ctx: MyContext) -> ToolResult:
    results = await ctx.memory.search(query)  # IDE 可自动补全
    ...
```

## execute_stream 异常兜底行为

| 场景 | 行为 |
|------|------|
| 工具正常完成 | yield 最终 `ToolResult(success=True)` |
| 工具内部抛异常 (emitter 模式) | yield `ToolResult(success=False, error_type=TOOL_ERROR)` |
| 工具内部抛异常 (async generator 模式) | 同上 |
| 外部 `task.cancel()` | yield `ToolResult(error_type=CANCELLED)` → re-raise `CancelledError` |
| 参数校验失败 | yield `ToolResult(error_type=INVALID_ARGUMENT)` |

**不会发生**: 工具异常导致 `async for` 循环直接崩溃。所有异常都被 Registry 拦截并转换为 `ToolResult`。

## 进度事件的序列化要求

`ToolProgress` 通过 WebSocket 推送到前端时会被 JSON 序列化。
**`message` 必须是 `str`，`data` 必须是可 JSON 序列化的基础类型**（`str`/`int`/`float`/`bool`/`list`/`dict`），不要传入 Python 对象、bytes 或自定义类实例。

```python
# 正确
await emit.progress(0.5, "处理中...", data={"file": "report.pdf", "lines": 1500})

# 错误 — data 包含不可序列化的对象
await emit.progress(0.5, data=some_pandas_dataframe)
```

## 运行测试

```bash
# 单元测试套件 (167 tests)
conda run -n Elysia python -m pytest Module/tool/test_tool.py -v --asyncio-mode=auto

# 集成烟雾测试
conda run -n Elysia python -m Module.tool
```

测试相关文档：

| 文档 | 内容 |
|------|------|
| [unit_test.md](unit_test.md) | 单元测试参考 — 测试结构、运行方式、编写规范、覆盖率 |
| [TDD.md](TDD.md) | TDD 开发过程 — 红-绿-重构循环、设计决策驱动、测试金字塔 |
| [test_plan.md](test_plan.md) | 测试检查清单 — 所有测试用例按 Step 分类的完整列表 |
