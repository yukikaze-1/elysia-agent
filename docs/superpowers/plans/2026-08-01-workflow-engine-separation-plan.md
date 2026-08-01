# WorkflowEngine 分离实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `Module/task_system` 分离为 `Module/workflow_engine` + 独立契约层 `Module/workflow_contract`，两侧互不 import core 内部，各自独立迭代。

**Architecture:** 三区：core 世界 | 契约层（workflow_contract，纯类型+协议+无状态纯函数+AgentInfra）| workflow_engine 世界（引擎本体 + Module.tool）。四阶段：改名 → 契约层提取 → 剥皮+依赖反转 → 独立迭代基建。

**Tech Stack:** Python 3.11+，pydantic，pytest（marker: unit/service/integration/slow），conda env `Elysia`。

## Global Constraints

- **不变式**：`Module/workflow_engine` 不 import 任何 `core.*`（除 stdlib + Module.tool + workflow_contract）。`Module.tool` 自身的两处 core 引用（`core.schema.error_type`、`core.utils.new_message_id`）为已知传递例外，阶段 3 消解。
- **契约层零业务逻辑**：只放类型、枚举、协议 ABC、无状态容器、无状态纯函数。不 import `core.*`，不 import workflow_engine 内部。
- **改名**：15 个标识符 `Task*`→`Workflow*`；`TaskScheduler`（core/task）、`TaskResultData`（core/schema/event）、`Task`/`TaskType`/`TaskStatus`（core/task）**不参与改名**。
- **每阶段后 `make test-quick` 全绿**；`make test-quick` = `conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short $(TASK_SYSTEM_IGNORE)`。
- **门禁**：阶段 3 后 `grep -rn "from core\.\|import core\." Module/workflow_engine Module/tool --include="*.py" --exclude-dir=tests` 输出为空（不锚定行首，覆盖延迟 import）。
- 代码注释用中文，README 用中文。

---

## Phase 1：改名（task_system → workflow_engine）

### Task 1: 目录 + 包路径 import 重命名

**Files:**
- Rename: `Module/task_system/` → `Module/workflow_engine/`
- Modify: 全部 `Module.task_system` 包路径引用（生产 + 测试 + Makefile + 文档）

**Interfaces:**
- Consumes: 无（项目现状）
- Produces: `Module.workflow_engine` 包路径可用，标识符名暂不变（仍是 `TaskSystem` 等旧名）

- [ ] **Step 1: 记录基线 HEAD**

```bash
git rev-parse HEAD > /tmp/workflow_base.txt
```

- [ ] **Step 2: 目录重命名**

```bash
git mv Module/task_system Module/workflow_engine
```

- [ ] **Step 3: 全量替换包路径 import（生产 + 测试 + 文档 + Makefile）**

对仓库内所有非 site-packages、非 `__pycache__` 的 `.py`/`.md`/`Makefile`：

```bash
# 代码：Module.task_system → Module.workflow_engine（含 Module.task_system 前缀的所有子模块）
grep -rl "Module\.task_system" Module/workflow_engine core Module --include="*.py" --exclude-dir=__pycache__ | xargs sed -i 's/Module\.task_system/Module.workflow_engine/g'

# Makefile --ignore
sed -i 's#--ignore=Module/task_system/tests#--ignore=Module/workflow_engine/tests#' Makefile

# 文档（README/CLAUDE.md/architecture/Module-README 里的路径引用）
# ⚠️ 排除：本计划文件、docs/review/*、docs/completed/*、docs/superpowers/archive/*——历史记录不改写
grep -rl "Module/task_system\|Module\.task_system" docs CLAUDE.md README.md Module/README.md Module/workflow_engine/README.md core/README.md --include="*.md" 2>/dev/null \
  | grep -vE "superpowers/plans/|docs/review/|docs/completed/|docs/superpowers/archive/" \
  | xargs -r sed -i 's#Module/task_system#Module/workflow_engine#g; s#Module\.task_system#Module.workflow_engine#g'
```

> ⚠️ **文档 sed 三步防护**：① 排除本计划文件（`superpowers/plans/`）——它含 `Module/task_system` 的 sed 模式与 `git mv` 命令，被改写会破坏自身；② 排除 `docs/review/`、`docs/completed/`、`docs/superpowers/archive/`——历史记录按 Task 4 约定不改写；③ sed 后遗留的**裸相对链接**（如 `task_system/docs/`、`task_system/tests/`，无 `Module/` 前缀）不匹配任何模式，在 Step 4 中人工改 `workflow_engine/...`。

- [ ] **Step 4: 验证包路径 import + 修复裸相对链接**

```bash
# 收集阶段：不应再有任何 Module.task_system 引用
grep -rn "Module\.task_system\|Module/task_system" Module/workflow_engine core Module --include="*.py" --exclude-dir=__pycache__ | head
# 期望：空输出（task_system 目录自身已改名，只剩 workflow_engine）

# 文档里的裸相对链接（无 Module/ 前缀）：人工改为 workflow_engine/...
grep -rn "task_system/" README.md Module/README.md CLAUDE.md core/README.md docs/architecture.md --include="*.md" 2>/dev/null | grep -v "docs/review/\|docs/completed/\|superpowers/" | head
# 逐个把 `task_system/docs/`、`task_system/tests/` 等改为 `workflow_engine/docs/`、`workflow_engine/tests/`
```

- [ ] **Step 5: 跑 workflow_engine 模块测试（标识符名未变，应全绿）**

```bash
conda run -n Elysia python -m pytest Module/workflow_engine/tests -m "unit or service" -q --tb=short
# 期望：全部 PASS（或仅个别与改名无关的既有失败）
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor: task_system 目录与包路径重命名为 workflow_engine（标识符暂留 Task*）"
```

---

### Task 2: 模块内 15 标识符 Task* → Workflow* + 文件重命名

**Files:**
- Rename: `Module/workflow_engine/task_system.py` → `Module/workflow_engine/workflow_engine.py`
- Modify: `Module/workflow_engine/` 内全部 `.py`（含 `tests/`、`__init__.py`）

**Interfaces:**
- Consumes: Task 1 的 `Module.workflow_engine` 包路径
- Produces: 模块内所有标识符为 `Workflow*`；`__all__` 同步更新。**注意：本任务后消费者（core/*）引用旧名会 import 失败，属预期中间态，Task 3 修复。**

- [ ] **Step 1: 模块内 sed 全量改名（15 个标识符，\b 词边界保证不误伤重叠名）**

```bash
cd Module/workflow_engine
find . -name "*.py" -not -path "*__pycache__*" -exec sed -i -E '
s/\bTaskSystem\b/WorkflowEngine/g;
s/\bTaskMailbox\b/WorkflowMailbox/g;
s/\bTaskId\b/WorkflowId/g;
s/\bTaskStatus\b/WorkflowStatus/g;
s/\bTaskEventType\b/WorkflowEventType/g;
s/\bTaskEvent\b/WorkflowEvent/g;
s/\bTaskState\b/WorkflowState/g;
s/\bTaskErrorCode\b/WorkflowErrorCode/g;
s/\bTaskCheckpoint\b/WorkflowCheckpoint/g;
s/\bTaskConstraints\b/WorkflowConstraints/g;
s/\bTaskSummary\b/WorkflowSummary/g;
s/\bTaskDAG\b/WorkflowDAG/g;
s/\bTaskDebugAPI\b/WorkflowDebugAPI/g;
s/\bTaskProgress\b/WorkflowProgress/g;
s/\bTaskResult\b/WorkflowResult/g;
' {} \;
```

- [ ] **Step 2: 主文件重命名 + 子模块路径改写**

```bash
git mv Module/workflow_engine/task_system.py Module/workflow_engine/workflow_engine.py

# ⚠️ 关键：改文件名的同时改写 `Module.workflow_engine.task_system` 子模块路径
#（__init__.py、debug_api.py、core 消费者都 import 它；不改则 import 直接 ModuleNotFoundError）
grep -rl "Module\.workflow_engine\.task_system" Module/workflow_engine core --include="*.py" --exclude-dir=__pycache__ \
  | xargs -r sed -i 's#Module\.workflow_engine\.task_system#Module.workflow_engine.workflow_engine#g'
```

- [ ] **Step 3: 验证模块内无 Task* 残留（排除注释允许）**

```bash
# 先确认子模块路径改写生效：import Module.workflow_engine 不再报 ModuleNotFoundError
conda run -n Elysia python -c "import Module.workflow_engine.workflow_engine" 2>&1 | head -3
```

```bash
grep -rnE "\bTask[A-Z]" Module/workflow_engine --include="*.py" --exclude-dir=__pycache__ | grep -v "TaskSummaryStore\|TaskScheduler\|TaskResultData" | head
# 期望：仅剩「TaskScheduler（core/task 对照注释）」等说明性引用
```

- [ ] **Step 4: 跑模块测试（模块内已自洽）**

```bash
conda run -n Elysia python -m pytest Module/workflow_engine/tests -m "unit or service" -q --tb=short
# 期望：全部 PASS
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: workflow_engine 模块内 15 标识符 Task*→Workflow* + 主文件重命名"
```

---

### Task 3: 消费者标识符改名 + Makefile 确认 + CLAUDE.md 索引

**Files:**
- Modify: `core/cognitive/loop.py`、`core/cognitive/tools.py`、`core/agent/agent.py`、`core/agent/_types.py`、`core/agent/lifecycle.py`、`core/agent/server.py`、`core/agent/_lifecycle_task_system.py`、`core/input/server.py`、`core/input/debug_routes/__init__.py`、`core/tools/runtime_config/plugin.py`、`core/agent/tests/test_types.py`、`core/tests/test_phase3.py`
- Modify: `CLAUDE.md`（模块树/模块索引中的 task_system 引用，Task 1 若未覆盖）

**Interfaces:**
- Consumes: Task 2 后的 `WorkflowEngine`/`WorkflowMailbox`/`WorkflowId`/`WorkflowDebugAPI` 等新名
- Produces: 全仓库消费者编译通过，`make test-quick` 全绿

- [ ] **Step 1: 消费者 sed——只改从 task_system 来的 4 个标识符（绝不碰 core/task 的 TaskScheduler/Task/TaskType/TaskStatus 和 core/schema 的 TaskResultData）**

```bash
# 消费者生产代码 + 测试：只替换这 4 个（消费者实际只用到这 4 个 Task* 标识符）
grep -rlE "Module\.workflow_engine" core/ Module/ --include="*.py" --exclude-dir=__pycache__ \
  | xargs sed -i -E '
s/\bTaskSystem\b/WorkflowEngine/g;
s/\bTaskMailbox\b/WorkflowMailbox/g;
s/\bTaskId\b/WorkflowId/g;
s/\bTaskDebugAPI\b/WorkflowDebugAPI/g;
'
```

> ⚠️ 此 sed 只作用于引用了 `Module.workflow_engine` 的文件；core/task 与 core/schema/event 不引用它，其 `TaskScheduler`/`Task`/`TaskType`/`TaskStatus`/`TaskResultData` 不会被触碰。

- [ ] **Step 2: 验证消费者无残留旧名**

```bash
# 全仓库：还有谁引用旧标识符但 import 自 workflow_engine？
grep -rnE "\bTask(System|Mailbox|Id|DebugAPI)\b" core/ Module/ --include="*.py" --exclude-dir=__pycache__ --exclude-dir=tests | grep -v "TaskScheduler\|TaskResultData" | head
# 期望：空（或仅不相关引用）
```

- [ ] **Step 3: 全量测试**

```bash
make test-quick
# 期望：全绿（1667+ passed，task_system→workflow_engine 的 --ignore 已更新）
```

- [ ] **Step 4: CLAUDE.md 模块索引核对**

检查 `CLAUDE.md` 中 `core/task/`、`Module/task_system`、debug 面板描述是否已随 Task 1 sed 更新；`Module/task_system` 若残留则手动改为 `Module/workflow_engine`。补一行说明：主类 `WorkflowEngine`、契约层 `Module/workflow_contract`（阶段 2 落位后补）。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: 消费者侧 Task*→Workflow* 标识符 + CLAUDE.md 索引同步"
```

---

### Task 4: 阶段 1 验收

**Files:** 无（纯验证）

- [ ] **Step 1: grep 门禁——全仓库无旧包路径 / 旧标识符（排除项除外）**

```bash
grep -rn "Module\.task_system\|Module/task_system" . --include="*.py" --include="*.md" --exclude-dir=__pycache__ --exclude-dir=node_modules | grep -v docs/completed | head
# 期望：仅历史归档 docs/completed/* 中的旧引用（不修改历史）
grep -rnE "\bTask(System|Mailbox|Id|DebugAPI|DebugAPI)\b" core/ Module/ --include="*.py" --exclude-dir=__pycache__ | grep -v "workflow" | grep -v "TaskScheduler\|TaskResultData" | head
# 期望：空
```

- [ ] **Step 2: test-quick + 模块独立测试双绿**

```bash
make test-quick
conda run -n Elysia python -m pytest Module/workflow_engine/tests -m "unit or service" -q --tb=short
```

- [ ] **Step 3: 记录 Phase 1 完成标记**

```bash
echo "Phase 1 完成：rename 全绿" 
```

---

## Phase 2：契约层提取（Module/workflow_contract/）

### Task 5: 创建契约层 + 跨边类型迁入 + AgentInfra

**Files:**
- Create: `Module/workflow_contract/__init__.py`、`Module/workflow_contract/schema.py`、`Module/workflow_contract/infra.py`、`Module/workflow_contract/tests/__init__.py`、`Module/workflow_contract/tests/test_schema.py`
- Modify: `Module/workflow_engine/schema/*.py`（删除迁走的类型定义）、`Module/workflow_engine/schema/__init__.py`（`__all__` 去掉迁走项）、`Module/workflow_engine/infra.py`（删除，AgentInfra 迁走）、`Module/workflow_engine/__init__.py`（不再导出迁走项）

**Interfaces:**
- Consumes: Task 2 的 `Workflow*` 命名
- Produces: `Module.workflow_contract` 导出以下跨边类型（从 `Module/workflow_engine/schema/protocol.py`、`schema/types.py`、`schema/output.py`、`infra.py` **原样搬移**类定义；**`Tool` 不在契约层**，直接归位 Module.tool）：

```python
# Module/workflow_contract/__init__.py 导出清单
DelegationContext, DelegationResult, DelegationStatus,
SubmitError, HITLData, ConstraintConfig, ApprovalId,
WorkflowId, WorkflowStatus, WorkflowEvent, WorkflowEventType, WorkflowConstraints,
SessionId, HITLNotifier, AgentInfra
```

> **字段类型同层解析**：`DelegationContext` 三字段——`tools: list[Tool]`（**先归位 Module.tool，契约层从 `Module.tool.tool_descriptor` 引用**）、`task_constraints: WorkflowConstraints`（契约层）、`session_id: SessionId`（契约层）——全部在契约层或 Module.tool 可解析，不产生对 core/引擎内部的引用。

- [ ] **Step 1a: Tool 投影直接归位 Module.tool（契约层 DelegationContext 从之引用）**

`Module/workflow_engine/schema/protocol.py` 的 `Tool` 轻量投影（`name: str`、`description: str = ""`、`capabilities: list[str]`）原样复制到 `Module/tool/tool_descriptor.py`。`Module/tool/__init__.py` 导出 `Tool`。契约层 `schema.py` 的 `DelegationContext.tools: list[Tool]` 改为 `from Module.tool.tool_descriptor import Tool`。**`Tool` 不进契约层**——这是允许方向（Module.tool 属 workflow_engine 世界）。

- [ ] **Step 1b: 搬移跨边类型定义（从源文件原样复制，非重写）**

源 → 目标（**源路径已核对实际位置**）：
- `Module/workflow_engine/schema/protocol.py` 中的 `DelegationContext`、`DelegationResult`、`DelegationStatus`、`SubmitError`、`HITLData`、`ConstraintConfig`、`WorkflowConstraints`（原 `TaskConstraints`）→ `Module/workflow_contract/schema.py`
- `Module/workflow_engine/schema/types.py` 中的 `ApprovalId`（`types.py:95`，NewType）、`SessionId`、`WorkflowId`（原 `TaskId`）、`WorkflowStatus`（原 `TaskStatus`，`types.py:127`）→ `Module/workflow_contract/schema.py`
- `Module/workflow_engine/schema/output.py` 中的 `WorkflowEvent`、`WorkflowEventType`（原 `TaskEvent`/`TaskEventType`，`output.py:29,39`，`TaskEvent(BaseModel)` 无 Event 基类）→ `Module/workflow_contract/schema.py`
- `Module/workflow_engine/protocol.py` 中的 `HITLNotifier`（ABC）→ `Module/workflow_contract/schema.py`
- `Module/workflow_engine/infra.py` 整个 `AgentInfra` dataclass → `Module/workflow_contract/infra.py`

**契约闭合规则**：搬移后契约层不得引用 `core.*`，不得引用 `Module.workflow_engine`。凡被搬移类型引用但未在清单中的类型/枚举（`ReturnMode`、`AgentId`、`NodeId` 等）**一并迁入或改引用**。搬移完成用自检证明闭合：

```bash
# 契约层自包含验证：逐类 import + 构造，确保无缺失引用
conda run -n Elysia python -c "
import Module.workflow_contract as wc
for n in ['DelegationContext','DelegationResult','DelegationStatus','SubmitError','HITLData','ConstraintConfig','WorkflowId','WorkflowStatus','WorkflowEvent','WorkflowEventType','WorkflowConstraints','SessionId','ApprovalId','HITLNotifier','AgentInfra']:
    assert hasattr(wc, n), f'契约层缺 {n}'
from Module.tool.tool_descriptor import Tool  # Tool 在 Module.tool
print('契约层闭合 OK')
"
```

**SessionId 定案**：`SessionId` 契约层拥有（NewType，str）。`core/schema/types.py` 的 SessionId 改为 `from Module.workflow_contract.schema import SessionId` 重导出（core 侧公开 API 不变，`core/schema → Module.workflow_contract` 是允许方向，契约层不反引 core）。

- [ ] **Step 2: 更新 workflow_engine 内部引用**

`Module/workflow_engine/` 内原 import 这些类型的地方改为 `from Module.workflow_contract import X`。删除 `schema/*.py` 中已搬走的类定义；`schema/__init__.py`、`__init__.py` 的 `__all__` 去掉迁走项。删除 `infra.py`（内容已迁）。

- [ ] **Step 3: 写契约层单测**

`Module/workflow_contract/tests/test_schema.py`：

```python
import pytest

pytestmark = pytest.mark.unit

from Module.workflow_contract import (
    DelegationContext, DelegationResult, DelegationStatus,
    SessionId, WorkflowId, WorkflowStatus, WorkflowConstraints,
)


def test_delegation_result_tri_state():
    # 三态可构造
    assert DelegationStatus.OK.value == "ok"
    assert DelegationStatus.NO_MATCHING_TOOLS.value == "no_matching_tools"
    assert DelegationStatus.TOO_VAGUE.value == "too_vague"


def test_workflow_id_newtype():
    # NewType 是 str 的运行时别名
    assert WorkflowId("wf-1") == "wf-1"
    assert SessionId("s-1") == "s-1"


def test_delegation_context_roundtrip():
    ctx = DelegationContext(
        intent="查天气",
        tools=[{"name": "web_search", "description": "", "capabilities": []}],
        task_constraints=WorkflowConstraints(),
        session_id=SessionId("s-1"),
    )
    # pydantic 校验 + 序列化往返
    again = DelegationContext.model_validate_json(ctx.model_dump_json())
    assert again.intent == "查天气"
    assert again.session_id == "s-1"
```

> ⚠️ `DelegationContext.task_constraints` 的字段类型是 `TaskConstraints`（→ `WorkflowConstraints`），**不是** `ConstraintConfig`（后者是全局天花板约束）。若 `Tool` 投影字段形状与上例不同，按 `Module/workflow_engine/schema/protocol.py` 中 `Tool` 的实际字段调整测试构造。

- [ ] **Step 4: 跑契约层 + workflow_engine 测试**

```bash
conda run -n Elysia python -m pytest Module/workflow_contract/tests Module/workflow_engine/tests -m "unit or service" -q --tb=short
# 期望：契约层新测 PASS + workflow_engine 模块测试 PASS
```

> ⚠️ 本任务后消费者（core/*）可能引用已被 workflow_engine 移除的导出而失败——Task 6 统一修复，期间以模块测试为门禁。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: 新建 Module/workflow_contract 契约层（跨边类型 + AgentInfra 迁入）"
```

---

### Task 6: utils 迁契约层（format_tools / strip_markdown_fence）

**Files:**
- Create: `Module/workflow_contract/utils.py`
- Modify: `Module/workflow_engine/utils.py`（删除两函数或保留为空壳）、`Module/workflow_engine/delegation_preparer.py`、`Module/workflow_engine/core/planner/{planner,replanner,parser}.py`、`Module/workflow_contract/__init__.py`

**Interfaces:**
- Consumes: Task 5 的契约层
- Produces: `Module.workflow_contract.utils` 导出 `format_tools(tools, *, include_capabilities=True)` 与 `strip_markdown_fence(text)`

- [ ] **Step 1: 搬移纯函数到契约层 utils（3 个）**

把以下无状态纯函数原样复制到 `Module/workflow_contract/utils.py`：
- `Module/workflow_engine/utils.py` 的 `strip_markdown_fence(text: str) -> str`
- `Module/workflow_engine/utils.py` 的 `format_tools(tools: list[Tool], *, include_capabilities: bool = True) -> str`（`Tool` 从 `Module.workflow_contract.schema` import）
- `Module/workflow_engine/schema/types.py:56` 的 `parse_iso(ts: str) -> datetime`（**定案**：随迁契约层 utils——纯函数，core 侧 plugin.py 与编排层共用；`schema/__init__.py` 的『内部工具不导出』注释相应删除 parse_iso 条目）

`workflow_contract/__init__.py` 加 `from .utils import format_tools, strip_markdown_fence, parse_iso`。

- [ ] **Step 2: 更新全部引用方（含测试文件）**

```bash
# ⚠️ 6 个文件都要改：delegation_preparer + 引擎侧 3 文件 + 2 个测试文件
# （test_planner.py:16、test_delegation_preparer.py:32 也顶层 import 这两个函数）
sed -i 's#from Module\.workflow_engine\.utils import #from Module.workflow_contract.utils import #' \
  Module/workflow_engine/delegation_preparer.py \
  Module/workflow_engine/core/planner/planner.py \
  Module/workflow_engine/core/planner/replanner.py \
  Module/workflow_engine/core/planner/parser.py \
  Module/workflow_engine/tests/unit/test_planner.py \
  Module/workflow_engine/tests/unit/test_delegation_preparer.py

# parse_iso：query.py 的多行 schema import 里移出 parse_iso，改单行 import 契约层
#（query.py:30 在 `from Module.workflow_engine.schema import (..., parse_iso, ...)` 中）
# 手工把 query.py 的 parse_iso 从 schema import 移到：from Module.workflow_contract.utils import parse_iso
# schema/__init__.py:26 移除 parse_iso 导出；core 侧 plugin.py 在 Task 7 改

# ⚠️ 删除前检查：不得用 -v planner / -v delegation_preparer 子串过滤——
#    那会把 test_planner.py / test_delegation_preparer.py 的命中吞掉造成假空。
#    只匹配 workflow_engine.utils（契约层 workflow_contract.utils 不命中）。
grep -rn "workflow_engine\.utils" Module/workflow_engine --include="*.py" --exclude-dir=__pycache__
# 期望：空输出 → 才可删除 utils.py
git rm Module/workflow_engine/utils.py
```

- [ ] **Step 3: 跑 workflow_engine 模块测试**

```bash
conda run -n Elysia python -m pytest Module/workflow_engine/tests -m "unit or service" -q --tb=short
# 期望：PASS
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor: format_tools/strip_markdown_fence 迁契约层 utils"
```

---

### Task 7: 消费者 import 迁移到契约层 + 阶段 2 验收

**Files:**
- Modify: `core/cognitive/loop.py`、`core/cognitive/tools.py`、`core/agent/_lifecycle_task_system.py`、`core/tools/runtime_config/plugin.py`、`core/tests/test_phase3.py`、`core/agent/tests/test_types.py`

**Interfaces:**
- Consumes: Task 5/6 的 `Module.workflow_contract`
- Produces: 外部消费者全部从 `Module.workflow_contract` 引用契约类型，`make test-quick` 全绿

- [ ] **Step 1: 消费者 import 精确改写（逐文件，深路径覆盖）**

> ⚠️ **不做整文件盲 sed**——`schema` 里还有 `UserProfile`（Task 8 归位 core，Task 7 期间仍可从 workflow_engine schema import）与引擎内部类型。盲目整体替换会指向不存在的契约导出。

```bash
# ① core/cognitive/loop.py：深路径 schema.types → 契约层
sed -i 's#from Module\.workflow_engine\.schema\.types import WorkflowId#from Module.workflow_contract import WorkflowId#' core/cognitive/loop.py

# ② core/cognitive/tools.py：深路径 SessionId + 浅层 SubmitError + Tool→Module.tool
sed -i 's#from Module\.workflow_engine\.schema\.types import SessionId as _SessionId#from Module.workflow_contract import SessionId as _SessionId#' core/cognitive/tools.py
sed -i 's#from Module\.workflow_engine\.schema import SubmitError#from Module.workflow_contract import SubmitError#' core/cognitive/tools.py
sed -i 's#from Module\.workflow_engine\.schema import Tool#from Module.tool.tool_descriptor import Tool#' core/cognitive/tools.py

# ③ core/agent/_lifecycle_task_system.py：浅层 + 深路径（schema.protocol / schema.types）
sed -i 's#from Module\.workflow_engine\.schema import ConstraintConfig#from Module.workflow_contract import ConstraintConfig#' core/agent/_lifecycle_task_system.py
sed -i 's#from Module\.workflow_engine\.schema\.protocol import HITLData#from Module.workflow_contract import HITLData#' core/agent/_lifecycle_task_system.py
sed -i 's#from Module\.workflow_engine\.schema\.types import ApprovalId#from Module.workflow_contract import ApprovalId#' core/agent/_lifecycle_task_system.py

# ④ core/tools/runtime_config/plugin.py：parse_iso → 契约层 utils（Task 6 已迁）
sed -i 's#from Module\.workflow_engine\.schema import parse_iso#from Module.workflow_contract.utils import parse_iso#' core/tools/runtime_config/plugin.py

# ⑤ 测试文件：test_phase3 / test_types 只 import 契约类型，可整文件替换
sed -i 's#from Module\.workflow_engine\.schema import#from Module.workflow_contract import#g' core/tests/test_phase3.py core/agent/tests/test_types.py

# ⑥ 不动：UserProfile 的三处 import（loop.py:44 / tools.py:15,30 / _lifecycle:88）——
#    Task 8 归位 core/cognitive/user_profile.py 时统一改
```

> `HITLData`/`ApprovalId`/`ConstraintConfig`/`WorkflowId`/`SessionId` 等类型在 Task 5 已从 workflow_engine schema 删走，深路径与浅层 import 必须在此改写；`UserProfile` 未迁走，此刻仍可用。

- [ ] **Step 2: 全量测试**

```bash
make test-quick
# 期望：全绿
```

- [ ] **Step 3: 阶段 2 验收——消费者不再 import workflow_engine 的 schema（UserProfile 除外）**

```bash
grep -rn "from Module\.workflow_engine\.schema" core/ --include="*.py" --exclude-dir=__pycache__
# 期望：仅剩 UserProfile 的三处 import（loop.py:44 / tools.py:15,30 / _lifecycle:88）——
#       契约类型已全部经 Module.workflow_contract / Module.tool / contract.utils
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor: 消费者契约类型 import 迁移到 Module.workflow_contract"
```

---

## Phase 3：剥皮 + 依赖反转

### Task 8: UserProfile / RBACLevel 归位（Tool 已在 Task 5 Step 1a 归位 Module.tool）

**Files:**
- Modify: `Module/workflow_engine/schema/types.py`（RBACLevel 迁出）、`Module/workflow_engine/schema/protocol.py`（UserProfile 迁出）、`Module/tool/`（RBACLevel 迁入）、`core/cognitive/user_profile.py`（UserProfile 落位，新建）、core 侧 4 个消费者

**Interfaces:**
- Consumes: Task 5（Tool→Module.tool 已完成）、Task 7 消费者迁移（UserProfile 三处 import 仍指 workflow_engine schema）
- Produces: `RBACLevel` 在 `Module.tool`；`UserProfile` 在 `core/cognitive/user_profile.py`

- [ ] **Step 1: RBACLevel 归位 Module.tool**

`Module/workflow_engine/schema/types.py` 的 `RBACLevel` 枚举 → `Module/tool/`（建议 `Module/tool/capability.py` 或协议文件）。workflow_engine 内引用改为从 Module.tool import。

- [ ] **Step 2: UserProfile 归位 core**

`Module/workflow_engine/schema/protocol.py` 的 `UserProfile` → 新建 `core/cognitive/user_profile.py`（CognitiveLoop 拥有；`model_config = {"extra": "allow"}` 占位语义保持）。**消费者三处 + 一处全部改向**：

```bash
sed -i 's#from Module\.workflow_engine\.schema import UserProfile#from core.cognitive.user_profile import UserProfile#' \
  core/cognitive/loop.py core/cognitive/tools.py core/agent/_lifecycle_task_system.py
# workflow_engine schema/protocol.py 删除 UserProfile 定义；schema/__init__.py 移除导出
```

> ⚠️ 若 `Module/workflow_engine` 内部（如 delegation_preparer）仍引用 UserProfile，改从 `core.cognitive.user_profile` 引用或删——`delegation_preparer` 已在 Task 9 迁 core，届时自然同层。

- [ ] **Step 3: 测试**

```bash
make test-quick
conda run -n Elysia python -m pytest Module/workflow_engine/tests -m "unit or service" -q --tb=short
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor: RBACLevel 归位 Module.tool，UserProfile 归位 core"
```

---

### Task 9: DelegationPreparer 迁 core/cognitive/delegation.py

**Files:**
- Move: `Module/workflow_engine/delegation_preparer.py` → `core/cognitive/delegation.py`
- Modify: `core/cognitive/loop.py`、`core/cognitive/tools.py`、`core/agent/lifecycle.py`、`core/agent/_lifecycle_task_system.py`（import 改 `from core.cognitive.delegation import DelegationPreparer`）

**Interfaces:**
- Consumes: Task 8 归位后的类型
- Produces: `core/cognitive/delegation.py::DelegationPreparer`；`Module/workflow_engine` 不再持有它

- [ ] **Step 1: 物理迁移 + import 修正**

```bash
git mv Module/workflow_engine/delegation_preparer.py core/cognitive/delegation.py
sed -i 's#Module\.workflow_engine#core.cognitive#g; s#Module\.workflow_contract#Module.workflow_contract#g' core/cognitive/delegation.py
# 手工核对 delegation.py 内 import：它引用契约层类型（契约层不反引用它）
# 消费者 import 改向
grep -rl "from Module\.workflow_engine\.delegation_preparer import" core/ --include="*.py" --exclude-dir=__pycache__ | xargs -r sed -i 's#from Module\.workflow_engine\.delegation_preparer import #from core.cognitive.delegation import #g'
# __init__.py 移除 DelegationPreparer 导出
```

- [ ] **Step 2: 消解 core.schema.message/part 依赖**

核对 `core/cognitive/delegation.py` 顶部 `from core.schema.message import Message` 与 `from core.schema.part import TextPart`——迁入 core 后这些是**合法引用**（delegation_preparer 属于 core），无需改动。

- [ ] **Step 3: 测试**

```bash
make test-quick
# 期望：全绿。WorkflowEngine.__init__ 若构造 DelegationPreparer，改为经 AgentInfra/契约层注入（见 _lifecycle_task_system.py 现有接线）
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor: DelegationPreparer 迁 core/cognitive/delegation.py"
```

---

### Task 10: EventBusHITLNotifier → core/hitl.py

**Files:**
- Move: `Module/workflow_engine/protocol.py` 的 `EventBusHITLNotifier` 实现 → `core/hitl.py`
- Modify: `core/agent/_lifecycle_task_system.py`（import）

**Interfaces:**
- Consumes: Task 5 契约层（`HITLNotifier` ABC 已在契约层）
- Produces: `core/hitl.py::EventBusHITLNotifier`

- [ ] **Step 1: 迁移实现**

`Module/workflow_engine/protocol.py` 仅保留 `HITLNotifier` ABC 的契约层引用；`EventBusHITLNotifier`（EventBus 演示实现）→ `core/hitl.py`。core 侧消费者 `from core.hitl import EventBusHITLNotifier`。`Module/workflow_engine/protocol.py` 若空则 `git rm`。

- [ ] **Step 2: 测试 + Commit**

```bash
make test-quick
git add -A && git commit -m "refactor: EventBusHITLNotifier 迁 core/hitl.py"
```

---

### Task 11: ErrorType → Module/_common + new_message_id 就地 uuid

**Files:**
- Create: `Module/_common/error_type.py`
- Modify: `Module/_common/__init__.py`、`core/schema/error_type.py`（改为重导出壳）、`Module/tool/protocol.py`、`Module/tool/manager.py`、`Module/tool/__init__.py`、`Module/tool/safety.py`、`Module/tool/__main__.py`、`Module/tool/safety.py:618`

**Interfaces:**
- Consumes: 无
- Produces: `Module._common.ErrorType`；`Module.tool` 不再引用 `core.*`

- [ ] **Step 1: ErrorType 迁 Module/_common**

```bash
# 1) 把 core/schema/error_type.py 的 ErrorType 枚举原样复制到 Module/_common/error_type.py
# 2) Module/_common/__init__.py 增加：
cat >> Module/_common/__init__.py <<'EOF'

from .error_type import ErrorType

__all__ = ["ErrorType"]
EOF
# 3) core/schema/error_type.py 改为重导出壳（core 侧 import 路径不变，兼容）：
cat > core/schema/error_type.py <<'EOF'
"""工具错误分类 — 从 Module/_common 重导出（保持 core.schema.error_type 兼容）。"""
from Module._common.error_type import ErrorType

__all__ = ["ErrorType"]
EOF
# 4) Module.tool 全部改向：
grep -rl "from core\.schema\.error_type import ErrorType" Module/tool --include="*.py" --exclude-dir=__pycache__ | xargs -r sed -i 's#from core\.schema\.error_type import ErrorType#from Module._common.error_type import ErrorType#g'
```

- [ ] **Step 2: new_message_id 就地 uuid（safety.py:618）**

```bash
# 顶部补 import uuid（当前未 import）
sed -i 's/^import asyncio$/import asyncio\nimport uuid/' Module/tool/safety.py
# 替换 618-619 行（保留 msg_ 前缀，行为等价）
perl -0pi -e 's/from core\.utils import new_message_id\n        request_id = new_message_id\(\)/request_id = f"msg_{uuid.uuid4().hex[:26]}"/' Module/tool/safety.py
```

- [ ] **Step 3: 验证 Module.tool 无 core.* 残留**

```bash
grep -rn "from core\.\|import core\." Module/tool --include="*.py" --exclude-dir=__pycache__ --exclude-dir=tests
# 期望：空
```

- [ ] **Step 4: 测试**

```bash
make test-quick
conda run -n Elysia python -m pytest Module/workflow_engine/tests Module/workflow_contract/tests -m "unit or service" -q --tb=short
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: ErrorType 迁 Module/_common，new_message_id 就地 uuid（Module.tool 去 core 依赖）"
```

---

### Task 12: 阶段 3 验收（门禁 grep）

**Files:** 无（纯验证）

- [ ] **Step 1: 核心门禁——workflow_engine 与 Module.tool 零 core 引用**

```bash
grep -rn "from core\.\|import core\." Module/workflow_engine Module/tool --include="*.py" --exclude-dir=__pycache__ --exclude-dir=tests
# 期望：空输出
```

- [ ] **Step 2: 契约层零 core 引用**

```bash
grep -rn "from core\.\|import core\." Module/workflow_contract --include="*.py" --exclude-dir=__pycache__ --exclude-dir=tests
# 期望：空输出
```

- [ ] **Step 3: workflow_engine 生产 import 白名单核对**

```bash
# ⚠️ 不用 -n（会给每行加 N: 前缀，破坏后续过滤的 ^ 锚点）。列清单人工核对：
grep -rhE "^(from|import) " Module/workflow_engine --include="*.py" --exclude-dir=__pycache__ --exclude-dir=tests | sort -u
```

人工核对标准：
- `Module` 前缀仅允许 `workflow_engine` / `workflow_contract` / `tool` / `_common`；
- 其余为 stdlib 或通用第三方（pydantic/httpx/openai/pymilvus/motor/bson 等）；
- **不应出现** `from core.` 或 `import core.`（Step 1 已保证，此处双重确认）。

- [ ] **Step 4: 全量回归**

```bash
make test-quick
```

- [ ] **Step 5: 记录阶段 3 完成**

```bash
echo "Phase 3 完成：干净叶子验证通过"
```

---

## Phase 4：独立迭代基建

### Task 13: make test-workflow-engine + 独立 README

**Files:**
- Modify: `Makefile`
- Create: `Module/workflow_engine/README.md`（替换旧 README）、`Module/workflow_contract/README.md`

**Interfaces:**
- Consumes: 全部分离成果
- Produces: `make test-workflow-engine` 门禁；两包独立 README

- [ ] **Step 1: Makefile 加独立测试目标**

```makefile
# 追加到 Makefile（在 test-full 之后）：
test-workflow-engine:
	conda run -n Elysia python -m pytest Module/workflow_engine/tests Module/workflow_contract/tests -m "unit or service" -q --tb=short
```

- [ ] **Step 2: 验证命令**

```bash
make test-workflow-engine
# 期望：全绿（不含 integration——那些需 DEEPSEEK_API_KEY，不进门禁）
```

- [ ] **Step 3: workflow_engine README 重写**

`Module/workflow_engine/README.md` 改为：三区架构图（core | workflow_contract | workflow_engine）、与 core/task TaskScheduler 的关系说明、独立迭代命令 `make test-workflow-engine`、契约层链接。面包屑 + 相关文档节（遵循 `docs/readme-conventions.md`）。

- [ ] **Step 4: workflow_contract README 新建**

`Module/workflow_contract/README.md`：契约层职责（零业务逻辑）、类型清单、允许的依赖方向、测试命令。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: make test-workflow-engine 门禁 + workflow_engine/workflow_contract README"
```

---

### Task 14: 全量文档更新 + 最终验收

**Files:**
- Modify: `CLAUDE.md`、`core/README.md`、`docs/architecture.md`、根 `README.md`、`Module/workflow_engine/docs/migration.md`

- [ ] **Step 1: 文档全量核对**

逐项核对并更新：
1. `CLAUDE.md` — 模块树：`Module/task_system` → `Module/workflow_engine`（含 core/ 任务子系统、_lifecycle_task_system 描述）；模块索引表加 `Module.workflow_contract`；TaskSystem→WorkflowEngine 主类名；debug Tasks tab 描述。
2. `core/README.md` — `_lifecycle_task_system.py` 描述、`core/cognitive/delegation.py`、`core/hitl.py` 新增。
3. `docs/architecture.md` — task_system 架构段落改名 + 三区说明。
4. 根 `README.md` — 模块列表 + 文档导航中 task_system 引用。
5. **`Module/README.md`**（review 补：6+ 处活跃引用，Task 1 sed 未覆盖）— 模块概览表 `task_system/` 行（L24）、示例代码 `from Module.task_system import TaskSystem`（L252）、文档导航相对链接 `task_system/docs/`（L259，**裸相对链接需手工改** `workflow_engine/docs/`）、测试命令 `Module/task_system/tests/`（L280）。
6. `Module/workflow_engine/docs/migration.md` — 7 项归位状态更新（已迁移标记），SessionId 目标改为契约层；**AgentInfra 行加覆盖说明**：归契约层，覆盖原「Core 共享基础设施层」建议（理由见 spec §6.3）。
7. `Module/workflow_contract/README.md` 已在 Task 13 创建；**按 readme-conventions.md 要求在 `Module/README.md` 模块概览表加 `> 📖` 链接**注册 workflow_contract。
8. `todo.md` — 若有 task_system 相关项，更新路径。
9. `docs/completed.md` — 归档本次分离（2026-08 追加）。

- [ ] **Step 2: 全量测试 + 门禁复核**

```bash
make test-quick
make test-workflow-engine
grep -rn "from core\.\|import core\." Module/workflow_engine Module/tool --include="*.py" --exclude-dir=__pycache__ --exclude-dir=tests
# 活跃文档零 task_system 残留（含 Module/README.md 与裸相对链接；排除历史归档与计划自身）
grep -rn "Module\.task_system\|Module/task_system\|task_system/" --include="*.md" docs CLAUDE.md README.md Module/README.md \
  | grep -vE "docs/completed|docs/review|superpowers/plans/"
# 期望：空（或仅历史归档 docs/completed、docs/review 与计划文档）
# 各门禁均符合预期
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "docs: workflow_engine 分离收官文档同步"
```

---

## 计划自审

- **Spec 覆盖**：§4 改名（Task 1-4）、§5 契约层（Task 5-7）、§6 剥皮（Task 8-10）、§7 依赖反转（Task 11）、§8 阶段划分（Task 1-14 对应）、§9 测试策略（各任务 gate + Task 12/14）、§10 风险（test-normal 不兜底 → 阶段 1 用 test-quick + 模块测试；传递依赖 → Task 11；文档缺口 → Task 14）。
- **无占位符**：重命名用精确 sed；类型搬移给源→目标映射；门禁命令完整。
- **类型一致性**：15 标识符新名在 Task 2/3 与 spec §4.1 一致；契约类型清单 Task 5 与 spec §5.1 一致；`WorkflowConstraints`（非 `ConstraintConfig`）在 Task 5 测试与契约导出一致。
- **二轮 review 修复已合入**（10 项）：Task 2 子模块路径改写、Task 1 docs sed 防护、Task 5 源路径勘误 + 契约闭合 + SessionId 定案、Task 5 测试类型修正、Task 6 测试文件引用 + parse_iso 定案、Task 7 逐文件深路径精确改写 + UserProfile 延迟 + Tool 提前归位 Module.tool、Task 12 Step 3 白名单 grep 修正、Task 14 Module/README.md + workflow_contract 📖 + migration.md AgentInfra 覆盖。
- **已定案（原判断点）**：`parse_iso` → 契约层 utils（Task 6）；`Tool` → Module.tool（Task 5 Step 1a，不经契约层）；`SessionId` → 契约层 + core 重导出（Task 5）。
- **留给实现者的判断点**：`Tool` 投影实际字段形状（Task 5 测试构造，按 `Module/workflow_engine/schema/protocol.py` 现值）、DelegationPreparer 的注入接线细节（Task 9）——均在对应任务标注。
