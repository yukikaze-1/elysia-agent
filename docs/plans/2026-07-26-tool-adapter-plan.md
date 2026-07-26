# ToolAdapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `ToolAdapter` to encapsulate tool subsystem lifecycle, and migrate 5 consumer files to use `core.adapter` instead of `Module.tool`.

**Architecture:** ToolAdapter implements `ToolExecutor` Protocol by delegating to `ToolManager`. It owns `ToolManager` + `PluginContext` creation, `load_all()` + `set_hitl_broadcast()` wiring, and `shutdown()`. Consumers get a single object instead of managing `ToolManager`, `PluginContext`, `ToolContext` separately.

**Tech Stack:** Python 3.11+, asyncio, pytest + pytest-asyncio

## Global Constraints

- ToolAdapter 实现 `ToolExecutor` Protocol（`Module/tool/protocol.py`），不修改 Protocol 接口
- 测试文件 import 不迁移（继续 `from Module.tool import ...`），但属性访问路径需更新（`agent._tool_manager` → `agent._tool_adapter`）
- `core/tools/*/plugin.py`（8 个插件目录）的 import 不修改
- `core/adapter/__init__.py` 重导出 `ToolAdapter`, `ToolResult`, `ErrorType`, `ToolContext`
- 测试使用 `@pytest.mark.service` marker
- `start()` 幂等：重复调用 no-op
- `definitions()` 在 `start()` 前返回空列表
- `tool_context` 属性返回同一个 mutable 对象（services 动态更新可见）
- 所有现有测试必须通过（`make test-quick` + `test_phase2.py` + `test_phase3.py` + `test_task_scheduler.py`）

---

### Task 1: ToolAdapter core class + tests (TDD)

**Files:**
- Create: `core/adapter/tool_adapter.py`
- Create: `core/adapter/tests/test_tool_adapter.py`

**Interfaces:**
- Produces: `ToolAdapter(plugins, *, session_id, user_name, config, services, hitl_broadcast)` — constructor + `start(extra_services=None)`, `shutdown()`, `tool_context` property, `execute()`, `execute_stream()`, `definitions()`, `cancel_all()`, `execute_now()`

- [ ] **Step 1: Write failing tests**

Create `core/adapter/tests/test_tool_adapter.py`:

```python
"""ToolAdapter 测试。"""
from __future__ import annotations

import pytest

from Module.tool.protocol import ToolResult, ToolContext, ToolPlugin
from Module.tool.safety import SafetyProfile


class _MockPlugin(ToolPlugin):
    """测试用插件 — 注册后 execute() 返回 ok。"""
    name = "mock_tool"
    description = "A mock tool for testing"
    safety = SafetyProfile(level="safe")

    async def execute(self, *, ctx, params=None):
        return ToolResult.ok(name=self.name, content="mock result", duration_ms=1.0)


# ---------------------------------------------------------------------------
# 构造函数
# ---------------------------------------------------------------------------

class TestConstruction:
    """ToolAdapter 构造和启动前行为。"""

    @pytest.mark.service
    def test_definitions_empty_before_start(self):
        """start() 前 definitions() 返回空列表。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        assert adapter.definitions() == []

    @pytest.mark.service
    def test_tool_context_available_before_start(self):
        """start() 前 tool_context 属性可用，services 已注入。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(
            plugins=[_MockPlugin],
            session_id="test-sid",
            user_name="tester",
        )
        ctx = adapter.tool_context
        assert isinstance(ctx, ToolContext)
        assert ctx.session_id == "test-sid"
        assert ctx.user_id == "tester"


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestStart:
    """start() 行为。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_start_loads_plugins(self):
        """start() 后 definitions() 返回已注册工具的 schema。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        defs = adapter.definitions()
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "mock_tool"
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        """重复 start() 无副作用 — definitions() 不会翻倍。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        await adapter.start()
        defs = adapter.definitions()
        assert len(defs) == 1
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_start_with_extra_services(self):
        """start(extra_services={...}) 合并到 tool_context.services。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(
            plugins=[_MockPlugin],
            session_id="sid-1",
            user_name="test-user",
            services={"memory": "mock_memory", "scheduler": "mock_scheduler"},
        )
        await adapter.start(extra_services={
            "session": "mock_session",
            "_started_at": 1234567890.0,
        })
        ctx = adapter.tool_context
        assert ctx.services.get("memory") == "mock_memory"
        assert ctx.services.get("scheduler") == "mock_scheduler"
        assert ctx.services.get("session") == "mock_session"
        assert ctx.services.get("_started_at") == 1234567890.0
        # ToolAdapter 自动注入的 key
        assert "_tool_manager" in ctx.services
        assert "_tool_names" in ctx.services
        assert "_tool_count" in ctx.services
        await adapter.shutdown()


# ---------------------------------------------------------------------------
# execute()
# ---------------------------------------------------------------------------

class TestExecute:
    """execute() 委托给 ToolManager。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_execute_delegates_to_manager(self):
        """execute() 调用 ToolManager.execute() 并返回 ToolResult。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        result = await adapter.execute(
            name="mock_tool",
            ctx=adapter.tool_context,
        )
        assert result.success is True
        assert result.content == "mock result"
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        """未注册工具返回失败结果。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        result = await adapter.execute(
            name="nonexistent",
            ctx=adapter.tool_context,
        )
        assert result.success is False
        await adapter.shutdown()


# ---------------------------------------------------------------------------
# 其他 ToolExecutor 方法
# ---------------------------------------------------------------------------

class TestToolExecutorMethods:
    """definitions(), cancel_all(), execute_now()。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_definitions_returns_openai_format(self):
        """definitions() 返回 OpenAI function-calling 格式。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        defs = adapter.definitions(format="openai")
        assert isinstance(defs, list)
        assert defs[0]["type"] == "function"
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_cancel_all_does_not_raise(self):
        """cancel_all() 无异常。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        await adapter.cancel_all()
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_execute_now_returns_list(self):
        """execute_now() 返回工具名列表。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        names = adapter.execute_now()
        assert isinstance(names, list)
        await adapter.shutdown()


# ---------------------------------------------------------------------------
# shutdown()
# ---------------------------------------------------------------------------

class TestShutdown:
    """shutdown() 行为。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_shutdown_does_not_raise(self):
        """shutdown() 不抛异常，二次调用安全。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        await adapter.shutdown()
        await adapter.shutdown()  # 二次调用不抛异常
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest core/adapter/tests/test_tool_adapter.py -v`
Expected: FAIL — "No module named 'core.adapter.tool_adapter'"

- [ ] **Step 3: Write ToolAdapter implementation**

Create `core/adapter/tool_adapter.py`:

```python
"""ToolAdapter — 封装工具子系统的创建、加载、生命周期管理。

实现 ToolExecutor Protocol，委托给 ToolManager。
消费者通过此适配器获取 ToolExecutor + ToolContext，
不再直接依赖 Module/tool 的类型。
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from Module.tool import ToolManager, PluginContext, ToolContext
from Module.tool.protocol import ToolResult, ErrorType  # noqa: F401 — 由 __init__.py 重导出

if TYPE_CHECKING:
    from ..app_config import AppConfig
    from Module.output import OutputManager

logger = logging.getLogger(__name__)


class ToolAdapter:
    """封装工具子系统：创建、加载、生命周期、类型导出。

    实现 ToolExecutor Protocol，委托给 ToolManager。

    用法:
        adapter = ToolAdapter(ALL_PLUGINS, session_id="...", user_name="...", config=...)
        await adapter.start(extra_services={...})
        # ... 使用 adapter.execute() / adapter.definitions() ...
        await adapter.shutdown()
    """

    def __init__(
        self,
        plugins: list[type],
        *,
        session_id: str = "",
        user_name: str = "",
        config: "AppConfig | None" = None,
        services: dict[str, Any] | None = None,
        hitl_broadcast: "OutputManager | None" = None,
    ):
        self._plugins = plugins
        self._session_id = session_id
        self._user_name = user_name
        self._config = config
        self._services = services or {}
        self._hitl_broadcast = hitl_broadcast
        self._started = False

        # 创建 ToolManager（构造时不加载插件）
        self._manager = ToolManager()

        # 创建 PluginContext（session_id 在 start() 中更新）
        self._plugin_ctx = PluginContext(
            session_id=session_id,
            config=config,
        )

        # 构建 ToolContext（services 在 start() 中填充）
        self._tool_ctx = ToolContext(
            session_id=session_id,
            user_id=user_name,
            services=dict(self._services),
        )

    # ---- 生命周期 ----

    async def start(self, extra_services: dict[str, Any] | None = None) -> None:
        """加载所有插件、设置 HITL broadcast、构建 ToolContext。

        extra_services 合并到 tool_context.services（运行时依赖注入）。
        幂等：重复调用 no-op。
        """
        if self._started:
            return

        # 合并运行时服务
        if extra_services:
            self._tool_ctx.services.update(extra_services)

        # 更新 PluginContext
        self._plugin_ctx.session_id = self._session_id
        self._plugin_ctx.memory = self._tool_ctx.services.get("memory")
        self._plugin_ctx.scheduler = self._tool_ctx.services.get("scheduler")

        # 加载插件
        await self._manager.load_all(self._plugins, self._plugin_ctx)

        # 注入 ToolAdapter 管理的服务
        self._tool_ctx.services["_tool_manager"] = self._manager
        self._tool_ctx.services["_tool_names"] = list(self._manager.tool_names)
        self._tool_ctx.services["_tool_count"] = len(self._manager._plugins)

        # 设置 HITL broadcast
        if self._hitl_broadcast is not None:
            self._manager.set_hitl_broadcast(self._hitl_broadcast)

        self._started = True
        logger.info("ToolAdapter: %d 个工具已加载", len(self._manager._plugins))

    async def shutdown(self) -> None:
        """关闭所有插件。委托给 ToolManager.shutdown()。"""
        await self._manager.shutdown()

    # ---- 属性 ----

    @property
    def tool_context(self) -> ToolContext:
        """运行时 ToolContext。

        返回同一个 mutable 对象，services 的动态更新
        （_tool_names、_fsm_state 等）对所有消费者立即可见。
        """
        return self._tool_ctx

    # ---- ToolExecutor Protocol ----

    async def execute(self, name: str, *, ctx: ToolContext, **raw_params: Any) -> ToolResult:
        """执行工具。委托给 ToolManager.execute()。"""
        return await self._manager.execute(name, ctx=ctx, **raw_params)

    async def execute_stream(self, name: str, *, ctx: ToolContext, emit: Any, **raw_params: Any):
        """流式执行工具。委托给 ToolManager.execute_stream()。"""
        async for event in self._manager.execute_stream(name, ctx=ctx, emit=emit, **raw_params):
            yield event

    def definitions(self, format: str = "openai") -> list[dict]:
        """返回已注册工具的 LLM function schema 列表。"""
        return self._manager.definitions(format)

    async def cancel_all(self) -> None:
        """打断所有运行中的工具。"""
        await self._manager.cancel_all()

    def execute_now(self) -> list[str]:
        """返回可立即执行的工具名列表。"""
        return self._manager.execute_now()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest core/adapter/tests/test_tool_adapter.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add core/adapter/tool_adapter.py core/adapter/tests/test_tool_adapter.py
git commit -m "feat: add ToolAdapter — encapsulate tool subsystem lifecycle"
```

---

### Task 2: Migrate consumers to ToolAdapter

**Files:**
- Modify: `core/agent/agent.py`
- Modify: `core/agent/lifecycle.py`
- Modify: `core/cognitive/loop.py`
- Modify: `core/cognitive/tools.py`
- Modify: `core/task_scheduler.py`
- Modify: `core/tests/test_phase2.py`
- Modify: `core/tests/test_phase3.py`

**Interfaces:**
- Consumes: `ToolAdapter(plugins, *, session_id, user_name, config, services, hitl_broadcast)` from Task 1
- Consumes: `ToolAdapter.start(extra_services=None)` from Task 1
- Produces: `CognitiveLoop.set_tool_adapter(adapter)`, `TaskScheduler.set_tool_adapter(adapter)`

- [ ] **Step 1: Migrate agent.py**

Changes to `/home/yomu/Agent/core/agent/agent.py`:

**Import (line 39):**
```diff
- from Module.tool import ToolManager, ToolContext, PluginContext
+ from core.adapter import ToolAdapter
```

**`__init__` — 创建端 (lines 99-106):**
```diff
- # ---- L2: Tools ----
- self._tool_manager = ToolManager()
- 
- # 构造 PluginContext（session_id 在 start() 中注入）
- self._plugin_ctx = PluginContext(
-     session_id="",  # 在 start() 中更新
-     config=self._config,
- )
+ # ---- L2: Tools ----
+ self._tool_adapter = ToolAdapter(
+     ALL_PLUGINS,
+     session_id="",
+     user_name=self._user_name,
+     config=self._config,
+     hitl_broadcast=self._output,
+ )
```

**CognitiveLoop 构造 (line 166):**
```diff
- tool_executor=self._tool_manager,
+ tool_executor=self._tool_adapter,
```

**TaskScheduler 构造 (lines 181-182):**
```diff
- tool_executor=self._tool_manager,
- tool_ctx=None,  # 在 start() 中赋值 tool_ctx 后更新
+ tool_executor=self._tool_adapter,
+ tool_ctx=None,  # 在 start() 中通过 set_tool_adapter() 更新
```

- [ ] **Step 2: Migrate lifecycle.py**

Changes to `/home/yomu/Agent/core/agent/lifecycle.py`:

**Import (line 22):**
```diff
- from Module.tool import ToolManager, ToolContext, PluginContext
```

删除该行。

**Import (line 23):**
```diff
- from core.tools import ALL_PLUGINS
```

删除该行（load_all 现在在 ToolAdapter.start() 内部调用）。

**TYPE_CHECKING 类型声明 (lines 78-79, 84):**
```diff
- _tool_manager: ToolManager
- _plugin_ctx: PluginContext
```

删除这两行。另删除 line 84：

```diff
- _tool_ctx: ToolContext
```

迁移后 `_tool_ctx` 不再是直接实例变量——统一通过 `self._tool_adapter.tool_context` 访问。

**`_start_tool_context()` (lines 216-246) — 完整替换：**
```python
async def _start_tool_context(self) -> None:
    """启动 ToolAdapter，注入运行时服务。"""
    await self._tool_adapter.start(extra_services={
        "memory": self._long_term,
        "scheduler": self._scheduler,
        "config": self._config,
        "session": self._session,
        "_started_at": self._started_at,
        "_token_tracker": self._token_tracker,
        "_fsm_state": str(self._fsm.state),
        "_session_msg_count": len(self._session.messages),
    })
    self._cognitive.set_tool_adapter(self._tool_adapter)
    self._scheduler.set_tool_adapter(self._tool_adapter)
```

**`_stop_tool_manager()` at line 482 — 重命名：**
```python
async def _stop_tool_adapter(self) -> None:
    """关闭 ToolAdapter（清理所有工具插件）。"""
    logger.debug("Stopping ToolAdapter...")
    await self._tool_adapter.shutdown()
```

**调用处 line 366:**
```diff
- await self._stop_tool_manager()
+ await self._stop_tool_adapter()
```

**`_start_task_system()` — 两处引用更新 (lines 292, 311):**
```diff
- tool_registry=self._tool_manager,
+ tool_registry=self._tool_adapter,
```

```diff
- self._tool_ctx.services["_task_system"] = self._task_system
+ self._tool_adapter.tool_context.services["_task_system"] = self._task_system
```

> **注意**: `ToolManager` 有 `submit_approval()` 方法（`Module/tool/manager.py:232`）但未在 `ToolExecutor` Protocol 中定义。`Module/task_system/lifecycle.py:173` 通过 `hasattr(tool_registry, "submit_approval")` 守卫。迁移后 `ToolAdapter` 不暴露此方法，HITL 审批（通过 TaskSystem 通道）将静默跳过。v1 的 `HITLNotifier` 本身已声明"审批由 ToolManager SafetyPipeline 直接处理，不经过此通道"，因此无功能影响。

- [ ] **Step 3: Migrate loop.py**

Changes to `/home/yomu/Agent/core/cognitive/loop.py`:

**Import (line 30):**
```diff
- from Module.tool import ToolContext
```

删除该行。

**`__init__` 参数 (line 107):**
```diff
- ctx: ToolContext | None = None,
```

删除该行。

**`__init__` 赋值 (line 123):**
```diff
- self._ctx = ctx
```

删除该行。

**`set_tool_context()` (lines 157-159):**
```diff
- def set_tool_context(self, ctx) -> None:
-     """设置当前 turn 的工具上下文。"""
-     self._ctx = ctx
+ def set_tool_adapter(self, adapter) -> None:
+     """设置工具适配器 — 提供 ToolExecutor 实现 + ToolContext。"""
+     self._tool_adapter = adapter
```

**trace_id 赋值 (lines 225-226):**
```diff
- if self._ctx is not None:
-     self._ctx.trace_id = new_message_id()
+ if self._tool_adapter is not None:
+     self._tool_adapter.tool_context.trace_id = new_message_id()
```

- [ ] **Step 4: Migrate cognitive/tools.py**

Changes to `/home/yomu/Agent/core/cognitive/tools.py`:

**Import (line 10):**
```diff
- from Module.tool.protocol import ToolResult, ErrorType
+ from core.adapter import ToolResult, ErrorType
```

**属性声明 (line 33):**
```diff
- _ctx: Any = None
+ _tool_adapter: Any = None   # type: ignore[assignment]
```

**execute 调用 (line 56):**
```diff
- tr = await self._tool_executor.execute(func_name, ctx=self._ctx, **args)
+ tr = await self._tool_executor.execute(func_name, ctx=self._tool_adapter.tool_context, **args)
```

(`self._ctx` 仅在 line 56 一处使用 — 已确认只此一处)

- [ ] **Step 5: Migrate task_scheduler.py**

Changes to `/home/yomu/Agent/core/task_scheduler.py`:

**Import (lines 17-18):**
```diff
- from Module.tool import ToolContext, ToolResult
- from Module.tool.protocol import ErrorType
+ from core.adapter import ToolContext, ToolResult, ErrorType
```

**`set_tool_context()` (lines 54-56):**
```diff
- def set_tool_context(self, ctx) -> None:
-     """更新工具上下文（用于 Agent.start() 中延迟注入）。"""
-     self._tool_ctx = ctx
+ def set_tool_adapter(self, adapter) -> None:
+     """设置工具适配器（用于 Agent.start() 中延迟注入 ToolContext）。"""
+     self._tool_ctx = adapter.tool_context
```

- [ ] **Step 6: Fix test files — `test_phase2.py` + `test_phase3.py`**

Changes to `/home/yomu/Agent/core/tests/test_phase2.py` (6 处引用):

**Line 247 — `test_search_memory_tool` 中 execute 调用:**
```diff
- result = await agent._tool_manager.execute("search_memory", ctx=agent._tool_ctx, query="爬山")
+ result = await agent._tool_adapter.execute("search_memory", ctx=agent._tool_adapter.tool_context, query="爬山")
```

**Line 265 — 保存原始 `_run_hitl`:**
```diff
- original_run_hitl = agent._tool_manager._pipeline._run_hitl
+ original_run_hitl = agent._tool_adapter._manager._pipeline._run_hitl
```

**Line 268 — 设置 `_run_hitl` mock:**
```diff
- agent._tool_manager._pipeline._run_hitl = _auto_approve
+ agent._tool_adapter._manager._pipeline._run_hitl = _auto_approve
```

**Line 270 — `test_save_memory_tool` 中 execute 调用:**
```diff
- await agent._tool_manager.execute("save_memory", ctx=agent._tool_ctx, content="测试保存的记忆", type="fact")
+ await agent._tool_adapter.execute("save_memory", ctx=agent._tool_adapter.tool_context, content="测试保存的记忆", type="fact")
```

**Line 291 — `test_end_to_end_with_memory` 中 HITL mock:**
```diff
- agent._tool_manager._pipeline._run_hitl = _auto_approve
+ agent._tool_adapter._manager._pipeline._run_hitl = _auto_approve
```

**Line 321 — `test_session_state_persists_in_memory` 中 HITL mock:**
```diff
- agent._tool_manager._pipeline._run_hitl = _auto_approve
+ agent._tool_adapter._manager._pipeline._run_hitl = _auto_approve
```

Changes to `/home/yomu/Agent/core/tests/test_phase3.py`:

**Lines 1072-1073 — `agent._tool_manager.load_all(...)` + `agent._tool_manager.tool_names`:**
```diff
- await agent._tool_manager.load_all(ALL_PLUGINS, PluginContext(session_id="test"))
- names = agent._tool_manager.tool_names
+ await agent._tool_adapter.start()
+ names = list(agent._tool_adapter._manager.tool_names)
```

**Lines 1090-1097 — 同上模式 + `agent._tool_manager.execute(...)`:**
```diff
- await agent._tool_manager.load_all(ALL_PLUGINS, PluginContext(session_id="test"))
+ await agent._tool_adapter.start()
  # ... seed memory entry ...
  ctx = ToolContext(services={"memory": agent._long_term})
- result = await agent._tool_manager.execute("search_memory", ctx=ctx, query="测试")
+ result = await agent._tool_adapter.execute("search_memory", ctx=ctx, query="测试")
```
(WARNING: 此处 ctx 是本地构造的 `ToolContext`，不经过 `agent._tool_adapter.tool_context`——测试独立场景，保持原样)

- [ ] **Step 7: Run test regression**

```bash
# ToolAdapter 测试
pytest core/adapter/tests/test_tool_adapter.py -v

# Cognitive 测试
pytest core/cognitive/tests/ -v

# TaskScheduler 测试
pytest core/tests/test_task_scheduler.py -v

# 受影响的集成测试
pytest core/tests/test_phase2.py -v
pytest core/tests/test_phase3.py -v

# 快速回归
make test-quick
```

Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add core/agent/agent.py core/agent/lifecycle.py core/cognitive/loop.py core/cognitive/tools.py core/task_scheduler.py core/tests/test_phase2.py core/tests/test_phase3.py
git commit -m "refactor: migrate consumers from Module.tool to ToolAdapter"
```

---

### Task 3: Update exports and documentation

**Files:**
- Modify: `core/adapter/__init__.py`
- Modify: `core/README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `ToolAdapter`, `ToolResult`, `ErrorType`, `ToolContext` from Task 1
- Produces: Public exports from `core.adapter`

- [ ] **Step 1: Update `core/adapter/__init__.py`**

```python
"""适配器层 — 封装 Module/ 后端，为 core/ 层提供统一接口。

LLM: LLMAdapter + tool_call 解析（重试、热切换、streaming）
TTS: TTSStreamManager（句队列、emoji 同步、门控播放）
Tool: ToolAdapter（工具子系统生命周期）+ ToolResult / ErrorType / ToolContext 重导出
Message: MessageBuilder（系统 prompt + 历史 → LLM API messages[]）
"""

from .llm_adapter import LLMAdapter, ThoughtResult
from .tts_adapter import TTSStreamManager, is_emoji_only
from .message_builder import MessageBuilder
from .tool_adapter import ToolAdapter
from Module.tool.protocol import ToolResult, ErrorType, ToolContext

__all__ = [
    "LLMAdapter",
    "ThoughtResult",
    "TTSStreamManager",
    "is_emoji_only",
    "MessageBuilder",
    "ToolAdapter",
    "ToolResult",
    "ErrorType",
    "ToolContext",
]
```

- [ ] **Step 2: Verify imports work**

Run: `python -c "from core.adapter import ToolAdapter, ToolResult, ErrorType, ToolContext; print('OK')"`
Expected: OK (no ImportError)

- [ ] **Step 3: Update `core/README.md`**

**Support module table — adapter row (around line 134):**
```diff
- | `adapter/` | 适配器层：`LLMAdapter` 封装 `Module.llm` 后端，提供 `stream_think()` 流式接口，自动重试（429/500/502/503/504，指数退避 3 次）。`TTSStreamManager` 管理 TTS 句队列 + emoji 同步 + 门控播放。`tool_call_parser` 流式 tool_call 合并/归一化。详见 [`adapter/`](adapter/) |
+ | `adapter/` | 适配器层：`LLMAdapter` 封装 `Module.llm` 后端，提供 `stream_think()` 流式接口，自动重试（429/500/502/503/504，指数退避 3 次）。`TTSStreamManager` 管理 TTS 句队列 + emoji 同步 + 门控播放。`ToolAdapter` 封装工具子系统生命周期（创建+加载+关闭）。`tool_call_parser` 流式 tool_call 合并/归一化。详见 [`adapter/`](adapter/) |
```

**Sub-package table — adapter row (around line 152):**
```diff
- | `core/adapter/` | 适配器层：LLMAdapter + TTSStreamManager + tool_call_parser，封装 Module/ 后端供 core/ 层使用 | [adapter/README.md](adapter/README.md) |
+ | `core/adapter/` | 适配器层：LLMAdapter + TTSStreamManager + ToolAdapter + tool_call_parser，封装 Module/ 后端供 core/ 层使用 | [adapter/README.md](adapter/README.md) |
```

- [ ] **Step 4: Update `CLAUDE.md`**

In the `core/` architecture tree, add after `tool_call_parser.py`:
```diff
  │   ├── tool_call_parser.py #  merge/normalize streaming tool_call deltas
+ │   ├── tool_adapter.py   #   ToolAdapter: tool subsystem lifecycle + ToolExecutor delegation
  │   ├── message_builder.py #   MessageBuilder: system prompt + history → LLM API messages[]
```

- [ ] **Step 5: Run full regression**

```bash
make test-quick
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add core/adapter/__init__.py core/README.md CLAUDE.md
git commit -m "docs: update exports, README, CLAUDE.md for ToolAdapter"
```

---

## Task Summary

| Task | Files | Description |
|------|-------|-------------|
| 1 | 2 new | ToolAdapter class + 10 service tests |
| 2 | 7 modify | Migrate agent/lifecycle/cognitive/tools/scheduler + test_phase2 + test_phase3 |
| 3 | 3 modify | Update __init__.py, README, CLAUDE.md |
