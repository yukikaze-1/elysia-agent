# Planner 拆分设计

> 日期: 2026-06-18  
> 范围: `Module/task_system/core/planner.py` (739行) 拆分为 `planner/` 目录下的 5 个聚焦模块

## 目标

将 `Planner` 类承担的三种职责——prompt 模板、JSON 解析、业务编排——分离到独立文件，同时将 `plan()` 和 `replan()` 拆为两个类。

## 当前问题

`planner.py` 739 行，混合：
- 5 个 prompt 字符串常量 (~145行)
- 5 个 `_parse_*` 方法 + `_PlanParseError` (~292行)
- `plan()` + `replan()` 两个独立业务逻辑 (~280行)
- `_call_llm()` 被 plan 和 replan 共享

`plan()` 和 `replan()` 角色不同（规划器 vs 修复器），prompt 模板不同，放同一类里职责不清。

## 目标结构

```
Module/task_system/core/planner/
├── __init__.py          # 公开导出 Planner, Replanner, PlanParseError
├── prompts.py           # 5 个 prompt 字符串常量（~145 行）
├── parser.py            # 纯函数 + call_llm + PlanParseError（~300 行）
├── planner.py           # Planner 类：plan()（~70 行）
└── replanner.py         # Replanner 类：replan()（~180 行）
```

原 `Module/task_system/core/planner.py` 删除。

### 各文件详情

#### `prompts.py` — 零依赖，纯数据常量

- `OUTPUT_SCHEMA_DESCRIPTION`
- `SYSTEM_PROMPT`
- `USER_PROMPT`
- `REPLAN_SYSTEM_PROMPT`
- `REPLAN_USER_PROMPT`

无 import（仅依赖 Python stdlib string 模板）。

#### `parser.py` — 纯函数，无类

依赖: `Module.task_system.schema`、`Module.task_system.utils.strip_markdown_fence`

- `call_llm(infra: AgentInfra, token_ledger: TokenLedger | None, system: str, user: str) -> str`  
  共享 LLM 调用（推理模式 + JSON Output + token 追踪）。从 `Planner._call_llm()` 提取并改参数为显式传入。
- `parse_plan_from_dict(data: dict) -> Plan`  
  从 dict 解析 Plan（nodes + edges）。由 `Planner._parse_plan_from_dict()` 改名而来。
- `parse_patch_from_dict(data: dict) -> DAGPatch`  
  从 dict 解析 DAGPatch。由 `Planner._parse_patch_from_dict()` 改名而来。
- `parse_execution_mode(raw) -> ExecutionMode`  
  容错解析 execution_mode。由 `Planner._parse_execution_mode()` 改名而来。
- `PlanParseError(Exception)`  
  由 `_PlanParseError` 改名而来，公开导出。

#### `planner.py` — `Planner` 类

依赖: `prompts.py`, `parser.py`, `schema.py`, `utils.format_tools`

- `__init__(self, infra: AgentInfra, token_ledger: TokenLedger | None = None)`
- `plan(self, context: DelegationContext, global_constraints: ConstraintConfig, experience_records: list[ExperienceRecord] | None = None) -> Plan`  
  行为不变：构建 prompt → `call_llm()` → `_parse_response()` → 返回 Plan
- `_parse_response(self, raw: str) -> Plan`  
  薄封装：`strip_markdown_fence()` → `json.loads()` → `parser.parse_plan_from_dict()`

#### `replanner.py` — `Replanner` 类

依赖: `prompts.py`, `parser.py`, `schema.py`, `utils.format_tools`

- `__init__(self, infra: AgentInfra, token_ledger: TokenLedger | None = None)`
- `replan(self, original_plan, failed_node_ids, failure_diagnosis, node_results, intent, tools, max_nodes, max_mutations) -> DAGPatch | Plan`  
  行为不变：格式化节点状态 → 构建 prompt → `call_llm()` → `_parse_replan_response()`
- `_parse_replan_response(self, raw: str) -> DAGPatch | Plan`  
  薄封装：`strip_markdown_fence()` → `json.loads()` → `parser.parse_plan_from_dict()` 或 `parser.parse_patch_from_dict()`
- `_format_nodes_by_status(node_results, plan, statuses) -> str`（static）  
  行为不变

### 受影响文件

**`Module/task_system/core/sub_agent.py`**（唯一消费者）:

```python
# 旧 import
from Module.task_system.core.planner import Planner

# 新 import
from Module.task_system.core.planner import Planner, Replanner

# 内部变更：
# self._planner = Planner(...)
# →
# self._planner = Planner(...)
# self._replanner = Replanner(...)
#
# self._planner.replan(...)
# →
# self._replanner.replan(...)
```

### 不受影响

- 测试文件——测试继续通过，`from Module.task_system.core.planner import Planner` → `from Module.task_system.core.planner import Planner, Replanner`
- `Plan` / `DAGPatch` / `DAGNode` 等 schema 类型——不变
- `PlanValidator`、`Orchestrator`、`Executor`、`Evaluator`——完全不碰

## 验证

```bash
# 单元测试
python -m pytest Module/task_system/tests/unit/test_planner.py -v

# SubAgent 测试（覆盖 plan + replan 调用路径）
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v

# 集成测试
python -m pytest Module/task_system/tests/integration/test_planner.py -v

# TaskSystem 全量
python -m pytest Module/task_system/tests/ -v
```
