# 集成测试稳定性改进设计

**日期**: 2026-06-18
**状态**: 待实现

## 背景

Module/task_system 的集成测试存在两个痛点：

1. **耗时长**：每个测试要走完整 Planner→Executor→Evaluator 链路，每步都是真实 LLM 调用（DeepSeek API），单测 12-25s，多节点任务线性增长。
2. **易触发不必要的 replan**：集成测试依赖 `WebSearchPlugin`，Tavily API 结果不稳定（网络波动、返回内容变化），导致 Evaluator 判 `GOAL_FAILED` 或 retry 耗尽，进而触发 replan。replan 又走一轮 Replanner LLM 调用，进一步拖慢测试甚至触发熔断。

核心原因是 **web_search 工具的输出不可控**。解决思路：给不稳定工具做 test double（mock plugin），让集成测试链路在 Planner 输出稳定 DAG 后，执行层也输出确定性的结果，从根源上避免不必要的 replan。

## 目标

- 集成测试使用稳定可控的工具，避免因网络波动触发 replan
- 保持完整的 Planner→Executor→Evaluator 执行链路（不 mock 核心组件）
- replan / DAGPatch 专项测试仍能通过 monkeypatch 强制触发 replan
- 消除各测试文件中重复的 `_build_task_system()` / `_get_tools_from_manager()` 样板代码

## 非目标

- 不修改 TaskSystem / Planner / Replanner / Evaluator / Orchestrator / SubAgent 的核心逻辑
- 不在 ConstraintConfig 中新增 `replan_enabled` 等开关
- 不改变单元测试（它们已经用 MagicMock/AsyncMock，不涉及此问题）

## 设计

### 1. MockWebSearchPlugin

**文件**: `core/tools/web/mock_plugin.py`（新增）

```python
class MockWebSearchPlugin(ToolPlugin):
    name = "web_search"          # 与真实 WebSearchPlugin 同名
    description = "搜索互联网，返回相关网页的标题、URL 和摘要"  # 与真实一致
    param_model = WebSearchParams
    capabilities = {Capability.NETWORK, Capability.WEB_SEARCH}
    safety = SafetyProfile(network_allowed=True, block_private_ips=True, max_concurrency=3)

    async def execute(self, *, ctx, emit, params):
        # 不调网络，根据 query 返回预设的固定结果
        ...
```

关键设计点：

- **加载 mock 代替真实 plugin**：`ToolManager.load_all()` 按 `instance.name` 注册到 `self._plugins`（`manager.py:67`，简单 dict 赋值）。调用方式为 `tm.load_all([TimePlugin, MockWebSearchPlugin], ctx)`——真实 `WebSearchPlugin` 不传入、不加载。Planner/Executor/Evaluator 通过 name 查找工具，对替换无感知。
- **接口一致**：`param_model`、`capabilities`、`safety` 完全与 `WebSearchPlugin` 一致。
- **显式声明 `safety`**：`safety` 属性在类级别显式赋值（而非继承默认 `SafetyProfile()`）。虽然 `MockWebSearchPlugin` 放在 `core/tools/web/` 目录下会命中 `core/tools/web/config.yaml` 的 `load_all()` 覆盖逻辑（`manager.py:53-65`），但两个插件的安全配置一致，当前场景下行为正确。显式赋值也消除了将来文件移动导致的静默行为变化。
- **确定性输出**：`execute()` 不发起网络请求，根据 `params.query` 关键词返回预设结果。例如：
  - query 含 "天气" → 返回模拟天气数据
  - query 含 "news" / "新闻" → 返回模拟新闻列表
  - 默认 → 返回 3-5 条通用搜索结果
- **输出格式**：与真实 `_tavily_search()` 返回格式一致（`1. title\n   url\n   content`），保证 Executor artifact 对 Evaluator 的 `output_contract` 判断依然有效。
- **无外部依赖**：不需要 `setup()`，不读 env var，不 import `tavily` / `httpx`。

**导出更新**: `core/tools/web/__init__.py` 增加 `MockWebSearchPlugin` 的导出：

```python
from .plugin import WebSearchPlugin, WebFetchPlugin
from .mock_plugin import MockWebSearchPlugin

__all__ = ["WebSearchPlugin", "WebFetchPlugin", "MockWebSearchPlugin"]
```

### 2. .env 加载 — conftest.py

**文件**: `Module/task_system/tests/integration/conftest.py`（新增）

当前 5 个集成测试文件每个都在文件顶部复制了 `.env` 加载代码（`test_e2e_mvp.py:33-42`、`test_task_system_lifecycle.py:24-34`、`test_planner.py:11-21` 等）。使用 pytest 标准实践，新增 `conftest.py` 在模块级别一次性加载 `.env`：

```python
# Module/task_system/tests/integration/conftest.py
import os
from pathlib import Path
from dotenv import load_dotenv

# 自动加载项目根目录的 .env 文件
_env_file = Path(__file__).resolve().parents[5] / ".env"  # 5 levels up to repo root
if _env_file.exists():
    load_dotenv(_env_file)
```

之后各测试文件可直接删除各自的 `.env` 加载样板代码。

### 3. 共享测试辅助模块

**文件**: `Module/task_system/tests/helpers/__init__.py`（新增）

提供三个可复用组件：

**`MOCK_PLUGIN_MAP: dict[str, type[ToolPlugin]]`**

工具名→mock 插件类的映射。初始内容：

```python
from core.tools.web.mock_plugin import MockWebSearchPlugin

MOCK_PLUGIN_MAP: dict[str, type[ToolPlugin]] = {
    "web_search": MockWebSearchPlugin,
}
```

后续新增不稳定工具的 mock 时只需往此 dict 加条目。

**`async def build_test_task_system(...) -> TaskSystem`**

工厂函数，一行创建带真实 LLM + 选定工具的 TaskSystem。

```python
async def build_test_task_system(
    plugins: list[type[ToolPlugin]] | None = None,
    use_mocks: bool = True,
    wrap_llm: bool = False,
) -> TaskSystem:
```

- `plugins`：指定要加载的插件类列表，默认 `None` 则使用 `[TimePlugin]`（纯确定性工具）。e2e 测试需显式传 `[TimePlugin, WebSearchPlugin]`。
- `use_mocks=True`：遍历 `plugins`，对每个 class 查找 `MOCK_PLUGIN_MAP`：如果 `cls.name` 在 map 中，替换为 mock 类。例如传入 `[TimePlugin, WebSearchPlugin]` → 最终传给 `load_all()` 的是 `[TimePlugin, MockWebSearchPlugin]`。`use_mocks=False` 时原样使用 `plugins`（给需要测试真实网络的场景）。
- `wrap_llm=False`：可选包装 LLM client 以记录调用（e2e 测试用）。
- 内部处理：`.env` 加载（作为最后兜底） → `DEEPSEEK_API_KEY` 检查 → 创建 `AsyncOpenAI` → 创建 `ToolManager` → `load_all()` → 构建 `AgentInfra` → `TaskSystem.start()` → 返回 `TaskSystem`。

**`def get_tools_from_manager(tm: ToolManager) -> list[Tool]`**

从 `ToolManager` 提取 `Tool` schema 对象列表，供 `DelegationPreparer.prepare()` 使用。当前此逻辑在 `test_e2e_mvp.py` 和 `test_task_system_lifecycle.py` 中重复出现，提到 helpers 统一复用。

> **已知局限**：此函数访问 `tm._plugins`（私有属性）。虽然现有测试已经这样做，但将其提取到共享 helpers 意味着这个私有访问被正式化。如果将来 `ToolManager` 重构了 `_plugins` 的内部结构，所有测试都会受影响。建议后续 PR 中给 `ToolManager` 增加公共方法（如 `list_tools() -> list[dict]`），届时 `get_tools_from_manager()` 改为调用公共 API。

### 4. replan / DAGPatch 专项测试的 mock

在 `helpers/__init__.py` 中提供：

**`mock_replan_response(response: DAGPatch | Plan)`**

返回一个 `unittest.mock.patch` 上下文管理器，mock `Replanner.replan()` 使其直接返回预设的 `DAGPatch` 或 `Plan`，跳过 LLM 调用。

```python
from unittest.mock import patch

def mock_replan_response(response: DAGPatch | Plan):
    """返回一个 patch 上下文管理器，mock Replanner.replan() 返回固定结果。"""
    async def _mock_replan(self, *args, **kwargs):
        return response
    return patch(
        "Module.task_system.core.planner.replanner.Replanner.replan",
        side_effect=_mock_replan,
    )
```

使用方式（在测试函数体内，不依赖 pytest `monkeypatch` fixture）：

```python
async def test_replan_dagpatch_recovery():
    ts = await build_test_task_system(...)
    # 第一步：monkeypatch Evaluator 强制返回 REPLAN（复用现有模式）
    # 第二步：mock Replanner 返回固定 DAGPatch
    patch_obj = Patch(nodes=[...], edges=[...])
    with mock_replan_response(patch_obj):
        # 提交任务，验证 DAGPatch 被正确应用
        ...
```

**预置样例数据**（可选，后续按需扩展）：
- `SAMPLE_DAGPATCH_2NODE`：一个 2-node 修复 patch（替换单个失败节点）
- `SAMPLE_PLAN_1NODE`：一个单节点完整 Plan（全量重建用）

搭配现有 monkeypatch Evaluator 返回 `REPLAN` 的模式，可以端到端测试：
- Evaluator → REPLAN → Replanner.replan()（mock 掉） → DAGPatch 应用 → 恢复执行
- 验证 `graph.apply_patch()` / `graph.replace()` / `plan_version` 递增等行为

### 5. 集成测试文件改动

4 个集成测试文件改为使用 helpers（`test_planner.py` 不动——它使用纯数据 `Tool` dataclass 构造工具列表，不需要 ToolManager）：

| 文件 | 改动 |
|------|------|
| `test_e2e_mvp.py` | `_build_task_system()` → `build_test_task_system(plugins=[TimePlugin, WebSearchPlugin], wrap_llm=True)`；`_get_tools_from_manager()` → `get_tools_from_manager()`；删除 `.env` 加载代码 |
| `test_task_system_lifecycle.py` | `_build_task_system()` → `build_test_task_system(plugins=[TimePlugin])`；`_tools_from_manager()` → `get_tools_from_manager()`；删除 `.env` 加载代码 |
| `test_executor.py` | `_build_integration_executor()` 中使用 helpers 创建 ToolManager；删除 `.env` 加载代码 |
| `test_delegation_preparer.py` | 删除 `.env` 加载代码；其 `_make_integration_tools()` 使用纯数据 `Tool` 对象（不经过 ToolManager），保持不变 |

`test_planner.py` 不改动：
- 它的 `make_tools()` 直接构造 `Tool` dataclass（非 plugin），路径与 helpers 不同
- 它的 `make_infra()` 使用 `tool_registry=object()`（占位），不需要真实 ToolManager

## 不变

- TaskSystem / Planner / Replanner / Evaluator / Orchestrator / SubAgent 核心逻辑不修改
- `ConstraintConfig` / `TaskConstraints` / `SafetyProfile` 不加新字段
- 单元测试（`tests/unit/`）完全不受影响
- `core/tools/web/plugin.py` 不动
- `core/tools/__init__.py` 的 `ALL_PLUGINS` 列表不动——不包含 mock 插件（mock 永远不应被生产代码加载）

## 验证

1. **MockWebSearchPlugin 单元验证**：实例化后检查 `name`、`param_model`、`capabilities` 与真实 `WebSearchPlugin` 一致；`execute()` 返回稳定字符串。
2. **集成测试回归**：`python -m pytest Module/task_system/tests/integration/ -v`，全部通过，耗时不应超过现有 baseline，且无意外 replan 触发（通过日志确认）。
3. **replan 专项测试**：`test_goal_failed_triggers_replan`、`test_replan_tool_timeout`、`test_replan_business_goal_failed` 仍通过；新增的 mock Replanner 用例能正确验证 DAGPatch/Plan 应用。
4. **helpers 导入验证**：`from Module.task_system.tests.helpers import build_test_task_system, get_tools_from_manager` 可正常导入。
5. **conftest .env 加载验证**：删除各测试文件的 `.env` 加载代码后，`DEEPSEEK_API_KEY` 能正常读取。
