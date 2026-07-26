# ToolAdapter — 补齐 Adapter 层（Tool）

> 状态: spec  
> 日期: 2026-07-26  
> 关联: [[2026-07-26-stream-processor-design|StreamProcessor]] · Adapter 层评估

## 背景

### 当前 Adapter 层状态

`core/adapter/` 现有 5 个模块，其中只有 2 个是真正的 Module/ 封装：

| 模块 | 是否 Adapter | 封装对象 |
|------|-------------|---------|
| `llm_adapter.py` | ✅ | `Module/llm` — 重试、热切换、tool-call JSON 解析 |
| `tts_adapter.py` | ✅ | `Module/tts` + `Module/output` — 句级门控、emoji 绑定 |
| `message_builder.py` | ❌ | 纯 schema 操作，无 Module/ 依赖 |
| `tool_call_parser.py` | ❌ | 纯流式解析，无 Module/ 依赖 |
| `schema/adapters/message_adapter.py` | ❌ | 格式转换，无 Module/ 依赖 |

### 评估结论

对 Output、Tool、Database 三个模块评估是否需要补齐 Adapter：

| 模块 | 结论 | 原因 |
|------|------|------|
| **Database** | ✅ 已完成 | `core/storage/` 下 5 个 Repository 类就是 Adapter，SessionMemory/LongTermMemory 完全不碰 `Module/database` |
| **Output** | ❌ 不需要 | `OutputManager` 本身是简单的通道多路复用器，包一层只增加缩进不增加价值。API 缺口（`_broadcast` 私有调用）通过给 `OutputManager` 加公开方法解决 |
| **Tool** | ✅ 需要 | 工具子系统创建/加载/生命周期散落在 `lifecycle.py` 中（~30 行），`ToolExecutor` Protocol 已经定义了运行期接口但创建端没有封装 |

## 目标

创建 `ToolAdapter`，封装工具子系统的创建、插件加载、生命周期管理。同时从 `core/adapter/` 重导出消费者需要的类型，使得 `agent.py`、`lifecycle.py`、`loop.py`、`cognitive/tools.py`、`task_scheduler.py` 不再直接 import `Module/tool/`。

## 非目标

- **不封装插件层** — `core/tools/*/plugin.py` 继续直接 import `Module/tool.protocol`（渐进式：先做消费者层，插件以后再说）
- **不处理 `core/schema/message.py` 的 `ErrorType` 引用** — schema 层对 Module/ 的依赖是独立的结构性问题，不在本次范围
- **不改变 `ToolExecutor` Protocol** — 保持 Module/tool/protocol.py 的接口定义不变

## 设计

### ToolAdapter

`core/adapter/tool_adapter.py`，实现 `ToolExecutor` Protocol，委托给 `ToolManager`。

```python
class ToolAdapter:
    """封装工具子系统：创建、加载、生命周期、类型导出。

    实现 ToolExecutor Protocol，委托给 ToolManager。
    """

    def __init__(
        self,
        plugins: list[type[ToolPlugin]],
        *,
        session_id: str = "",
        user_name: str = "",
        config: AppConfig,
        services: dict[str, Any] | None = None,   # 额外服务注入
        hitl_broadcast: OutputManager | None = None,
    ):
        """创建 ToolManager + PluginContext，不加载插件。"""
        ...

    # ---- 生命周期 ----

    async def start(self) -> None:
        """加载所有插件、设置 HITL broadcast、构建 ToolContext。

        幂等：重复调用 no-op。
        """
        ...

    async def shutdown(self) -> None:
        """关闭所有插件。委托给 ToolManager.shutdown()。"""
        ...

    # ---- 属性 ----

    @property
    def tool_context(self) -> ToolContext:
        """运行时 ToolContext，启动后可用。"""
        ...

    # ---- ToolExecutor Protocol ----

    async def execute(self, name: str, *, ctx: ToolContext, **raw_params) -> ToolResult:
        return await self._manager.execute(name, ctx=ctx, **raw_params)

    async def execute_stream(self, name: str, *, ctx: ToolContext, emit, **raw_params):
        return self._manager.execute_stream(name, ctx=ctx, emit=emit, **raw_params)

    def definitions(self, format: str = "openai") -> list[dict]:
        return self._manager.definitions(format)

    async def cancel_all(self) -> None:
        await self._manager.cancel_all()

    def execute_now(self) -> list[str]:
        return self._manager.execute_now()
```

### 类型重导出

`core/adapter/__init__.py` 新增：

```python
from .tool_adapter import ToolAdapter
from Module.tool.protocol import ToolResult, ErrorType, ToolContext

__all__ = [
    "LLMAdapter", "ThoughtResult",
    "TTSStreamManager", "is_emoji_only",
    "MessageBuilder",
    # Tool adapter + types
    "ToolAdapter", "ToolResult", "ErrorType", "ToolContext",
]
```

### 启动前行为

| 方法 | start() 前行为 |
|------|---------------|
| `definitions()` | 返回空列表（安全，LLM 不会调用工具） |
| `execute()` | 正常执行（ToolManager 支持直接调用，未注册工具返回 unknown_tool 错误） |
| `tool_context` | 返回已构造的 ToolContext（services 已注入，但 `_tool_count` 为 0） |

start() 前不抛异常，和现有行为一致——当前 agent 构造时 ToolManager 也是空的，CognitiveLoop 可以在插件加载前就持有引用。

## 消费者迁移

### 迁移前

```
agent.py __init__:
  self._tool_manager = ToolManager()
  self._plugin_ctx = PluginContext(session_id="", config=self._config)

lifecycle.py _start_tool_context() (~30 行):
  self._tool_ctx = ToolContext(session_id=..., user_id=..., services={...})
  self._cognitive.set_tool_context(self._tool_ctx)
  self._plugin_ctx.session_id = ...
  self._plugin_ctx.memory = ...
  self._plugin_ctx.scheduler = ...
  await self._tool_manager.load_all(ALL_PLUGINS, self._plugin_ctx)
  self._tool_ctx.services["_tool_names"] = list(self._tool_manager.tool_names)
  self._tool_ctx.services["_tool_count"] = len(self._tool_manager._plugins)
  self._tool_manager.set_hitl_broadcast(self._output)
  self._scheduler.set_tool_context(self._tool_ctx)

lifecycle.py _stop_tool_manager():
  await self._tool_manager.shutdown()
```

### 迁移后

```
agent.py __init__:
  self._tool_adapter = ToolAdapter(
      ALL_PLUGINS,
      session_id="",
      user_name=self._user_name,
      config=self._config,
      hitl_broadcast=self._output,
  )
  # tool_executor 参数不变（ToolAdapter 实现 ToolExecutor Protocol）
  CognitiveLoop(..., tool_executor=self._tool_adapter, ...)

lifecycle.py _start_tool_context() (~5 行):
  await self._tool_adapter.start()
  self._cognitive.set_tool_adapter(self._tool_adapter)
  self._scheduler.set_tool_adapter(self._tool_adapter)

lifecycle.py _stop_tool_adapter():
  await self._tool_adapter.shutdown()
```

### 各文件 import 变更

| 文件 | 旧 import | 新 import |
|------|----------|----------|
| `agent.py` | `from Module.tool import ToolManager, ToolContext, PluginContext` | `from core.adapter import ToolAdapter` |
| `lifecycle.py` | `from Module.tool import ToolManager, ToolContext, PluginContext` | *(不需要额外 import，ToolAdapter 已由 agent.py 创建)* |
| `loop.py` | `from Module.tool import ToolContext` | *(不再需要；`set_tool_adapter()` 接收 ToolAdapter，ctx 通过 `adapter.tool_context` 获取)* |
| `cognitive/tools.py` | `from Module.tool.protocol import ToolResult, ErrorType` | `from core.adapter import ToolResult, ErrorType` |
| `task_scheduler.py` | `from Module.tool import ToolContext, ToolResult` / `from Module.tool.protocol import ErrorType` | `from core.adapter import ToolContext, ToolResult, ErrorType` |

### CognitiveLoop 变更

```diff
- ctx: ToolContext | None = None
- self._ctx = ctx
- def set_tool_context(self, ctx): ...
+ def set_tool_adapter(self, adapter):
+     self._tool_adapter = adapter

  # execute 调用处
- await self._tool_executor.execute(name, ctx=self._ctx, **args)
+ await self._tool_executor.execute(name, ctx=self._tool_adapter.tool_context, **args)
```

### TaskScheduler 变更

```diff
- def set_tool_context(self, ctx): ...
+ def set_tool_adapter(self, adapter):
+     self._tool_ctx = adapter.tool_context

  # 内部 use 不变（self._tool_ctx 继续存在）
```

## 边缘情况

### 未启动就调用 execute()

ToolAdapter 构造时 ToolManager 已创建但插件未加载。
- `definitions()` → 返回空列表（安全）
- `execute()` → 正常执行，未注册工具返回 `ToolResult.fail(error="unknown_tool")`

### 重复 start()

幂等。`start()` 设置 `_started` flag，重复调用 no-op。

### shutdown() 失败

`ToolManager.shutdown()` 内部已有容错（逐个插件 teardown，异常不中断）。adapter 层不额外包装。

### ToolContext services 动态字段

`_tool_names`、`_tool_count`、`_fsm_state` 等运行时动态更新字段通过 `adapter.tool_context.services[key] = value` 修改。`tool_context` 属性返回同一个 mutable 对象，修改立即对所有消费者可见。

## 测试策略

### 新测试: `core/adapter/tests/test_tool_adapter.py`

使用 mock `ToolPlugin` 和 `ToolManager`，`@pytest.mark.service`。

覆盖：
- **构造函数** — `definitions()` 在 start 前返回空
- **start()** — 插件正确加载，`tool_context.services` 包含预期 key
- **start() 幂等** — 重复调用无副作用
- **execute()** — 委托给 ToolManager，成功/失败路径
- **definitions()** — start 后返回注册插件的 schema
- **cancel_all()** — 委托
- **execute_now()** — 委托
- **shutdown()** — 调用 ToolManager.shutdown()

### 现有测试回归

```bash
# 完整 cognitive 测试（确认 set_tool_adapter 不影响工具执行）
pytest core/cognitive/tests/ -v

# TaskScheduler 测试
pytest core/tests/test_task_scheduler.py -v

# 全量 phase3 测试（工具管线完整性）
pytest core/tests/test_phase3.py -v -k "tool"

# 快速回归
make test-quick
```

## 文件布局

```
core/adapter/
├── __init__.py           — 重导出 + ToolAdapter, ToolResult, ErrorType, ToolContext
├── llm_adapter.py        — (不变)
├── message_builder.py    — (不变)
├── tool_adapter.py       — NEW: ToolAdapter 类
├── tool_call_parser.py   — (不变)
├── tts_adapter.py        — (不变)
└── tests/
    ├── test_message_builder.py  — (不变)
    └── test_tool_adapter.py     — NEW
```

## 不变的部分

- `Module/tool/` — 所有源文件不变
- `ToolExecutor` Protocol — 接口不变
- `core/tools/*/plugin.py` — 8 个插件目录的 import 不变
- `core/schema/message.py` — `from Module.tool.protocol import ErrorType` 不变
- `core/tests/` — 测试文件 import 暂不迁移
