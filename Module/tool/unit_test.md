# 单元测试文档 — Module/tool/

## 运行测试

```bash
# 完整测试套件
conda run -n Elysia python -m pytest Module/tool/test_tool.py -v --asyncio-mode=auto

# 单个测试类
conda run -n Elysia python -m pytest Module/tool/test_tool.py::TestToolParameter -v

# 单个测试用例
conda run -n Elysia python -m pytest Module/tool/test_tool.py::TestToolParameter::test_basic_string_param -v

# 集成烟雾测试
conda run -n Elysia python -m Module.tool
```

## 测试文件结构

```
Module/tool/
├── test_tool.py          # 单元测试 + 集成测试（单一文件）
├── test_plan.md          # 测试检查清单（按 Step 组织）
├── TDD.md                # TDD 开发过程文档
└── unit_test.md          # 本文档 — 单元测试参考
```

测试文件 `test_tool.py` 按开发步骤分块，使用注释分隔：

```python
# ===================================================================
# Step 1 — 数据模型
# ===================================================================

class TestToolParameter:       # 10 tests
class TestToolDefinition:      # 7 tests
class TestToolResult:          # 3 tests

# ===================================================================
# Step 2 — 注册中心
# ===================================================================

class TestToolRegistryRegister:     # 7 tests
class TestToolRegistryDefinitions:  # 4 tests
class TestToolRegistryExecute:      # 14 tests

# ... 后续 Steps
```

## 测试类与职责

### 数据模型层

| 测试类 | 数量 | 职责 |
|--------|------|------|
| `TestToolParameter` | 10 | ToolParameter 的数据模型、JSON Schema 导出、嵌套/数组/枚举 |
| `TestToolDefinition` | 7 | ToolDefinition 的三 Provider 格式导出、跨 provider 等价性 |
| `TestToolResult` | 3 | ToolResult.ok() / fail() 工厂方法、截断标记 |

### 安全机制

| 测试类 | 数量 | 职责 |
|--------|------|------|
| `TestValidateArgs` | 12 | validate_args：类型强制（int/float/bool/string）、必填检查、默认值 |
| `TestTruncateResult` | 3 | truncate_result：截断、边界、不截断 |
| `TestExecuteWithTimeout` | 2 | execute_with_timeout：正常返回、超时抛出 |

### 注册中心

| 测试类 | 数量 | 职责 |
|--------|------|------|
| `TestToolRegistryRegister` | 7 | 注册/卸载/批量/覆盖/get/repr |
| `TestToolRegistryDefinitions` | 4 | 三 provider 格式导出、空 registry |
| `TestToolRegistryExecute` | 14 | 执行、超时、异常隔离、历史记录、截断、execute_safe |
| `TestRegistryToolResultPassthrough` | 4 | ToolResult 直通 / str 自动包装 |

### 装饰器 + Schema

| 测试类 | 数量 | 职责 |
|--------|------|------|
| `TestToolDecorator` | 11 | @tool 基本用法、type hints、Literal、Optional、docstring、manual override |
| `TestToolDecoratorExtended` | 5 | requires / side_effects / result_type / 组合 |
| `TestTypeHintsToParams` | 6 | type_hints_to_params：空函数、基本类型、默认值、list、无标注 fallback |
| `TestPydanticToParams` | 5 | pydantic_to_params：简单模型、描述、非 pydantic 错误、嵌套、enum |

### 扩展功能

| 测试类 | 数量 | 职责 |
|--------|------|------|
| `TestMediaItem` | 3 | MediaItem：URL/data/defaults |
| `TestToolResultExtended` | 7 | llm_content 多态、images/audio/files、向后兼容 |
| `TestToolDefinitionExtended` | 2 | result_type/requires/side_effects 默认值 |
| `TestErrorType` | 4 | ErrorType 枚举值、is_retryable |
| `TestToolContext` | 4 | ToolContext.get()、默认值、session_id/user_id |
| `TestToolProgress` | 2 | ToolProgress 字段、默认值 |
| `TestToolResultErrorType` | 3 | ToolResult fail 含 error_type |
| `TestHiddenParamsSchema` | 2 | _hidden_params 不出现在 JSON Schema |
| `TestCtxDecorator` | 4 | @tool 自动检测 ctx 参数、_ctx_required |
| `TestRegistryCtxInjection` | 3 | execute() 注入 ctx |
| `TestRegistryErrorTypes` | 4 | execute() 各错误路径设置正确 error_type |
| `TestSchemaCache` | 4 | 缓存命中/失效/深拷贝/线程安全 |
| `TestCtxFailFast` | 4 | ctx 必需但未提供 → TOOL_ERROR |
| `TestLLMFriendlyErrors` | 5 | _format_error_for_llm() 各类错误消息格式 |
| `TestExecuteStream` | 6 | execute_stream：未注册/无 func/不支持/参数校验/生成器/异常 |
| `TestToolEmitter` | 3 | ToolEmitter.progress()/is_cancelled() |
| `TestExecuteStreamEmitter` | 5 | execute_stream emitter 模式：进度/结果/取消/keep-alive/异常 |

### 集成测试

| 测试类 | 数量 | 职责 |
|--------|------|------|
| `TestIntegration` | 3 | 端到端流程、跨 provider schema、多工具 |

## 测试编写规范

### 命名规范

```
test_<what>_<expected_behavior>
```

示例：
- `test_basic_string_param` — 测试基础 string 参数
- `test_execute_unknown_tool` — 测试执行未知工具
- `test_llm_content_image_type` — 测试 image 类型的 llm_content

### 异步测试

所有涉及 `await` 的测试方法必须加 `@pytest.mark.asyncio`：

```python
@pytest.mark.asyncio
async def test_execute_success(self):
    result = await registry.execute("tool", {})
    assert result.success
```

同步测试（纯数据模型）不需要装饰器：

```python
def test_basic_string_param(self):
    p = ToolParameter(type="string", description="query")
    assert p.to_json_schema_property()["type"] == "string"
```

### 辅助函数

文件级辅助函数放在测试类外部，用于减少重复代码：

```python
def _make_registry_with(func, name=None, **defn_kwargs):
    """快速创建注册了单个工具的 ToolRegistry。"""
    ...

def _make_definition(name="test", **kwargs):
    """快速创建 ToolDefinition。"""
    ...

async def _collect_stream(registry, name, args):
    """收集 execute_stream 的所有事件。"""
    events = []
    async for event in registry.execute_stream(name, args):
        events.append(event)
    return events
```

### 测试工具的定义模式

在测试函数内使用内联装饰器定义工具，保持测试自包含：

```python
@tool(description="add two numbers")
async def add(a: int, b: int) -> str:
    return str(a + b)

registry = ToolRegistry()
registry.register(get_tool_definition(add))
```

### 断言风格

- 优先使用直接 `assert` 而非 `assertEqual`
- 对复杂对象检查关键字段而非全量比对
- 对错误场景检查 `error_type` 而不仅仅是 `success=False`

```python
# 推荐
result = await registry.execute("tool", {})
assert not result.success
assert result.error_type == ErrorType.INVALID_ARGUMENT

# 而非
assert result == ToolResult.fail(...)
```

### Mock 与真实依赖的边界

Module/tool/ 的单元测试必须只验证自身逻辑，不依赖外部服务。涉及以下外部依赖时，使用 Mock 隔离：

| 外部依赖 | Mock 方式 | 示例 |
|---------|----------|------|
| 网络请求（httpx/aiohttp） | `unittest.mock.AsyncMock` 替代 AsyncClient | 工具调用远程 API |
| 文件系统 I/O | `pytest.fixture(tmp_path)` 或 mock `open()` | 工具读写临时文件 |
| 外部数据库（Milvus/MongoDB） | 手写 fake 类注入 `ToolContext.services` | L2 记忆检索 |
| LLM API（OpenAI/Anthropic） | `AsyncMock` 返回固定 ToolResult | 认知循环调用工具 |
| 时间相关（`time.time`/`asyncio.sleep`） | 使用极短的 sleep (0.01s) 而非 mock 时间 | 超时测试 |

**Fake 优于 Mock**: 对于注入 `ToolContext.services` 的服务（如向量数据库），优先手写轻量 fake 类而非使用 `unittest.mock.MagicMock`。Fake 类行为明确、可调试、可复用。

```python
# 推荐: 手写 fake，注入 ToolContext
class FakeMemoryStore:
    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query: str, top_k: int = 5) -> list[str]:
        self.queries.append(query)
        return [f"result-{i}" for i in range(min(top_k, 3))]

store = FakeMemoryStore()
ctx = ToolContext(services={"memory": store})
result = await registry.execute("memory_search", {"query": "rust"}, ctx=ctx)
assert store.queries == ["rust"]  # 验证工具调用了正确的服务方法

# 不推荐: MagicMock 行为不透明
mock_store = MagicMock()
mock_store.search.return_value = ["fake"]
# 无法验证工具是否传了正确的参数给 search()
```

**原则**: 纯逻辑单元测试应在无外网、无数据库的环境下毫秒级跑通。集成烟雾测试（`python -m Module.tool`）覆盖真实依赖。

### 强类型 ToolContext

对于复杂应用，推荐继承 `ToolContext` 获得 IDE 类型提示。测试用例中应覆盖强类型 Context 的注入路径：

```python
from Module.tool import ToolContext, tool, ToolRegistry

class MyToolContext(ToolContext):
    """应用级上下文 — 带类型提示，IDE 可自动补全。"""

    @property
    def memory(self) -> FakeMemoryStore:
        return self.services["memory"]

    @property
    def database(self):
        return self.services.get("database")

# 测试中使用强类型 Context
def test_typed_context():
    store = FakeMemoryStore()
    ctx = MyToolContext(session_id="test", services={"memory": store})

    @tool(description="搜索记忆", requires=["database:read"])
    async def search_memory(query: str, ctx: MyToolContext) -> str:
        results = await ctx.memory.search(query)  # IDE 补全 memory.search()
        return str(results)

    registry = ToolRegistry()
    registry.register(search_memory._tool_definition)

    # 验证 ctx 对 LLM 隐身
    oai = registry.definitions("openai")
    props = oai[0]["function"]["parameters"]["properties"]
    assert "ctx" not in props  # 强类型 ctx 也对 LLM 隐藏
```

## 覆盖率地图

### 按模块维度

| 模块 | 测试数 | 覆盖内容 |
|------|--------|---------|
| `protocol.py` | ~35 | ToolParameter, ToolDefinition, ToolResult, ErrorType, ToolContext, ToolEmitter, ToolProgress, MediaItem |
| `registry.py` | ~55 | register/unregister, definitions cache, execute, execute_safe, execute_stream, history, error_type |
| `decorator.py` | ~25 | @tool 基本用法, type hints, ctx/emit 检测, bare decorator, metadata |
| `safety.py` | ~17 | validate_args, truncate_result, execute_with_timeout |
| `schema.py` | ~12 | type_hints_to_params, pydantic_to_params, constraint extraction |

### 按路径维度

| 路径 | 测试保障 |
|------|---------|
| 正常执行 | `execute()` → `ToolResult.ok` → history |
| 参数错误 | `validate_args()` → `INVALID_ARGUMENT` |
| 工具不存在 | `get() is None` → `UNKNOWN_TOOL` |
| 超时 | `wait_for` → `TIMEOUT` |
| 工具异常 | `except Exception` → `TOOL_ERROR` |
| 取消 | `CancelledError` → `CANCELLED` (yield + re-raise) |
| 外部取消 + 资源清理 | `task.cancel()` → `finally` 释放局部资源 |
| ctx 缺失 | `_ctx_required` → `TOOL_ERROR` |
| ctx 无状态存储 | 注入 fake 服务 → 对 LLM Schema 隐身 |
| 流式进度 | `execute_stream` + emitter queue poll |
| 进度序列化 | `ToolProgress` → `asdict()` → `json.dumps()` |
| 结果截断 | str result over max → truncated=True |
| ToolResult 直通 | `isinstance(raw, ToolResult)` → 原样返回 |
| Pydantic 约束 | `Field(ge/le/pattern)` → JSON Schema `minimum/maximum/pattern` |

## 添加新测试

### 新功能的标准流程

1. 在 `test_tool.py` 末尾创建新的测试类
2. 遵循 `Test<FeatureName>` 命名
3. 添加类级 docstring 说明测试目标
4. 先写失败路径测试，再写成功路径测试

```python
class TestNewFeature:
    """新功能单元测试。"""

    def test_basic_behavior(self):
        """基础行为测试。"""
        ...

    @pytest.mark.asyncio
    async def test_async_behavior(self):
        """异步行为测试。"""
        ...

    def test_edge_case_empty_input(self):
        """边界：空输入。"""
        ...

    def test_error_path(self):
        """错误路径。"""
        ...
```

### 不应测试的内容

- Python 标准库行为（如 dataclass 默认值）
- 第三方库行为（如 pytest 的 mark 装饰器）
- 纯样板代码（如 `__init__.py` 的 re-export）
- 日志输出格式

## 常见问题

### 测试超时

```bash
# 某些测试涉及真实 asyncio.sleep(0.5)，使用 longer timeout
pytest Module/tool/test_tool.py -v --timeout=30
```

### 选择性运行

```bash
# 只运行 emitter 相关测试
pytest Module/tool/test_tool.py -v -k "Emitter"

# 只运行 error_type 相关测试
pytest Module/tool/test_tool.py -v -k "ErrorType"

# 跳过集成测试
pytest Module/tool/test_tool.py -v -k "not Integration"
```

### 查看测试覆盖率

```bash
conda run -n Elysia python -m pytest Module/tool/test_tool.py \
  --cov=Module.tool --cov-report=term-missing \
  --cov-report=html:htmlcov/tool \
  --asyncio-mode=auto
```
